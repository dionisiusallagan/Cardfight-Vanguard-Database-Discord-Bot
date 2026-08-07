"""Cardfight!! Vanguard card data lookup via the public MediaWiki API."""

import difflib
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup, NavigableString

try:
    from rapidfuzz import fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    fuzz = None
    RAPIDFUZZ_AVAILABLE = False

API_URL = "https://cardfight.fandom.com"
WIKI_BASE = "https://cardfight.fandom.com"
YUYUTEI_SEARCH_URL = "https://yuyu-tei.jp/sell/vg/s/search"
USER_AGENT = "VanguardCardBot/1.0 (contact: your-email)"
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "card_cache.json")
CACHE_TTL = 7 * 24 * 60 * 60  # 7 days in seconds
CACHE_VERSION = 3  # bump when the fetched fields change, to invalidate stale entries
PRICE_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "price_cache.json"
)
PRICE_CACHE_TTL = 12 * 60 * 60  # 12 hours in seconds
PRICE_CACHE_VERSION = 2  # bump when the parser changes, to invalidate stale entries

_cache = {}
_price_cache = {}


def _load_cache():
    global _cache
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        # Schema bump: drop entries from before image_url/jp_name/version.
        _cache = {
            k: v
            for k, v in loaded.items()
            if "image_url" in v
            and "jp_name" in v
            and v.get("version") == CACHE_VERSION
        }
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


def _load_price_cache():
    global _price_cache
    try:
        with open(PRICE_CACHE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        # Schema bump: drop entries cached by older parser versions.
        _price_cache = {
            k: v
            for k, v in loaded.items()
            if v.get("version") == PRICE_CACHE_VERSION and "listings" in v
        }
    except (OSError, ValueError):
        _price_cache = {}


def _save_price_cache():
    try:
        with open(PRICE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_price_cache, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _is_price_cache_fresh(entry):
    return time.time() - entry.get("timestamp", 0) < PRICE_CACHE_TTL


_load_cache()
_load_price_cache()


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
            result["version"] = CACHE_VERSION
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


def _parse_int_text(tag) -> int:
    if tag is None:
        return 0
    digits = re.sub(r"[^\d]", "", "".join(tag.stripped_strings))
    return int(digits) if digits else 0


def _parse_stock(tag) -> int | None:
    if tag is None:
        return None
    text = "".join(tag.stripped_strings)
    digits = re.sub(r"[^\d]", "", text)
    if digits:
        return int(digits)
    # Explicit out-of-stock markers (× ✕ ✗) mean 0 stock.
    if re.search(r"[×✕✗]", text):
        return 0
    # Otherwise stock status isn't expressed numerically (e.g. ◯ "in stock").
    return None


def _product_to_dict(card, rarity) -> dict | None:
    set_span = card.find(
        "span",
        class_=lambda c: bool(c) and "border-dark" in c and "text-center" in c,
    )
    if set_span is None:
        return None
    set_code = "".join(set_span.stripped_strings).strip()
    if not set_code:
        return None
    return {
        "rarity": rarity,
        "set_code": set_code,
        "price_yen": _parse_int_text(card.find("strong")),
        "stock": _parse_stock(
            card.find(class_=lambda c: bool(c) and "cart_sell_zaiko" in c)
        ),
    }


def _extract_rarity(set_code: str, alt: str | None) -> str | None:
    # Yuyu-tei alt text looks like "D-BT11/FFR03 FFR ドラグリッター ...":
    # second whitespace token is the rarity code.
    if alt:
        parts = alt.split()
        if len(parts) >= 2 and parts[0] == set_code and re.fullmatch(r"[A-Z]+", parts[1]):
            return parts[1]
    # Fallback: strip trailing digits from the last slash segment, e.g. FFR03 -> FFR.
    segment = set_code.rsplit("/", 1)[-1]
    match = re.match(r"([A-Z]+)\d*$", segment)
    if match:
        return match.group(1)
    return None


def _section_rarity(heading) -> str | None:
    # Banner is <h3>...<span>SR</span> Card List</h3>: rarity lives in the span.
    badge = heading.find("span")
    if badge is not None:
        rarity = "".join(badge.stripped_strings).strip()
        if rarity:
            return rarity
    text = heading.get_text(" ", strip=True)
    if " Card List" in text:
        return text.split(" Card List", 1)[0].strip()
    return None


def _fetch_yuyutei(search_word: str) -> list[dict]:
    resp = requests.get(
        YUYUTEI_SEARCH_URL,
        params={"search_word": search_word},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    # Each rarity is a <div class="... cards-list ..."> section whose <h3>
    # banner carries the rarity code (e.g. "SR Card List"), followed by the
    # product cards for that rarity.
    sections = soup.find_all(
        "div",
        class_=lambda c: bool(c) and "cards-list" in c and "py-4" in c,
    )
    if sections:
        for section in sections:
            heading = section.find("h3")
            rarity = _section_rarity(heading) if heading is not None else None
            for card in section.find_all(class_="card-product"):
                entry = _product_to_dict(card, rarity)
                if entry is not None:
                    results.append(entry)
    else:
        # Fallback for pages that render products without the section layout.
        for card in soup.find_all(class_="card-product"):
            set_span = card.find(
                "span",
                class_=lambda c: bool(c) and "border-dark" in c and "text-center" in c,
            )
            if set_span is None:
                continue
            set_code = "".join(set_span.stripped_strings).strip()
            if not set_code:
                continue
            img = card.find("img", class_="card")
            entry = _product_to_dict(
                card, _extract_rarity(set_code, img.get("alt") if img else None)
            )
            if entry is not None:
                results.append(entry)

    results.sort(key=lambda x: x["price_yen"], reverse=True)
    return results


def get_yuyutei_prices(jp_name: str) -> list[dict]:
    if jp_name in _price_cache and _is_price_cache_fresh(_price_cache[jp_name]):
        return _price_cache[jp_name]["listings"]

    results = _fetch_yuyutei(jp_name)
    if not results:
        # Some wiki kana fields include a title prefix Yuyu-tei spells
        # differently (e.g. じゅんかんのフレデュール vs 春歓のフレデュール),
        # so retry with just the base card name.
        base_name = jp_name.split()[-1] if jp_name.split() else jp_name
        if base_name and base_name != jp_name:
            results = _fetch_yuyutei(base_name)

    _price_cache[jp_name] = {
        "listings": results,
        "timestamp": time.time(),
        "version": PRICE_CACHE_VERSION,
    }
    _save_price_cache()
    return results


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


def _collect_header_text(node) -> str:
    # Build text from the header's Japanese portion, dropping ruby <rt>
    # annotations so we keep the kanji base (rb), not just the reading.
    if node.name in ("rt", "small"):
        return ""
    if isinstance(node, NavigableString):
        return str(node)
    return "".join(_collect_header_text(child) for child in node.children)


def _header_japanese_name(header) -> str | None:
    # Header layout: "English Title<br/>Japanese Name" (kanji via ruby when
    # present, e.g. 春歓のフレデュール, otherwise kana).
    after = []
    seen_br = False
    for child in header.children:
        if getattr(child, "name", None) == "br":
            seen_br = True
            continue
        if seen_br:
            after.append(_collect_header_text(child))
    return "".join(after).strip() or None


def extract_japanese_name(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    # Prefer the actual Japanese name shown next to the English title.
    header = soup.find(class_="header")
    if header is not None:
        name = _header_japanese_name(header)
        if name:
            return name
        # Header without a <br/> split: fall back to any kana run.
        text = "".join(header.stripped_strings)
        match = re.search(r"[\u3040-\u30ff][\u3040-\u30ff\s]*", text)
        if match:
            candidate = match.group(0).strip()
            if candidate:
                return candidate
    # Fallback: labeled "Kana" field in the infobox table.
    for td in soup.find_all("td"):
        if "".join(td.stripped_strings) == "Kana":
            next_td = td.find_next("td")
            if next_td is not None:
                kana = "".join(next_td.stripped_strings).strip()
                if kana:
                    return kana
    return None


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
        "jp_name": extract_japanese_name(html),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python scraper.py <query>")
        sys.exit(1)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    query = " ".join(sys.argv[1:])
    result = get_card_effect(query)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
