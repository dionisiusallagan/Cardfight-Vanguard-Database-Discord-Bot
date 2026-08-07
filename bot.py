"""Discord bot that looks up Cardfight!! Vanguard card effects via scraper.py."""

import asyncio
import os
import re

import discord
from discord.ext import commands
from dotenv import load_dotenv

from scraper import get_card_effect, get_card_effect_by_title, resolve_top_matches

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
MAX_EMBED_DESCRIPTION = 4096
LOOKUP_TIMEOUT = 30.0
TRUNCATION_SUFFIX = "\n… (truncated)"
NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

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


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    queries = [
        q.strip()
        for q in re.findall(r"<<(?!lookup\?)(.+?)>>", message.content)
        if q.strip()
    ][:MAX_LOOKUPS_PER_MESSAGE]

    lookup_queries = [
        q.strip()
        for q in re.findall(r"<<lookup\?(.+?)>>", message.content)
        if q.strip()
    ][:MAX_LOOKUP_CHOICES]

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

    await bot.process_commands(message)


if __name__ == "__main__":
    bot.run(TOKEN)
