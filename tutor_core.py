"""Pomocné funkcie pre Russellov korpus – bez závislosti na Streamlit/Gemini."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import re
import unicodedata
from zoneinfo import ZoneInfo

MAX_TURNS = 20
ID_RE = re.compile(r"\bR\d{2}-\d{4}\b", re.IGNORECASE)
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
ALLOWED_TYPES = {"paragraph", "heading", "note"}

class BookError(ValueError):
    """Korpus sa nedá bezpečne použiť."""


def read_book(path: Path) -> list[dict]:
    """Načíta korpus vrátane metadát: titul, kapitol, strán a údajov o preklade."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise BookError(f"Súbor {path.name} chýba. Musí byť v priečinku vedľa app.py.") from exc
    except json.JSONDecodeError as exc:
        raise BookError(f"Neplatný JSON ({path.name}): riadok {exc.lineno}, stĺpec {exc.colno}.") from exc
    if isinstance(raw, dict):
        raw = raw.get("paragraphs")
    if not isinstance(raw, list) or not raw:
        raise BookError("Korpus musí byť neprázdny zoznam odsekov.")
    result, seen = [], set()
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise BookError(f"Záznam č. {n} nemá správny formát.")
        ident = str(item.get("id") or "").strip()
        kind = item.get("type", "paragraph")
        work_id = str(item.get("work_id") or "").strip()
        work_title = str(item.get("work_title") or "").strip()
        body = item.get("text")
        if (not ID_RE.fullmatch(ident) or ident in seen or kind not in ALLOWED_TYPES
                or not work_id or not work_title or not isinstance(body, str) or not body.strip()):
            raise BookError(f"Nesprávny alebo duplicitný záznam č. {n}: {ident!r}.")
        seen.add(ident)
        result.append({**item, "id": ident, "type": kind, "text": body.strip(),
                       "work_id": work_id, "work_title": work_title})
    return result


def works(rows: list[dict]) -> list[dict]:
    ordered = {}
    for r in rows:
        wid = r["work_id"]
        if wid not in ordered:
            src = Path(r.get("source_pdf") or "").stem
            if "Jazyk a poznanie" in src:
                collection = "Jazyk a poznanie (SK)"
            elif "křesťanem" in src or "kresťanem" in src:
                collection = "Proč nejsem křesťanem a jiné eseje (CZ)"
            else:
                collection = src
            ordered[wid] = {"id": wid, "title": r["work_title"],
                            "book": collection, "language": r.get("language", "sk")}
    return list(ordered.values())


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text).casefold())
    return "".join(c for c in value if not unicodedata.combining(c))


def terms(text: str) -> list[str]:
    return [w for w in WORD_RE.findall(normalize(text)) if len(w) > 2]

# Jednoduché dvojjazyčné lexikálne ekvivalenty; pôvodné texty sa nijako nemenia.
EQUIVALENTS = {
    "pravda": "pravda", "pravdy": "pravda", "pravde": "pravda",
    "pravdivost": "pravdivost", "pravdivosti": "pravdivost",
    "poznanie": "poznanie", "poznani": "poznanie", "poznatky": "poznanie",
    "vyznam": "vyznam", "vyznamu": "vyznam", "vyznamem": "vyznam",
    "vseobecniny": "vseobecnina", "vseobecneniny": "vseobecnina",
    "pricina": "pricina", "priciny": "pricina", "priciny": "pricina",
    "zmysel": "zmysel", "smysl": "zmysel", "smyslu": "zmysel",
    "jazyk": "jazyk", "jazyka": "jazyk",
    "deskripcia": "deskripcia", "deskripce": "deskripcia", "deskripcie": "deskripcia",
    "atomizmus": "atomizmus", "atomismus": "atomizmus",
    "viera": "viera", "vira": "viera", "very": "viera",
    "existencia": "existencia", "existence": "existencia",
    "poznat": "poznanie", "poznatku": "poznanie",
}
STOPWORDS = {
    "ako", "preco", "prečo", "podla", "podľa", "co", "čo", "ktorý", "ktory", "ktorá", "ktora",
    "teda", "tomto", "tento", "toho", "tych", "tých", "jedno", "medzi", "pre", "pri", "jeho", "je", "som", "mam", "ake", "aký", "by", "aby", "ten", "akoze", "alebo", "ale", "toto", "moze", "možno", "su", "sú"
}

def termset(text: str) -> set[str]:
    return {EQUIVALENTS.get(t, t) for t in terms(text) if t not in STOPWORDS}


class PassageIndex:
    """Lokálne vyhľadávanie; nevkladá do odpovede žiadne vonkajšie údaje."""
    def __init__(self, rows: list[dict]):
        self.paragraphs = [x for x in rows if x["type"] == "paragraph"]
        self.by_id = {x["id"].upper(): i for i, x in enumerate(self.paragraphs)}
        self.docs = [Counter(EQUIVALENTS.get(t, t) for t in terms(
            x["text"] + " " + x["work_title"] + " " + (x.get("chapter") or "")
            + " " + (x.get("subchapter") or "")
        ) if t not in STOPWORDS) for x in self.paragraphs]
        self.df = Counter(t for doc in self.docs for t in doc)
        self.sizes = {x["work_id"]: sum(r["work_id"] == x["work_id"] for r in self.paragraphs)
                      for x in self.paragraphs}

    def find(self, query: str, *, work_id: str | None = None,
             focus_id: str | None = None, limit: int = 11) -> list[dict]:
        candidates = [i for i, r in enumerate(self.paragraphs)
                      if work_id is None or r["work_id"] == work_id]
        if not candidates:
            return []
        toks = termset(query)
        refids = {x.upper() for x in ID_RE.findall(query)}
        root_fragments = {t[:5] for t in toks if len(t) > 5}
        corpus_n = len(self.paragraphs)
        ranked = []
        for i in candidates:
            row, doc = self.paragraphs[i], self.docs[i]
            score = sum((1 + math.log(doc[t])) * math.log(1 + (corpus_n + 1) / (self.df[t] + 1))
                        for t in toks if doc[t])
            if root_fragments and any(key.startswith(root) for key in doc for root in root_fragments):
                score += 0.3
            if row["id"].upper() in refids:
                score += 400
            if focus_id and row["id"] == focus_id:
                score += 2000
            if score > 0:
                ranked.append((score, i))
        best = [i for _, i in sorted(ranked, key=lambda x: (-x[0], x[1]))][:limit]
        if focus_id and focus_id.upper() in self.by_id:
            k = self.by_id[focus_id.upper()]
            if work_id is None or self.paragraphs[k]["work_id"] == work_id:
                neighbors = [k] + [j for j in (k-1, k+1)
                                   if 0 <= j < len(self.paragraphs)
                                   and self.paragraphs[j]["work_id"] == self.paragraphs[k]["work_id"]]
                best = neighbors + best
        positions = list(dict.fromkeys(best))[:limit]
        return [self.paragraphs[i] for i in positions]


def source_label(row: dict) -> str:
    bits = [row["work_title"]]
    for field in ("chapter", "subchapter"):
        val = str(row.get(field) or "").strip()
        if val and val not in bits: bits.append(val)
    if row.get("section") not in (None, ""):
        bits.append(f"oddiel {row['section']}")
    if row.get("source_print_page") not in (None, ""):
        bits.append(f"s. {row['source_print_page']}")
    return " | ".join(bits) + f" | interný identifikátor {row['id']}"


def context_for_model(index: PassageIndex, query: str, *, work_id: str | None = None,
                      focus_id: str | None = None, limit: int = 11,
                      char_budget: int = 13000) -> str:
    evidence = index.find(query, work_id=work_id, focus_id=focus_id, limit=limit)
    if not evidence:
        return "Relevantná pasáž sa v sprístupnenom korpuse nenašla. Neodhaduj obsah z pamäti."
    chunks, remaining = [], char_budget
    for r in evidence:
        header = f"[{source_label(r)}; jazyk: {r.get('language', 'sk')}]\n"
        max_piece = 5000 if r["id"] == focus_id else 1700
        n = min(max_piece, len(r["text"]), remaining - len(header) - 65)
        if n < 90:
            break
        snippet = r["text"] if n == len(r["text"]) else r["text"][:n].rsplit(" ", 1)[0] + " […] [skrátené]"
        block = header + snippet
        remaining -= len(block) + 2
        chunks.append(block)
    return (
        "DOSLOVNÉ PRIMÁRNE PASÁŽE Z POSKYTNUTÝCH RUSSELLOVÝCH TEXTOV:\n\n"
        + "\n\n".join(chunks) + "\n\n"
        + "Upozornenie: Rxx-yyyy sú interné identifikátory digitalizácie, nie Russellovo číslovanie. "
          "Úvod do Principia Mathematica je spoločnou prácou Russella a Whiteheada. "
          "V dialógu s Coplestonom sú dostupné iba Russellove repliky. "
          "Vstupné preklady môžu obsahovať OCR chyby vrátane matematických znakov. "
          "Neurčité formulácie neopravuj podľa pamäti a pri nedostatku textu to priznaj."
    )


def api_error_message(exc: Exception, model: str = "") -> str:
    code = getattr(exc, "code", None)
    s = str(exc).lower()
    model_desc = f" (model: {model})" if model else ""
    if code == 429 or "resource_exhausted" in s or "429" in s:
        return f"Gemini API odmietlo požiadavku kvôli kvóte (429){model_desc}. Skontrolujte kvóty RPM/TPM/RPD v Google AI Studio; otázka sa nezapočítala."
    if code == 503 or "service_unavailable" in s or "503" in s:
        return "Gemini API je dočasne nedostupné (503). Opakujte požiadavku neskôr."
    if code == 404 or "not_found" in s or "404" in s:
        return f"Model nie je dostupný (404){model_desc}. Skontrolujte názov v GEMINI_MODEL."
    if code in (401,403) or "401" in s or "403" in s:
        return "Neplatný API kľúč alebo zakázaný prístup. Skontrolujte Streamlit Secrets."
    if "readtimeout" in s or "timed out" in s or "timeout" in s:
        return "Gemini neodpovedal v stanovenom časovom limite (ReadTimeout). Otázka zostala uložená; skúste to znova."
    return f"Odpoveď Gemini sa nepodarilo získať ({type(exc).__name__}). Podrobnosti sú v serverových logoch."


def format_transcript(messages: list[dict], student_name: str, model: str,
                      session_id: str, scope_title: str,
                      at: datetime | None = None) -> str:
    timestamp = at or datetime.now(ZoneInfo("Europe/Bratislava"))
    parts = ["BERTRAND RUSSELL – FILOZOFICKÝ DIALÓG", "=" * 44,
             f"Študent/ka: {student_name.strip() or '(neuvedené)'}",
             f"Dátum: {timestamp:%d. %m. %Y %H:%M %Z}",
             f"Relácia: {session_id}",
             f"Použitý AI model: {model}",
             f"Rozsah primárnych textov: {scope_title}",
             f"Počet študentských vstupov: {sum(x['role']=='user' for x in messages)}/{MAX_TURNS}",
             "", "DOSLOVNÝ PREPIS", "-"*44, ""]
    for item in messages:
        parts += ["ŠTUDENT:" if item["role"] == "user" else "AI TÚTOR:", item["content"].strip(), ""]
    return "\n".join(parts)
