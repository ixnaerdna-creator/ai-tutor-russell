"""Bertrand Russell – filozofický AI tútor pre texty poskytnuté vyučujúcim.

Spustenie: streamlit run app.py
Predpoklady: russell_texty.json, tutor_core.py, book_reader.py vedľa tohto súboru.
API kľúč patrí do Streamlit Secrets, nie do GitHub repozitára.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import streamlit as st
from google import genai
from google.genai import types

from book_reader import book_reader
from tutor_core import (
    MAX_TURNS, BookError, PassageIndex, api_error_message,
    context_for_model, format_transcript, read_book, source_label, works,
)

st.set_page_config(page_title="Bertrand Russell – Filozofický AI tútor", page_icon="📖", layout="wide")
st.markdown("""
<style>
.block-container{padding-top:1.0rem;max-width:98rem;}
h1,h2,h3{font-family:Georgia,serif;}
@media(max-width:800px){.block-container{padding-top:.65rem;}}
</style>
""", unsafe_allow_html=True)
BASE = Path(__file__).resolve().parent
LOG = logging.getLogger("russell_tutor")


def secret(name: str, fallback=None):
    try:
        return st.secrets.get(name, os.environ.get(name, fallback))
    except Exception:
        return os.environ.get(name, fallback)


API_KEY = secret("GEMINI_API_KEY")
MODEL = str(secret("GEMINI_MODEL", "gemini-3.5-flash-lite"))
THINKING = str(secret("GEMINI_THINKING_LEVEL", "low")).lower()
if THINKING not in {"minimal", "low", "medium", "high"}:
    THINKING = "low"
if MODEL == "gemini-3.8-flash" and THINKING == "minimal":
    THINKING = "low"
try:
    TIMEOUT_MS = max(30000, min(240000, int(secret("GEMINI_TIMEOUT_MS", 120000))))
except (ValueError, TypeError):
    TIMEOUT_MS = 120000


@st.cache_data(show_spinner=False)
def load_book(path: str):
    return read_book(Path(path))


try:
    corpus = load_book(str(BASE / "russell_texty.json"))
except BookError as exc:
    st.error(str(exc))
    st.stop()

all_works = works(corpus)
work_map = {w["id"]: w for w in all_works}
by_id = {r["id"]: r for r in corpus}
CORPUS_HASH = hashlib.sha256(
    json.dumps(corpus, ensure_ascii=False, sort_keys=True).encode("utf-8")
).hexdigest()


@st.cache_resource(show_spinner=False)
def get_index(path: str, digest: str):
    return PassageIndex(read_book(Path(path)))


@st.cache_resource(show_spinner=False)
def get_client(api_key: str, timeout: int):
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(
        timeout=timeout, retry_options=types.HttpRetryOptions(attempts=1),
    ))


index = get_index(str(BASE / "russell_texty.json"), CORPUS_HASH)
client = get_client(API_KEY, TIMEOUT_MS) if API_KEY else None

EXPLANATION_RULES = """Si vysokoškolský učiteľ analytickej filozofie. Vysvetľuješ iba
Russellove texty citované v predložených podkladoch. Nepoužívaj web, sekundárnu
literatúru, nepredložené diela ani svoje predtrénované filozofické znalosti ako dôkaz.
Nedopĺňaj chýbajúce pasáže podľa pamäti. Píš spisovnou slovenčinou. Pri českom preklade
používaj slovenskú odbornú terminológiu, ale pri doslovných citátoch zachovaj české znenie.
Rozlišuj: (1) explicitné tvrdenie autora; (2) rekonštrukciu premís a argumentu;
(3) vzťahy k ostatným doloženým pasážam a prípadné otvorené problémy.
Odkazuj na názov textu, kapitolu/prednášku, stránku. Rxx-yyyy sú iba interné čísla
digitalizácie; nesmú pôsobiť ako Russellovo autorské číslovanie. Neuvádzaj nedoložené
čísla strán. Pri spoločnom texte Principia Mathematica neignoruj Whiteheadovo spoluautorstvo.
V dialógu s Coplestonom sú dostupné iba Russellove repliky: nesmieš rekonštruovať
Coplestonove doslovné otázky či tvrdenia, ktoré nemáš v podkladoch.
Ak pasáž obsahuje poškodený OCR zápis alebo nečitateľnú logickú notáciu, upozorni na
neistotu a odporuč porovnanie so skenom, nie domyslenú opravu.
Vysvetli vybraný odsek analyticky, presne a zrozumiteľne (približne 140–250 slov).
Neklaď na konci otázku, lebo ide o výklad mimo diskusie.
"""

DIALOGUE_RULES = """Si sokratovský filozofický tútor Bertranda Russella.
Jedinými autorizovanými prameňmi sú pasáže z priloženého korpusu v aktuálnej požiadavke.
Žiadne webové vyhľadávanie, sekundárna literatúra ani vedomosti z iných Russellových kníh.
Odpovedaj na skutočnú otázku študenta, a nie na posledný výklad kliknutého odseku:
výklady nie sú súčasťou konverzácie. Dbaj na filozofickú argumentáciu, implicitné premisy,
logickú platnosť, protinámietky a odlíšenie výslovných tvrdení od hypotéz.
Prípadnú hypotézu výslovne označ ako hypotézu, nie za Russellovo tvrdenie.
Odkazuj na konkrétne doložené state a strany; interné ID Rxx-yyyy nie sú autorove tézy.
Pri Principia Mathematica rešpektuj Whiteheadovo spoluautorstvo a neúplnosť OCR vzorcov.
Pri rozhlasovej diskusii s Coplestonom sú k dispozícii len Russellove repliky.
Píš po slovensky odborne, ale prístupne; pôvodné české citáty neprekladaj ako doslovné.
Štandardná odpoveď má približne 110–230 slov a končí JEDNOU otázkou na kritické premýšľanie.
Ak sú podklady nedostatočné, povedz to; nepodsúvaj študentovi postoje a nefinguj citácie.
Pri poslednom 20. vstupe odpovedz a vypracuj zvlášť kritické zhodnotenie viditeľnej
20-krokovej diskusie – argumentačný posun, silné/slabé miesta, otvorené otázky.
Záverečnú odpoveď neukončuj ďalšou otázkou. Nepredstieraj, že poznáš myslenie študenta
mimo priebehu zapísanej diskusie.
"""


def ask_model(system: str, prompt: str, *, final: bool = False) -> str:
    if client is None:
        raise RuntimeError("Chýba GEMINI_API_KEY v Streamlit Secrets.")
    start = perf_counter()
    try:
        response = client.models.generate_content(
            model=MODEL, contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                thinking_config=types.ThinkingConfig(thinking_level=THINKING),
                max_output_tokens=3100 if final else 2150,
            ),
        )
        result = (response.text or "").strip()
        if not result:
            raise RuntimeError("Model vrátil prázdnu odpoveď.")
        return result
    finally:
        LOG.info("Model=%s; trvanie=%.1fs; vstup=%d znakov", MODEL,
                 perf_counter() - start, len(prompt))


def init_session():
    initial = {
        "selected_work": all_works[0]["id"],
        "selected_id": None,
        "last_reader_event": None,
        "explanation": "", "explanation_error": "", "show_explanation": False,
        "explanation_cache": {},
        "scope_choice": "Len vybraný text",
        "scope_work": "", "scope_title": "",
        "messages": [], "count": 0,
        "pending": None, "pending_failed": False, "pending_error": "",
        "session_id": uuid4().hex[:12], "student_name": "",
    }
    for name, value in initial.items():
        if name not in st.session_state:
            st.session_state[name] = value


init_session()


def on_work_changed():
    # Prepínanie čítačky nemení rozbehnutú diskusiu.
    st.session_state.selected_id = None
    st.session_state.last_reader_event = None
    st.session_state.show_explanation = False
    st.session_state.explanation_error = ""


def reset_dialogue():
    st.session_state.messages = []
    st.session_state.count = 0
    st.session_state.pending = None
    st.session_state.pending_failed = False
    st.session_state.pending_error = ""
    st.session_state.scope_work = ""
    st.session_state.scope_title = ""
    st.session_state.session_id = uuid4().hex[:12]
    st.session_state.show_explanation = False


@st.cache_data(show_spinner=False, ttl=604800, max_entries=1200)
def shared_explanation(identifier: str, digest: str, model: str, thinking: str):
    row = by_id[identifier]
    evidence = context_for_model(
        index, row["text"][:2500] + " " + row["work_title"],
        work_id=row["work_id"], focus_id=identifier, limit=9, char_budget=11500,
    )
    prompt = (f"VYBRANÝ ODSEK: {source_label(row)}\n\n{row['text']}\n\n"
              f"ĎALŠIE SÚVISIACE PASÁŽE:\n{evidence}\n\n"
              "Vysvetli odsek v kontexte predloženého diela a rozlišuj doložené tvrdenia od interpretácie.")
    return ask_model(EXPLANATION_RULES, prompt)


def explain(identifier: str):
    cached = st.session_state.explanation_cache.get(identifier)
    if cached is not None:
        st.session_state.explanation = cached
        st.session_state.explanation_error = ""
        st.session_state.show_explanation = True
        return
    try:
        with st.spinner("Tútor pripravuje výklad odseku…"):
            text = shared_explanation(identifier, CORPUS_HASH, MODEL, THINKING)
    except Exception as exc:
        st.session_state.explanation = ""
        st.session_state.explanation_error = api_error_message(exc, MODEL)
        st.session_state.show_explanation = False
        LOG.exception("Russell – nepodarilo sa vytvoriť výklad")
    else:
        st.session_state.explanation_cache[identifier] = text
        st.session_state.explanation = text
        st.session_state.explanation_error = ""
        st.session_state.show_explanation = True


def process_pending():
    p = st.session_state.pending
    if not p or st.session_state.count >= MAX_TURNS:
        return
    question = p["text"]
    prev_questions = " ".join(m["content"] for m in st.session_state.messages
                              if m["role"] == "user")[-2500:]
    evidence = context_for_model(
        index, question + "\n" + prev_questions, work_id=p["work_id"],
        limit=11, char_budget=15000,
    )
    history = "\n\n".join(
        f"{'ŠTUDENT' if m['role']=='user' else 'TÚTOR'}: {m['content']}"
        for m in st.session_state.messages
    )[-17000:]
    final = st.session_state.count == MAX_TURNS - 1
    instruction = (
        "TOTO JE POSLEDNÝ (20.) VSTUP. Odpovedz a pridaj osobitne záverečné "
        "kritické hodnotenie viditeľnej diskusie. Už sa nič nepýtaj."
        if final else "Toto nie je posledný krok. Zakonči odpoveď jednou analytickou otázkou."
    )
    prompt = (
        f"ROZSAH POVOLENÝCH TEXTOV: {p['scope_title']}\n\n"
        f"DOLOŽENÉ PASÁŽE:\n{evidence}\n\n"
        f"DOSLOVNÁ HISTÓRIA ROZHOVORU (bez výkladov kliknutých odsekov):\n{history or '(začiatok)'}\n\n"
        f"OTÁZKA ŠTUDENTA ({st.session_state.count + 1}/{MAX_TURNS}):\n{question}\n\n{instruction}"
    )
    try:
        with st.spinner("Tútor pripravuje odpoveď…"):
            reply = ask_model(DIALOGUE_RULES, prompt, final=final)
    except Exception as exc:
        st.session_state.pending_failed = True
        st.session_state.pending_error = api_error_message(exc, MODEL)
        LOG.exception("Russell – nepodarilo sa vytvoriť odpoveď")
    else:
        st.session_state.messages.extend([
            {"role": "user", "content": question},
            {"role": "assistant", "content": reply},
        ])
        st.session_state.count += 1
        st.session_state.scope_work = p["work_id"] if p["work_id"] else "*"
        st.session_state.scope_title = p["scope_title"]
        st.session_state.pending = None
        st.session_state.pending_failed = False
        st.session_state.pending_error = ""


# Pri chybe sa študentská otázka NESMIE započítať ani automaticky opakovať.
if st.session_state.pending and not st.session_state.pending_failed and API_KEY:
    process_pending()

st.title("Bertrand Russell – filozofický AI tútor")
if not API_KEY:
    st.warning("V Streamlit Secrets chýba GEMINI_API_KEY. Texty možno čítať a vyhľadávať; AI výklad a diskusia zostávajú vypnuté.")
if len(all_works) != 25:
    st.warning(f"V JSON sa našlo {len(all_works)} textov namiesto očakávaných 25. Skontrolujte zdrojový súbor.")


def work_label(identifier: str) -> str:
    w = work_map[identifier]
    return f"{w['book']}  ·  {w['title']}"


st.selectbox("Vyberte Russellov text", options=list(work_map),
             format_func=work_label, key="selected_work", on_change=on_work_changed)
active_work = st.session_state.selected_work
rows = [row for row in corpus if row["work_id"] == active_work]

if active_work == "russell-02":
    st.caption("Úvod k Principia Mathematica je spoločným textom Russella a A. N. Whiteheada; matematický OCR prepis môže obsahovať chyby.")
elif active_work == "russell-25":
    st.caption("K dispozícii sú iba Russellove repliky z diskusie s Frederickom Coplestonom, nie celý rozhovor.")

left, right = st.columns([2, 1], gap="large", vertical_alignment="top")
with left:
    st.caption("Kliknutím na odsek otvoríte jeho výklad.")
    event = book_reader(
        data={"paragraphs": rows, "selected": st.session_state.selected_id,
              "bookKey": f"{active_work}-{CORPUS_HASH[:14]}"},
        key="russell_reader", width="stretch", height=780,
        on_selected_change=lambda: None,
    )
    chosen = event.selected
    if isinstance(chosen, dict):
        identifier, nonce = chosen.get("id"), chosen.get("nonce")
        event_key = f"{active_work}:{identifier}:{nonce}"
        if (identifier in by_id and by_id[identifier]["work_id"] == active_work
                and event_key != st.session_state.last_reader_event):
            st.session_state.last_reader_event = event_key
            if by_id[identifier]["type"] == "paragraph":
                st.session_state.selected_id = identifier
                if API_KEY:
                    explain(identifier)
                else:
                    st.session_state.explanation_error = "Výklad vyžaduje GEMINI_API_KEY v nastaveniach aplikácie."
                st.rerun()

with right:
    title_col, reset_col = st.columns([1.4, 1], vertical_alignment="center")
    with title_col:
        st.markdown(f"#### Diskusia · {st.session_state.count}/{MAX_TURNS}")
    with reset_col:
        if st.button("Vynulovať diskusiu", width="stretch", key="reset_chat"):
            reset_dialogue()
            st.rerun()
    st.progress(st.session_state.count / MAX_TURNS)

    if st.session_state.explanation_error and st.session_state.selected_id:
        st.error(st.session_state.explanation_error)
        if "429" in st.session_state.explanation_error:
            st.markdown("[Skontrolovať kvóty Gemini](https://aistudio.google.com/rate-limit)")
        if API_KEY and st.button("Skúsiť výklad znova", width="stretch"):
            explain(st.session_state.selected_id)
            st.rerun()

    if st.session_state.show_explanation and st.session_state.selected_id:
        row = by_id[st.session_state.selected_id]
        page = f"s. {row['source_print_page']}" if row.get("source_print_page") is not None else ""
        st.markdown(f"**Výklad · {row['work_title']} {page}**")
        with st.container(height=230, border=False):
            st.markdown(st.session_state.explanation)
        if st.button("Zavrieť výklad", width="stretch"):
            st.session_state.show_explanation = False
            st.rerun()

    st.markdown("**Sokratovský dialóg**")
    st.radio("Rozsah diskusie", ["Len vybraný text", "Všetky Russellove texty"],
             horizontal=True, key="scope_choice",
             disabled=bool(st.session_state.count or st.session_state.pending),
             help="Rozsah sa uzamkne pri prvej otázke; prepínanie čítačky ho potom nemení.")
    if st.session_state.count:
        st.caption(f"Textové podklady: {st.session_state.scope_title}")
    with st.container(height=240 if st.session_state.show_explanation else 420,
                      border=True, autoscroll=True):
        if not st.session_state.messages:
            st.caption("Začnite vlastnou otázkou, námietkou alebo pokusom o rekonštrukciu Russellovho argumentu.")
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    if st.session_state.pending_failed:
        st.error(st.session_state.pending_error)
        st.info("Otázka zostala uložená a nezapočítala sa do 20 krokov.")
        retry_col, cancel_col = st.columns(2)
        with retry_col:
            if st.button("Skúsiť znova", width="stretch", disabled=not API_KEY):
                st.session_state.pending_failed = False
                st.rerun()
        with cancel_col:
            if st.button("Zrušiť otázku", width="stretch"):
                st.session_state.pending = None
                st.session_state.pending_failed = False
                st.session_state.pending_error = ""
                st.rerun()
    elif st.session_state.count < MAX_TURNS:
        with st.form("dialogue_question", border=False, clear_on_submit=True):
            question = st.text_area("Otázka alebo argument", height=78, max_chars=3000,
                                    placeholder="Čo Russell myslí pojmom logický atóm?",
                                    label_visibility="collapsed")
            submitted = st.form_submit_button("Odoslať tútorovi", type="primary",
                                               width="stretch", disabled=not API_KEY)
        if submitted:
            if not question.strip():
                st.warning("Napíšte otázku alebo argument.")
            else:
                if st.session_state.count == 0:
                    scoped = active_work if st.session_state.scope_choice == "Len vybraný text" else None
                    label = work_map[scoped]["title"] if scoped else "Všetky Russellove texty"
                else:
                    scoped = None if st.session_state.scope_work == "*" else st.session_state.scope_work
                    label = st.session_state.scope_title
                st.session_state.pending = {"text": question.strip(), "work_id": scoped,
                                            "scope_title": label}
                st.session_state.pending_failed = False
                st.session_state.show_explanation = False
                st.rerun()
    else:
        st.success("Dosiahli ste 20 študentských vstupov. Dialóg je ukončený.")

    if st.session_state.messages:
        st.text_input("Meno študenta/študentky (voliteľné)", key="student_name",
                      placeholder="Na odovzdanie v Moodle")
        output = format_transcript(st.session_state.messages, st.session_state.student_name,
                                   MODEL, st.session_state.session_id, st.session_state.scope_title)
        st.download_button("Stiahnuť celý dialóg (TXT)", data=output.encode("utf-8-sig"),
                           file_name=f"russell_dialog_{st.session_state.session_id}.txt",
                           mime="text/plain", width="stretch")

with st.expander("O textoch a presnosti digitalizácie", expanded=False):
    st.write("Korpus obsahuje slovenské a české preklady Russellových statí a prednášok. "
             "Podklady pochádzajú z OCR, ktoré neprešlo úplnou ručnou korektúrou. "
             "Pri overovaní interpretácie porovnajte vybrané pasáže s tlačenou stranou; "
             "obzvlášť matematické značky v úvode k Principia Mathematica. "
             "Model nepoužíva webové vyhľadávanie, no nie je technicky možné "
             "úplne vylúčiť jeho predtrénované znalosti.")
