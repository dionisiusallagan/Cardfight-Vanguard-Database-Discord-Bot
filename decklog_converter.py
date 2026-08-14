"""Convert a decklog (JP host) decklist into Cardfight Connect deck JSON.

Target format (matches the game's DeckList::FromJSON field names):
    {"regulation": <int>, "hasRide": <bool>, "rideCrest": <int>,
     "mainDeck": [<cfc index>...], "rideDeck": [<cfc index>...],
     "strideDeck": [<cfc index>...]}

Data sources
------------
- Decklog JSON API (JP): POST /system/app/api/view/<CODE> on
  decklog.bushiroad.com with the headers used by the site's own XHR client
  (Referer/Origin + X-Requested-With + a real browser User-Agent). Returns
  the deck with ``list`` (main deck), ``sub_list``, ``p_list`` (ride/crest
  zone) and ``deck_param1`` (regulation letter). Valid VG decks have
  ``game_title_id == 1``.
- Decklog sort API: POST /system/app/api/sort/<game_title_id> with
  {deck_param1, deck_param2, no: [card_numbers], sub_no: []} returns the
  list in the site's display order. That order is what the game's example
  deck JSONs use, so we apply it before emitting mainDeck.
- Cardfight fandom wiki: opensearch by Japanese name to recover the English
  title, then match that against the CFC card database's English names.
- CFC database (sharedassets0.assets): ``cardsData``/``cardsData_v``/
  ``cardsData_g`` JSON documents hold the canonical name -> index table.

Mapping rules
-------------
- regulation: deck_param1 letter -> CFC regulation index
  (D=0 Standard, V=1, G=2, P=3 Premium, O=8 Overdress; matched against the
  game's regulations doc). 
- hasRide: true when the deck has a ride deck (p_list grade slots present).
- rideCrest: 1 for D-era Standard decks (energy generator), else 0.
- mainDeck: each ``list`` card repeated ``num`` times, in sort-API order.
- rideDeck: the p_list grade_0..grade_3 units in grade order.
- strideDeck: empty for Standard; stride cards from p_list stride slots
  otherwise.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time

import requests

try:
    from rapidfuzz import fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    fuzz = None
    RAPIDFUZZ_AVAILABLE = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DECKLOG_API = "https://decklog.bushiroad.com/system/app/api"
CFC_ASSETS = r"C:\Program Files (x86)\Cardfight Connect\Cardfight Connect_Data\sharedassets0.assets"
CFC_DOCS = [342612, 12263220, 14089220]  # cardsData, cardsData_v, cardsData_g
REGULATIONS_DOC = 12248532

# deck_param1 letter -> CFC regulation index (from the game's regulations doc).
REGULATION_MAP = {
    "D": 0,   # Standard
    "V": 1,   # V Format
    "G": 2,   # G Format
    "P": 3,   # Premium
    "O": 8,   # Overdress
    "L": 6,   # Legion
    "B": 7,   # Break Ride
}

# Nationless (Elemental) trigger substitutes by CFC "gift" (trigger type).
# The four Elementals are the canonical same-nationless triggers used when a
# decklog card (e.g. a collab/mini-chara promo) is not in the CFC database.
ELEMENTAL_TRIGGER_INDEX = {
    "Heal": 3973,      # Wood Elemental, Leafy
    "Critical": 2214,  # Light Elemental, Pachiri
    "Draw": 1210,      # Earth Elemental, Garara
    "Front": 1752,     # Heat Elemental, Marg
}
NATIONLESS_TRIGGER_GIFTS = set(ELEMENTAL_TRIGGER_INDEX)

# JP card names known to exist on decklog but not in CFC, mapped to their
# "gift" (trigger type) so they can be substituted by a same-type nationless
# trigger. Extended as new collab/mini-chara promos are reported.
COLLAB_TRIGGER_GIFTS = {
    "ミニキャラ 八雲カゲツ": "Heal",
}

CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cfc_deck_index_cache.json"
)
NAME_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cfc_name_cache.json"
)
CACHE_TTL = 7 * 24 * 60 * 60

CARD_MAX_COPIES = 4

_cfc_index_by_name: dict[str, int] | None = None
_cfc_cards: dict[int, dict] | None = None
_name_cache: dict[str, int] | None = None


def _load_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) > CACHE_TTL:
            return data.get("data")
        return data.get("data")
    except (OSError, ValueError):
        return None


def _save_cache(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "data": data}, f, ensure_ascii=False)
    except OSError:
        pass


# ---------------------------------------------------------------- CFC DB

def _find_json_end(text: str, start: int) -> int:
    depth = 0
    in_str = False
    esc = False
    i = start
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _load_cfc_index() -> dict[str, int]:
    """English card name -> CFC index, from the game's card database."""
    global _cfc_index_by_name
    if _cfc_index_by_name is not None:
        return _cfc_index_by_name

    cached = _load_cache(CACHE_FILE)
    if cached:
        _cfc_index_by_name = cached
        return cached

    if not os.path.isfile(CFC_ASSETS):
        raise FileNotFoundError(CFC_ASSETS)
    with open(CFC_ASSETS, "rb") as f:
        text = f.read().decode("latin1")

    index = {}
    for doc_start in CFC_DOCS:
        end = _find_json_end(text, doc_start)
        if end == -1:
            continue
        try:
            doc = json.loads(text[doc_start:end + 1])
        except ValueError:
            continue
        for card in doc.get("cards", {}).values():
            name = card.get("name")
            if name:
                key = name.lower()
                idx = card["index"]
                if key not in index or idx < index[key]:
                    index[key] = idx
    _cfc_index_by_name = index
    _save_cache(CACHE_FILE, index)
    return index


def _load_cfc_cards() -> dict[int, dict]:
    """index -> full CFC card dict (for effect-text matching)."""
    global _cfc_cards
    if _cfc_cards is not None:
        return _cfc_cards
    if not os.path.isfile(CFC_ASSETS):
        raise FileNotFoundError(CFC_ASSETS)
    with open(CFC_ASSETS, "rb") as f:
        text = f.read().decode("latin1")
    cards = {}
    for doc_start in CFC_DOCS:
        end = _find_json_end(text, doc_start)
        if end == -1:
            continue
        try:
            doc = json.loads(text[doc_start:end + 1])
        except ValueError:
            continue
        for card in doc.get("cards", {}).values():
            if card.get("name"):
                cards[card["index"]] = card
    _cfc_cards = cards
    return cards


# ------------------------------------------------------- name bridge

def _norm(s: str) -> str:
    return s.lower().strip()


def _fuzzy_score(a: str, b: str) -> float:
    if RAPIDFUZZ_AVAILABLE:
        return fuzz.WRatio(a.lower(), b.lower())
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100


def _clean_effect(text: str) -> str:
    """Normalize a card effect for matching (drops wiki markup)."""
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\{\{[^{}]*\}\}", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[’'\"“”]", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _wiki_page_meta(title: str) -> dict | None:
    """grade/nation/effect for a wiki card page, or None."""
    try:
        resp = requests.get(
            "https://cardfight.fandom.com/api.php",
            params={
                "action": "parse",
                "page": title,
                "prop": "wikitext",
                "format": "json",
            },
            headers={"User-Agent": "VanguardCardBot/1.0"},
            timeout=15,
        )
        txt = resp.json()["parse"]["wikitext"]["*"]
    except (requests.RequestException, ValueError, KeyError):
        return None
    meta = {}
    m_grade = re.search(r"\|\s*grade\s*=\s*(\d+)", txt)
    if m_grade:
        meta["grade"] = int(m_grade.group(1))
    m_trig = re.search(r"\|\s*trig\s*=\s*(.+)", txt)
    if m_trig:
        meta["trig"] = m_trig.group(1).strip()
    m_nation = re.search(r"\|\s*nation\s*=\s*(.+)", txt)
    if m_nation:
        meta["nations"] = [n.strip() for n in m_nation.group(1).split(",") if n.strip()]
    m_effect = re.search(r"\|\s*effect\s*=\s*(.+?)\n(?:}}|$)", txt, re.S)
    if m_effect:
        meta["effect"] = _clean_effect(m_effect.group(1))
    return meta or None


def _wiki_opensearch(query: str) -> list[str]:
    try:
        resp = requests.get(
            "https://cardfight.fandom.com/api.php",
            params={
                "action": "opensearch",
                "search": query,
                "limit": 10,
                "namespace": 0,
                "format": "json",
            },
            headers={"User-Agent": "VanguardCardBot/1.0"},
            timeout=15,
        )
        data = resp.json()
        return data[1] if isinstance(data, list) and len(data) > 1 else []
    except (requests.RequestException, ValueError):
        return []


def _wiki_queries(jp_name: str) -> list[str]:
    """Candidate opensearch queries, most specific first.

    Some decklog names don't resolve directly (e.g. short AI names), so we
    also try dropping designator markers and then each space-separated token,
    which reliably finds the card for short unit names.
    """
    queries = [jp_name.strip()]
    stripped = re.sub(r"[「」『』《》“”『』（）\(\)【】]", "", jp_name)
    stripped = re.sub(r"[^0-9A-Za-zぁ-んァ-ヶ一-龠] +", " ", stripped).strip()
    if stripped and stripped != queries[0]:
        queries.append(stripped)
    for token in jp_name.split():
        if len(token) >= 2 and token not in queries:
            queries.append(token)
    return queries


def _lookup_cfc_index(jp_name: str, grade: int | None = None) -> int | None:
    """English name -> CFC index, with fallbacks for odd queries.

    Wiki opensearch ranks results by relevance, so we take the first title
    that has a solid CFC match rather than maximizing the score across all
    candidates (maximizing conflates similarly-named cards, e.g. two JP
    Rewinder cards that differ only by one kanji).

    When no name matches strongly, fall back to effect-text matching: the CFC
    translation sometimes differs from the wiki's title (e.g. CFC "Valgrowth
    of Expansive Summer" vs wiki "Valgros of Summer's Expanse"), but the
    abilities are textually near-identical, which uniquely identifies the card.

    ``grade`` (optional) restricts candidates to that CFC grade; ride-deck
    slots know their grade, which disambiguates e.g. "Estacion" (grade 0)
    from "Legend of the Raging Skies, Estacion" (grade 2).
    """
    index = _load_cfc_index()
    cards = _load_cfc_cards() if grade is not None else None
    seen = set()
    first_title = None
    for query in _wiki_queries(jp_name):
        for title in _wiki_opensearch(query):
            title = re.sub(r"\s*\((?:card|unit|EN|JP)\)\s*$", "", title).strip()
            if title.startswith("Card Gallery:") or title in seen:
                continue
            if first_title is None:
                first_title = title
            seen.add(title)
            # exact name in the CFC DB
            hit = index.get(_norm(title))
            if hit is not None:
                if grade is None or cards[hit].get("grade") == grade:
                    return hit
                continue
            # first strong fuzzy CFC match, in wiki rank order
            best_idx = None
            best_score = 0.0
            for cfc_name, cfc_idx in index.items():
                if grade is not None and cards[cfc_idx].get("grade") != grade:
                    continue
                sc = _fuzzy_score(title, cfc_name)
                if sc > best_score:
                    best_score = sc
                    best_idx = cfc_idx
            if best_idx is not None and best_score >= 90:
                return best_idx

    # effect-text fallback: fetch the wiki page and match abilities
    if first_title:
        meta = _wiki_page_meta(first_title)
        if meta and meta.get("effect"):
            best_idx = None
            best_score = 0.0
            for cidx, card in _load_cfc_cards().items():
                if grade is not None and card.get("grade") != grade:
                    continue
                if meta.get("grade") is not None and card.get("grade") != meta["grade"]:
                    continue
                if meta.get("nations") and card.get("nation"):
                    if not set(meta["nations"]) & set(card["nation"]):
                        continue
                c_effect = _clean_effect(card.get("effect") or "")
                if not c_effect:
                    continue
                sc = _fuzzy_score(meta["effect"], c_effect)
                if sc > best_score:
                    best_score = sc
                    best_idx = cidx
            if best_idx is not None and best_score >= 80:
                return best_idx
    return None


def jp_to_cfc_index(jp_name: str, grade: int | None = None) -> int | None:
    """Map a decklog Japanese card name to a CFC index (cached).

    Only successful resolutions are cached; failures are re-attempted on the
    next run so cache-warming with an older resolver can't wedge a card.
    """
    global _name_cache
    if _name_cache is None:
        _name_cache = _load_cache(NAME_CACHE_FILE) or {}
    key = jp_name.strip()
    if grade is not None:
        key = f"{key}||g{grade}"
    if key in _name_cache:
        return _name_cache[key]
    idx = _lookup_cfc_index(jp_name.strip(), grade)
    if idx is not None:
        _name_cache[key] = idx
        _save_cache(NAME_CACHE_FILE, _name_cache)
    return idx


# ------------------------------------------------------------- decklog API

def _decklog_session(code: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://decklog.bushiroad.com/view/{code}",
        "Origin": "https://decklog.bushiroad.com",
        "Accept": "application/json, text/plain, */*",
    })
    return s


def fetch_decklog(code: str) -> dict | None:
    """Full deck JSON from the JP decklog API, or None if invalid."""
    code = re.sub(r"[^A-Za-z0-9]", "", code).upper()
    if not code:
        return None
    s = _decklog_session(code)
    try:
        resp = s.post(
            f"{DECKLOG_API}/view/{code}",
            json={},
            timeout=30,
        )
        resp.raise_for_status()
        deck = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(deck, dict) or not deck.get("list"):
        return None
    return deck


def _sort_cards(deck: dict, cards: list[dict]) -> list[str]:
    """Order card_numbers the way decklog's UI (and the game) does."""
    s = _decklog_session(str(deck.get("id", "")))
    payload = {
        "deck_param1": deck.get("deck_param1"),
        "deck_param2": deck.get("deck_param2"),
        "no": [c["card_number"] for c in cards],
        "sub_no": [c["card_number"] for c in (deck.get("sub_list") or [])],
    }
    try:
        resp = s.post(
            f"{DECKLOG_API}/sort/{deck.get('game_title_id')}",
            json=payload,
            timeout=30,
        )
        data = resp.json()
        return data.get("list", []) if isinstance(data, dict) else []
    except (requests.RequestException, ValueError):
        return []


def _sort_sub_cards(deck: dict) -> list[str]:
    """Order of sub_list card_numbers (stride/G zone) per the sort API."""
    s = _decklog_session(str(deck.get("id", "")))
    payload = {
        "deck_param1": deck.get("deck_param1"),
        "deck_param2": deck.get("deck_param2"),
        "no": [c["card_number"] for c in deck.get("list", [])],
        "sub_no": [c["card_number"] for c in (deck.get("sub_list") or [])],
    }
    try:
        resp = s.post(
            f"{DECKLOG_API}/sort/{deck.get('game_title_id')}",
            json=payload,
            timeout=30,
        )
        data = resp.json()
        if not isinstance(data, dict):
            return []
        return data.get("sub_list", [])
    except (requests.RequestException, ValueError):
        return []


# ------------------------------------------------------------- conversion

def _collab_trigger_gift(jp_name: str) -> str | None:
    """Trigger type for a collab/promo card missing from the CFC database.

    Checks the known-missing names first, then asks the wiki for a card page
    with a ``trig`` field. Returns the CFC gift string ("Heal"/"Critical"/
    "Draw"/"Front") or None if the card is not a trigger.
    """
    known = COLLAB_TRIGGER_GIFTS.get(jp_name.strip())
    if known:
        return known
    for query in _wiki_queries(jp_name):
        for title in _wiki_opensearch(query):
            title = re.sub(r"\s*\((?:card|unit|EN|JP)\)\s*$", "", title).strip()
            if title.startswith("Card Gallery:"):
                continue
            meta = _wiki_page_meta(title)
            if meta and meta.get("trig"):
                return meta["trig"]
    return None


def _nationless_trigger_candidates(gift: str) -> list[int]:
    """CFC indices of same-gift Nationless triggers, preferred first.

    The canonical Elemental of that type leads the list; matching nationless
    triggers (covers reskins and other Elemental-family cards) follow, so a
    deck that already runs 4x the Elemental can still be built legally.
    """
    cards = _load_cfc_cards()
    cands = []
    elem = ELEMENTAL_TRIGGER_INDEX.get(gift)
    if elem is not None:
        cands.append(elem)
    for idx, card in sorted(cards.items()):
        if idx in cands:
            continue
        if card.get("type") != "Trigger Unit":
            continue
        if card.get("gift") != gift:
            continue
        if "Nationless" not in (card.get("nation") or []):
            continue
        cands.append(idx)
    return cands


def _substitute_trigger(
    gift: str,
    count: int,
    main_deck: list[int],
    counts: dict[int, int],
) -> bool:
    """Place ``count`` copies of a missing collab trigger into ``main_deck``
    using same-gift Nationless triggers, never exceeding 4 copies of one card
    (``counts`` tracks copies already in the deck). Returns True if all copies
    were placed.
    """
    for _ in range(count):
        placed = None
        for cand in _nationless_trigger_candidates(gift):
            if counts.get(cand, 0) < CARD_MAX_COPIES:
                placed = cand
                break
        if placed is None:
            return False
        main_deck.append(placed)
        counts[placed] = counts.get(placed, 0) + 1
    return True


def convert_decklog(code: str) -> dict:
    """Produce the CFC deck JSON for a decklog code.

    Raises ValueError on any conversion problem (unknown card, etc.).
    """
    deck = fetch_decklog(code)
    if deck is None:
        raise ValueError(f"Decklog code {code!r} not found on the JP host.")

    if deck.get("game_title_id") != 1:
        raise ValueError("Decklog entry is not a Cardfight Vanguard deck.")

    regulation = REGULATION_MAP.get(deck.get("deck_param1"))
    if regulation is None:
        regulation = 0

    # main deck, in display order
    ordered = _sort_cards(deck, deck.get("list", []))
    by_number = {c["card_number"]: c for c in deck.get("list", [])}
    ordered_cards = [by_number[n] for n in ordered if n in by_number]
    if not ordered_cards:
        ordered_cards = deck.get("list", [])

    main_deck = []
    counts: dict[int, int] = {}
    unresolved = []
    collab_triggers = []  # (name, num) placed after native cards are counted
    for card in ordered_cards:
        cidx = jp_to_cfc_index(card["name"])
        num = int(card["num"])
        if cidx is not None:
            main_deck.extend([cidx] * num)
            counts[cidx] = counts.get(cidx, 0) + num
        else:
            collab_triggers.append((card["name"], num))

    for name, num in collab_triggers:
        gift = _collab_trigger_gift(name)
        if gift is not None and _substitute_trigger(gift, num, main_deck, counts):
            continue
        unresolved.append(name)

    # ride deck: p_list grade_0..grade_3 units in grade order
    ride_cards = {}
    for card in deck.get("p_list", []):
        slot = card.get("slot") or ""
        if re.match(r"grade_[0-4]$", slot):
            ride_cards[slot] = card
    ride_deck = []
    for slot in ("grade_0", "grade_1", "grade_2", "grade_3"):
        card = ride_cards.get(slot)
        if card is not None:
            grade = int(slot.split("_")[1])
            cidx = jp_to_cfc_index(card["name"], grade=grade)
            if cidx is None:
                unresolved.append(card["name"])
            else:
                ride_deck.append(cidx)

    # stride deck: sub_list is the stride/G zone, in the site's display order
    sub_cards = deck.get("sub_list") or []
    stride_deck = []
    if sub_cards:
        sub_by_number = {c["card_number"]: c for c in sub_cards}
        ordered_sub = [n for n in _sort_sub_cards(deck) if n in sub_by_number]
        if not ordered_sub:
            ordered_sub = [c["card_number"] for c in sub_cards]
        for n in ordered_sub:
            card = sub_by_number[n]
            cidx = jp_to_cfc_index(card["name"], grade=4)
            if cidx is None:
                unresolved.append(card["name"])
            else:
                stride_deck.extend([cidx] * int(card.get("num") or 1))

    if unresolved:
        raise ValueError(
            "Could not resolve card(s) to Cardfight Connect indices: "
            + ", ".join(sorted(set(unresolved)))
        )

    has_ride = bool(ride_deck)
    ride_crest = 1 if regulation == 0 else 0

    return {
        "regulation": regulation,
        "hasRide": has_ride,
        "rideCrest": ride_crest,
        "mainDeck": main_deck,
        "rideDeck": ride_deck,
        "strideDeck": stride_deck,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python decklog_converter.py <DECKLOG_CODE>")
        sys.exit(1)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    code = sys.argv[1]
    try:
        result = convert_decklog(code)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
