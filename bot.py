"""Discord bot that looks up Cardfight!! Vanguard cards, prices, and decklists."""

import asyncio
import io
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import discord
from discord.ext import commands
from dotenv import load_dotenv

from decklog_converter import convert_decklog
from decklog_scraper import (
    decklog_image_url,
    decklog_view_url,
    get_all_top_plays,
    resolve_top_play,
)
from scraper import (
    get_card_effect,
    get_card_effect_by_title,
    get_yuyutei_prices,
    resolve_top_matches,
)

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise SystemExit(
        "Missing DISCORD_BOT_TOKEN. Add it to a .env file next to bot.py "
        "(e.g. DISCORD_BOT_TOKEN=your-token) and try again."
    )

MAX_LOOKUPS_PER_MESSAGE = 3
MAX_LOOKUP_CHOICES = 1
MAX_CANDIDATES = 5
MAX_PRICE_QUERIES = 1
MAX_DECKLOG_QUERIES = 2
MAX_DECKCFC_QUERIES = 1
MAX_EMBED_DESCRIPTION = 4096
LOOKUP_TIMEOUT = 30.0
TRUNCATION_SUFFIX = "\n… (truncated)"
NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
PAGE_EMOJIS = ("◀️", "▶️")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def build_card_embed(result: dict) -> discord.Embed:
    embed = discord.Embed(
        title=result.get("title", "?"),
        url=result.get("url"),
        description=truncate_reply(result.get("effect", "")),
    )
    image_url = result.get("image_url")
    if image_url:
        embed.set_image(url=image_url)
    return embed


def truncate_reply(text: str) -> str:
    if len(text) <= MAX_EMBED_DESCRIPTION:
        return text
    cutoff = MAX_EMBED_DESCRIPTION - len(TRUNCATION_SUFFIX)
    return text[:cutoff].rstrip() + TRUNCATION_SUFFIX


def format_price_line(item: dict) -> str:
    rarity = item.get("rarity") or "?"
    set_code = item.get("set_code", "")
    price = f"¥{item['price_yen']:,}"
    stock = item.get("stock")
    if stock is None:
        stock_text = "?"
    else:
        stock_text = str(stock)
    return f"**{rarity}** ({set_code}) — {price} (stock: {stock_text})"


def build_price_embed(title: str, listings: list[dict]) -> discord.Embed:
    lines = [format_price_line(item) for item in listings]
    return discord.Embed(title=title, description="\n".join(lines))


async def handle_price(message: discord.Message, query: str):
    async with message.channel.typing():
        matches = resolve_top_matches(query, limit=1)

    if not matches:
        await message.reply(
            f"⚠️ No card found matching '{query}'.",
            mention_author=False,
        )
        return

    title = matches[0]["title"]

    async with message.channel.typing():
        data = get_card_effect_by_title(title)
        jp_name = data.get("jp_name")
        if not jp_name:
            await message.reply(
                f"⚠️ Couldn't find a Japanese name for '{title}' to search prices.",
                mention_author=False,
            )
            return
        listings = get_yuyutei_prices(jp_name)

    if not listings:
        await message.reply(
            f"⚠️ No Yuyu-tei listings found for '{title}'.",
            mention_author=False,
        )
        return

    embed = build_price_embed(title, listings)
    await message.reply(embed=embed, mention_author=False)


async def handle_lookup(message: discord.Message, query: str):
    async with message.channel.typing():
        matches = resolve_top_matches(query, limit=MAX_CANDIDATES)

    if not matches:
        await message.reply(
            f"⚠️ No card found matching '{query}'.",
            mention_author=False,
        )
        return

    lines = [
        f"{NUMBER_EMOJIS[i]} {m['title']}"
        for i, m in enumerate(matches)
    ]
    list_embed = discord.Embed(
        title=f"Cards matching '{query}'",
        description="\n".join(lines)
        + "\n\nReact with a number to see that card's full effect. "
        + f"Expires in {int(LOOKUP_TIMEOUT)}s.",
    )
    sent = await message.reply(embed=list_embed, mention_author=False)

    for i in range(len(matches)):
        await sent.add_reaction(NUMBER_EMOJIS[i])

    allowed = set(NUMBER_EMOJIS[: len(matches)])

    def check(reaction, user):
        return (
            user == message.author
            and user != bot.user
            and reaction.message.id == sent.id
            and str(reaction.emoji) in allowed
        )

    try:
        reaction, _ = await bot.wait_for(
            "reaction_add", timeout=LOOKUP_TIMEOUT, check=check
        )
    except asyncio.TimeoutError:
        list_embed.description += "\n\n*Selection timed out.*"
        await sent.edit(embed=list_embed)
        try:
            await sent.clear_reactions()
        except discord.HTTPException:
            pass
        return

    index = NUMBER_EMOJIS.index(str(reaction.emoji))
    title = matches[index]["title"]
    result = get_card_effect_by_title(title)
    if "error" in result:
        await sent.edit(content=f"⚠️ {result['error']}", embed=None)
    else:
        await sent.edit(embed=build_card_embed(result))
    try:
        await sent.clear_reactions()
    except discord.HTTPException:
        pass


def build_decklog_embed(entry: dict, title: str, page: int, total: int) -> discord.Embed:
    lines = []
    if entry.get("event_name"):
        lines.append(f"**{entry['event_name']}**")
    if entry.get("date"):
        lines.append(entry["date"])
    if entry.get("location"):
        lines.append(entry["location"])
    if entry.get("rank") is not None:
        lines.append(f"Rank {entry['rank']}")
    if entry.get("player"):
        lines.append(entry["player"])
    if entry.get("set_name"):
        lines.append(f"Set: {entry['set_name']}")
    if entry.get("format"):
        lines.append(f"Format: {entry['format']}")
    if entry.get("list_url"):
        lines.append(entry["list_url"])
    embed = discord.Embed(title=title, description="\n".join(lines))
    embed.set_footer(text=f"Top play {page + 1} of {total}")
    return embed


async def handle_decklog(message: discord.Message, query: str, lang: str):
    async with message.channel.typing():
        deck_name = resolve_top_play(query, lang)
    if not deck_name:
        await message.reply(
            f"⚠️ No deck found matching '{query}'.",
            mention_author=False,
        )
        return

    async with message.channel.typing():
        entries = get_all_top_plays(deck_name, lang)
    playable = [e for e in entries if e.get("decklog_code")]
    if not playable:
        await message.reply(
            f"⚠️ No Decklog lists found for '{deck_name}'.",
            mention_author=False,
        )
        return

    label = "Global" if lang == "en" else "JP"
    title = f"{deck_name} ({label})"
    index = 0
    sent = None
    while True:
        entry = playable[index]
        embed = build_decklog_embed(entry, title, index, len(playable))
        image_host = entry.get("decklog_host") or lang
        image_url = decklog_image_url(entry["decklog_code"], image_host)
        if image_url:
            embed.set_image(url=image_url)
        if sent is None:
            sent = await message.reply(embed=embed, mention_author=False)
            if len(playable) > 1:
                for emoji in PAGE_EMOJIS:
                    await sent.add_reaction(emoji)
        else:
            await sent.edit(embed=embed)

        def check(reaction, user):
            return (
                user == message.author
                and user != bot.user
                and reaction.message.id == sent.id
                and str(reaction.emoji) in PAGE_EMOJIS
            )

        try:
            reaction, _ = await bot.wait_for(
                "reaction_add", timeout=LOOKUP_TIMEOUT, check=check
            )
        except asyncio.TimeoutError:
            try:
                await sent.clear_reactions()
            except discord.HTTPException:
                pass
            return

        if str(reaction.emoji) == PAGE_EMOJIS[0]:
            index = (index - 1) % len(playable)
        else:
            index = (index + 1) % len(playable)


async def handle_deck_direct(message: discord.Message, code: str, lang: str):
    code = code.strip()
    async with message.channel.typing():
        image_url = decklog_image_url(code, lang)
    if not image_url:
        await message.reply(
            f"⚠️ No decklist found for code '{code}'.",
            mention_author=False,
        )
        return
    embed = discord.Embed(
        title=f"Decklist {code}",
        url=decklog_view_url(code, lang),
    )
    embed.set_image(url=image_url)
    await message.reply(embed=embed, mention_author=False)


async def handle_deck_cfc(message: discord.Message, code: str):
    code = code.strip()
    async with message.channel.typing():
        try:
            result = convert_decklog(code)
        except ValueError as exc:
            await message.reply(
                f"⚠️ Could not convert decklist: {exc}",
                mention_author=False,
            )
            return
    if result is None:
        await message.reply(
            f"⚠️ No decklist found for code '{code}'.",
            mention_author=False,
        )
        return
    json_text = json.dumps(result, ensure_ascii=False)
    if len(json_text) <= MAX_EMBED_DESCRIPTION:
        await message.reply(
            f"CFC deck JSON for `{code}` (`{len(result['mainDeck'])}` main "
            f"/ `{len(result['rideDeck'])}` ride / "
            f"`{len(result['strideDeck'])}` stride):\n```json\n"
            + json_text
            + "\n```",
            mention_author=False,
        )
    else:
        filename = f"{code}.json"
        data = json_text.encode("utf-8")
        fbuf = discord.File(io.BytesIO(data), filename=filename)
        await message.reply(
            content=f"CFC deck JSON for `{code}`:",
            file=fbuf,
            mention_author=False,
        )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    queries = [
        q.strip()
        for q in re.findall(
            r"<<(?!(?:lookup|price|decklogen|decklogjp|decken|deckjp|deckcfc)\?)(.+?)>>",
            message.content,
        )
        if q.strip()
    ][:MAX_LOOKUPS_PER_MESSAGE]

    lookup_queries = [
        q.strip()
        for q in re.findall(r"<<lookup\?(.+?)>>", message.content)
        if q.strip()
    ][:MAX_LOOKUP_CHOICES]

    price_queries = [
        q.strip()
        for q in re.findall(r"<<price\?(.+?)>>", message.content)
        if q.strip()
    ][:MAX_PRICE_QUERIES]

    decklog_en_queries = [
        q.strip()
        for q in re.findall(r"<<decklogen\?(.+?)>>", message.content)
        if q.strip()
    ][:MAX_DECKLOG_QUERIES]

    decklog_jp_queries = [
        q.strip()
        for q in re.findall(r"<<decklogjp\?(.+?)>>", message.content)
        if q.strip()
    ][:MAX_DECKLOG_QUERIES]

    deck_en_codes = [
        q.strip()
        for q in re.findall(r"<<decken\?(.+?)>>", message.content)
        if q.strip()
    ][:MAX_DECKLOG_QUERIES]

    deck_jp_codes = [
        q.strip()
        for q in re.findall(r"<<deckjp\?(.+?)>>", message.content)
        if q.strip()
    ][:MAX_DECKLOG_QUERIES]

    deck_cfc_codes = [
        q.strip()
        for q in re.findall(r"<<deckcfc\?(.+?)>>", message.content)
        if q.strip()
    ][:MAX_DECKCFC_QUERIES]

    for query in queries:
        async with message.channel.typing():
            result = get_card_effect(query)

        if "error" in result:
            await message.reply(
                f"⚠️ No card found matching '{query}'.",
                mention_author=False,
            )
            continue

        embed = build_card_embed(result)
        await message.reply(embed=embed, mention_author=False)

    for query in lookup_queries:
        await handle_lookup(message, query)

    for query in price_queries:
        await handle_price(message, query)

    for query in decklog_en_queries:
        await handle_decklog(message, query, "en")

    for query in decklog_jp_queries:
        await handle_decklog(message, query, "jp")

    for code in deck_en_codes:
        await handle_deck_direct(message, code, "en")

    for code in deck_jp_codes:
        await handle_deck_direct(message, code, "jp")

    for code in deck_cfc_codes:
        await handle_deck_cfc(message, code)

    await bot.process_commands(message)


HEALTH_HOST = os.getenv("HEALTH_HOST", "0.0.0.0")
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "5000"))


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal liveness endpoint so cloud hosts (e.g. Replit Autoscale) treat
    this process as a web service instead of scaling it to zero."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def _start_health_server() -> threading.Thread:
    server = ThreadingHTTPServer((HEALTH_HOST, HEALTH_PORT), _HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="health-server"
    )
    thread.start()
    return thread


if __name__ == "__main__":
    _start_health_server()
    bot.run(TOKEN)
