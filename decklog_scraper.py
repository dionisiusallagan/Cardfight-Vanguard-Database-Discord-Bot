"""VG-Paradox + Decklog tournament decklist lookup for the Discord bot.

How this data source works (investigation notes)
------------------------------------------------
- vg-paradox.com's TopDecks pages are thin JS shells: the ranking tables are
  populated client-side by fetching Google Sheets data through the gviz
  endpoint. We query those same sheets directly with `requests` and parse the
  JSON response -- no browser / headless rendering is needed.
- Deck name lists (column A): EN sheet 14wOwDJME7ZFNv2wEtB6ncsh5PI7DOowd
  (gid 1288571172) and JP sheet 16Bgv0YhgCdRExDZKcJm4qKgoEpXmz6kLNydbAsWdAd8
  (gid 721666387).
- Per-deck top-play entries:
    * EN: same EN sheet, gids 1845741216 (paper) + 2095583511 (online),
      filtered with `where C = '<deck name>' and Q IS NOT NULL Order By K desc`.
    * JP: same JP sheet, gids 0 (paper) + 63998654 (online), filtered with
      `where D = '<deck name>' Order By K desc`.
  The filter value is the deck's full name exactly as listed (the site's
  per-deck Tops URL slug is that name with spaces removed, and its <main id>
  is the full name again -- so we filter by the full name).
- The decklist "export image": Decklog renders it server-side, NOT via client
  canvas. A decklog view page exposes it as its og:image meta tag:
      <host>/deckimages/<CODE>.png
  Decklog runs two independent sites whose codes can overlap: decklog.bushiroad
  .com (JP) and decklog-en.bushiroad.com (EN). Each host only serves its own
  codes (403 for the other's), and the same code string can be two DIFFERENT
  decks. The entry's list URL tells us which host the code belongs to, so
  get_decklog_image() takes that host (falling back to the other host for bare
  code lookups) and decklog_image_url() returns a URL on the serving host.
  Missing codes 403 on both hosts. Playwright is intentionally NOT a
  dependency.
"""

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

USER_AGENT = "VanguardCardBot/1.0 (contact: your-email)"
DECKLOG_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "decklog_cache.json"
)
DECKLOG_CACHE_TTL = 24 * 60 * 60  # 24 hours in seconds
DECKLOG_CACHE_VERSION = 3  # bump to invalidate stale deck-name/entry caches
DECKLOG_IMAGE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "decklog_images"
)
DECKLOG_IMAGE_HOSTS = {
    "en": "https://decklog-en.bushiroad.com",
    "jp": "https://decklog.bushiroad.com",
}
DECKLOG_VIEW_BASES = {
    "en": "https://decklog-en.bushiroad.com/view",
    "jp": "https://decklog.bushiroad.com/view",
}


def decklog_image_url(code: str, host: str) -> str | None:
    """Direct image URL if the code resolves to a real decklist, else None.

    Uses whichever decklog host actually served the image, so the URL won't
    403 for Discord's image proxy and matches the deck the entry points at.
    """
    key = (host, code)
    if key not in _decklog_image_bytes:
        get_decklog_image(code, host)
    if not _decklog_image_bytes.get(key):
        return None
    base = _decklog_image_host.get(key) or DECKLOG_IMAGE_HOSTS.get(
        host, DECKLOG_IMAGE_HOSTS["jp"]
    )
    return f"{base}/deckimages/{code}.png"

# gviz spreadsheet endpoints used by vg-paradox.com.
_TOPDECKS_SHEETS = {
    "en": {
        "sheet": "https://docs.google.com/spreadsheets/d/14wOwDJME7ZFNv2wEtB6ncsh5PI7DOowd/gviz/tq?gid=",
        "rank_gid": "1288571172",
        "entry_gids": ["1845741216", "2095583511"],
        "deck_col": "C",
        "cols": "A,B,C,D,E,F,G,H,I,J,K,L,M,Q,N",
        "extra": "and Q IS NOT NULL",
        "list_idx": 4,
        "date_idx": 10,
        "event_idx": 12,
        "set_idx": 13,
        "format_idx": 14,
        "nation_idx": 3,
        "loc_idx": 8,
        "player_idx": 1,
        "rank_idx": 0,
    },
    "jp": {
        "sheet": "https://docs.google.com/spreadsheets/d/16Bgv0YhgCdRExDZKcJm4qKgoEpXmz6kLNydbAsWdAd8/gviz/tq?gid=",
        "rank_gid": "721666387",
        "entry_gids": ["0", "63998654"],
        "deck_col": "D",
        "cols": "A,B,C,D,E,F,G,H,I,J,K,L,M,N",
        "extra": "",
        "list_idx": 5,
        "date_idx": 10,
        "event_idx": 12,
        "set_idx": 13,
        "format_idx": None,
        "nation_idx": 4,
        "loc_idx": 7,
        "player_idx": 2,
        "rank_idx": 0,
    },
}

_decklog_cache = {}
_decklog_image_bytes = {}
_decklog_image_host = {}


def _load_decklog_cache():
    global _decklog_cache
    try:
        with open(DECKLOG_CACHE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        _decklog_cache = {
            k: v
            for k, v in loaded.items()
            if v.get("version") == DECKLOG_CACHE_VERSION
        }
    except (OSError, ValueError):
        _decklog_cache = {}


def _save_decklog_cache():
    try:
        with open(DECKLOG_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_decklog_cache, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _decklog_cache_fresh(entry):
    return time.time() - entry.get("timestamp", 0) < DECKLOG_CACHE_TTL


_load_decklog_cache()


def _gviz(sheet_url: str, query: str) -> dict | None:
    resp = requests.get(
        sheet_url,
        params={"tq": query},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    text = resp.text
    start = text.find("(")
    end = text.rfind(");")
    if start == -1 or end == -1:
        return None
    return json.loads(text[start + 1 : end])


def _fuzzy_score(a: str, b: str) -> float:
    if RAPIDFUZZ_AVAILABLE:
        return fuzz.WRatio(a.lower(), b.lower())
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100


def get_deck_names(lang: str) -> list[str]:
    """Vanguard deck names for a format, from the ranking sheet's column A.

    The sheets also list collaboration decks (e.g. "Fate Rewinder (CoroCoro)",
    "MyGO"), whose Nation column is the collab theme rather than a Standard
    nation; those are still Cardfight Vanguard decks, so every listed deck is
    kept.
    """
    cfg = _TOPDECKS_SHEETS.get(lang)
    if cfg is None:
        return []
    key = f"names:{lang}"
    if key in _decklog_cache and _decklog_cache_fresh(_decklog_cache[key]):
        return _decklog_cache[key]["names"]

    data = _gviz(cfg["sheet"] + cfg["rank_gid"], "Select A Order By A")
    names = []
    if data and "table" in data:
        for row in data["table"]["rows"]:
            cell = (row.get("c") or [None])[0]
            value = (cell or {}).get("v")
            if isinstance(value, str) and value.strip():
                names.append(value.strip())

    _decklog_cache[key] = {
        "names": names,
        "timestamp": time.time(),
        "version": DECKLOG_CACHE_VERSION,
    }
    _save_decklog_cache()
    return names


def resolve_top_play(deck_name: str, lang: str) -> str | None:
    """Best-matching official deck name for a user query (or None)."""
    best = None
    best_score = 0.0
    for name in get_deck_names(lang):
        score = _fuzzy_score(deck_name, name)
        if score > best_score:
            best_score = score
            best = name
    if best is None or best_score < 70:
        return None
    return best


def get_all_top_plays(deck_name: str, lang: str) -> list[dict]:
    """All recorded top plays for a deck (paper + online), newest first.

    Cache keyed by "lang:deck name"; entries only exist for plays that
    include a decklist link (list links that aren't Decklog get no code).
    """
    cfg = _TOPDECKS_SHEETS.get(lang)
    if cfg is None:
        return []
    key = f"{lang}:{deck_name}"
    if key in _decklog_cache and _decklog_cache_fresh(_decklog_cache[key]):
        return _decklog_cache[key]["entries"]

    query = (
        f"Select {cfg['cols']} "
        f"where {cfg['deck_col']} = '{deck_name}' {cfg['extra']} Order By K desc"
    )
    rows = []
    for gid in cfg["entry_gids"]:
        try:
            data = _gviz(cfg["sheet"] + gid, query)
        except (requests.RequestException, ValueError):
            continue
        if data and "table" in data:
            rows.extend(data["table"]["rows"])

    entries = []
    for row in rows:
        cells = row.get("c") or []
        entry = _row_to_entry(cells, deck_name, cfg)
        if entry is not None:
            entries.append(entry)

    _decklog_cache[key] = {
        "entries": entries,
        "timestamp": time.time(),
        "version": DECKLOG_CACHE_VERSION,
    }
    _save_decklog_cache()
    return entries


def _row_to_entry(cells: list, deck_name: str, cfg: dict) -> dict | None:
    def cell_value(index):
        if index >= len(cells) or cells[index] is None:
            return None
        return cells[index].get("v")

    list_url = cell_value(cfg["list_idx"])
    if not isinstance(list_url, str) or not list_url.strip():
        return None
    list_url = list_url.strip()

    date_raw = cell_value(cfg["date_idx"])
    date_cell = cells[cfg["date_idx"]] if cfg["date_idx"] < len(cells) else None
    date = (date_cell or {}).get("f") or (str(date_raw) if date_raw else None)

    code_match = re.search(r"/view/([A-Za-z0-9]+)", list_url)
    host_match = re.search(r"decklog(-en)?\.bushiroad\.com", list_url)
    decklog_host = "jp"
    if host_match is not None and host_match.group(1) == "-en":
        decklog_host = "en"
    format_value = (
        cell_value(cfg["format_idx"]) if cfg["format_idx"] is not None else None
    )
    return {
        "deck": deck_name,
        "rank": cell_value(cfg["rank_idx"]),
        "player": cell_value(cfg["player_idx"]),
        "list_url": list_url,
        "decklog_code": code_match.group(1) if code_match else None,
        "decklog_host": decklog_host,
        "location": cell_value(cfg["loc_idx"]),
        "date": date,
        "event_name": cell_value(cfg["event_idx"]),
        "set_name": cell_value(cfg["set_idx"]),
        "format": format_value,
    }


def decklog_view_url(code: str, lang: str) -> str:
    base = DECKLOG_VIEW_BASES.get(lang, DECKLOG_VIEW_BASES["jp"])
    return f"{base}/{code}"


def get_decklog_image(code: str, host: str) -> bytes | None:
    """The rendered decklist PNG for a code, cached on disk by code+host.

    Decklog runs two independent sites whose codes can overlap: a code created
    on decklog.bushiroad.com (JP) and one created on decklog-en.bushiroad.com
    (EN) may share the same code string but be different decks. Each host only
    serves its own codes (403 for the other's), so `host` tells us where the
    code was made -- taken from the entry's list URL. As a safety net we still
    fall back to the other host, which covers bare-code `/deckjp` lookups.
    """
    key = (host, code)
    if key in _decklog_image_bytes:
        return _decklog_image_bytes[key]

    os.makedirs(DECKLOG_IMAGE_DIR, exist_ok=True)
    path = os.path.join(DECKLOG_IMAGE_DIR, f"{host}_{code}.png")
    if os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                data = f.read()
            _decklog_image_bytes[key] = data
            _decklog_image_host[key] = DECKLOG_IMAGE_HOSTS.get(
                host, DECKLOG_IMAGE_HOSTS["jp"]
            )
            return data
        except OSError:
            pass

    primary = DECKLOG_IMAGE_HOSTS.get(host, DECKLOG_IMAGE_HOSTS["jp"])
    candidates = [primary] + [
        base for base in DECKLOG_IMAGE_HOSTS.values() if base != primary
    ]
    for base in candidates:
        try:
            resp = requests.get(
                f"{base}/deckimages/{code}.png",
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
        except requests.RequestException:
            continue
        # 403 (AWS AccessDenied) = this host doesn't serve the code.
        if resp.status_code != 200 or not resp.content:
            continue
        if not (resp.headers.get("Content-Type", "").startswith("image/")):
            continue
        try:
            with open(path, "wb") as f:
                f.write(resp.content)
        except OSError:
            pass
        _decklog_image_bytes[key] = resp.content
        _decklog_image_host[key] = base
        return resp.content

    _decklog_image_bytes[key] = None
    return None


def main():
    if len(sys.argv) < 2:
        print(
            "Usage:\n"
            "  python decklog_scraper.py resolve <en|jp> <deck query>\n"
            "  python decklog_scraper.py entries <en|jp> <exact deck name>\n"
            "  python decklog_scraper.py image <en|jp> <decklog code>"
        )
        sys.exit(1)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    cmd = sys.argv[1]
    lang = sys.argv[2] if len(sys.argv) > 2 else "en"

    if cmd == "resolve":
        name = resolve_top_play(" ".join(sys.argv[3:]), lang)
        print(name)
    elif cmd == "entries":
        entries = get_all_top_plays(" ".join(sys.argv[3:]), lang)
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    elif cmd == "image":
        code = sys.argv[3] if len(sys.argv) > 3 else ""
        data = get_decklog_image(code, lang)
        print(f"image bytes: {len(data) if data else None}")


if __name__ == "__main__":
    main()
