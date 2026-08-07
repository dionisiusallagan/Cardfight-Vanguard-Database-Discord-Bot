"""Cardfight!! Vanguard card data lookup via the public MediaWiki API."""

import difflib
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

try:
    from rapidfuzz import fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    fuzz = None
    RAPIDFUZZ_AVAILABLE = False

API_URL = "https://cardfight.fandom.com"
WIKI_BASE = "https://cardfight.fandom.com"
USER_AGENT = "VanguardCardBot/1.0 (contact: your-email)"
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "card_cache.json")
CACHE_TTL = 7 * 24 * 60 * 60  # 7 days in seconds

_cache = {}


def _load_cache():
    global _cache
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        # Schema bump: drop entries from before image_url was added.
        _cache = {k: v for k, v in loaded.items() if "image_url" in v}
    except (OSError, ValueError):
        _cache = {}


def _save_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _is_cache_fresh(entry):
    return time.time() - entry.get("timestamp", 0) < CACHE_TTL


def _api_get(params):
    resp = requests.get(
        f"{API_URL}/api.php",
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def search_candidate_titles(query: str) -> list[str]:
    data = _api_get(
        {
            "action": "opensearch",
            "search": query,
            "limit": 10,
            "namespace": 0,
            "format": "json",
        }
    )
    if isinstance(data, list) and len(data) > 1:
        return list(data[1])
    return data.get(1, []) if isinstance(data, dict) else []


def _fuzzy_score(a: str, b: str) -> float:
    if RAPIDFUZZ_AVAILABLE:
        return fuzz.WRatio(a.lower(), b.lower())
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100


def pick_best_title(query: str, candidates: list[str]) -> str | None:
    best = None
    best_score = 0.0
    for rank, candidate in enumerate(candidates):
        score = _fuzzy_score(query, candidate)
        # Favor earlier opensearch results on ties (subtract a small tiebreak).
        adjusted = score - (rank * 0.001)
        if adjusted > best_score:
            best_score = adjusted
            best = candidate
    if best is None or best_score < 60:
        return None
    return best


def is_card_page(title: str) -> bool:
    data = _api_get(
        {
            "action": "query",
            "titles": title,
            "prop": "categories",
            "cllimit": 50,
            "format": "json",
        }
    )
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        for cat in page.get("categories", []):
            if cat.get("title") == "Category:Cards":
                return True
    return False


def resolve_top_matches(query: str, limit: int = 5) -> list[dict]:
    candidates = search_candidate_titles(query)
    ranked = []
    for rank, title in enumerate(candidates):
        if not is_card_page(title):
            continue
        score = _fuzzy_score(query, title) - (rank * 0.001)
        ranked.append({"title": title, "score": score})
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:limit]


def get_card_effect_by_title(title: str) -> dict:
    cache_key = title
    if cache_key in _cache and _is_cache_fresh(_cache[cache_key]):
        result = _cache[cache_key]
    else:
        result = _fetch_card_effect(title)
        if "error" not in result:
            result["timestamp"] = time.time()
            _cache[cache_key] = result
            _save_cache()
    return {k: v for k, v in result.items() if k != "timestamp"}


def get_card_effect(query: str) -> dict:
    matches = resolve_top_matches(query, limit=10)
    for match in matches:
        result = get_card_effect_by_title(match["title"])
        if "error" not in result:
            return result
    return {"error": f"No card found matching '{query}'."}


def _extract_effect_text(soup) -> str | None:
    effect_heading = None
    for tag in soup.find_all(["h2", "h3"]):
        text = "".join(tag.stripped_strings).strip()
        if text in ("Card Effect(s)", "Card Effects", "Card Effect"):
            effect_heading = tag
            break

    if effect_heading is not None:
        paragraphs = []
        for sibling in effect_heading.find_all_next(["h2", "h3", "p"]):
            if sibling.name in ("h2", "h3"):
                break
            if sibling.name == "p":
                paragraphs.append(sibling.get_text(" ", strip=True))
        effect = "\n".join(paragraphs).strip()
        if effect:
            return effect

    # Fallback: card pages on this wiki render the effect as a table row
    # whose label cell (th) reads "Card Effect(s)" / "Effect(s)".
    effect_labels = ("Card Effect(s)", "Card Effects", "Card Effect", "Effect(s)")
    for cell in soup.find_all("th"):
        text = "".join(cell.stripped_strings).strip()
        if text in effect_labels:
            next_td = cell.find_next("td")
            if next_td is not None:
                effect = " ".join(next_td.get_text(" ", strip=True).split())
                if effect:
                    return effect
    return None


def _extract_infobox_image(soup):
    img_tag = None
    # Generic Fandom portable infobox.
    aside = soup.find("aside", class_="portable-infobox")
    if aside is not None:
        img_tag = aside.find("img")
    else:
        # This wiki renders card infoboxes as a .cftable (or similar) div.
        container = soup.find(
            class_=lambda c: bool(c) and any(
                token in c for token in ("cftable", "cardtable", "cardinfobox")
            )
        )
        if container is not None:
            img_tag = container.find("img")

    if img_tag is None:
        return None

    url = img_tag.get("data-src") or img_tag.get("src")
    if not url:
        return None

    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = WIKI_BASE + url

    # Prefer full resolution: strip resize params like /scale-to-width-down/300.
    match = re.search(r"/revision/latest/", url)
    if match:
        qindex = url.find("?", match.end())
        query = url[qindex:] if qindex != -1 else ""
        url = url[:match.end()].rstrip("/") + query
    return url


def _fetch_card_effect(title: str) -> dict:
    data = _api_get(
        {
            "action": "parse",
            "page": title,
            "format": "json",
            "prop": "text",
            "redirects": 1,
        }
    )
    html = data.get("parse", {}).get("text", {}).get("*", "")
    if not html:
        return {"error": f"No card found matching '{title}'."}

    soup = BeautifulSoup(html, "html.parser")
    effect = _extract_effect_text(soup)
    if not effect:
        return {"error": f"No effect section found on page '{title}'."}

    return {
        "title": title,
        "url": f"{API_URL}/wiki/{title.replace(' ', '_')}",
        "effect": effect,
        "image_url": _extract_infobox_image(soup),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python scraper.py <query>")
        sys.exit(1)
    query = " ".join(sys.argv[1:])
    _load_cache()
    result = get_card_effect(query)
    if "error" not in result:
        _save_cache()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
