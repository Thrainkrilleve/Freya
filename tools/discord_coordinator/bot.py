"""Run once per bot identity: python -m tools.discord_coordinator.bot --role openai|claude."""
import argparse
import asyncio
import os
from pathlib import Path

from .core import Config, allowed, clean_prompt, parse_ids, route, system_prompt
from .providers import ProviderError, generate

def env_required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} locally; never put secrets in Discord or Git")
    return value

async def run(role):
    try:
        import discord
    except ImportError:
        raise SystemExit("Install requirements-discord.txt in a separate environment") from None
    config = Config(role, next(iter(parse_ids(env_required("FREYA_DISCORD_GUILD_ID")))),
                    parse_ids(env_required("FREYA_DISCORD_CHANNEL_IDS")),
                    parse_ids(env_required("FREYA_DISCORD_OWNER_IDS")))
    token = env_required("FREYA_DISCORD_" + role.upper() + "_TOKEN")
    key = env_required("FREYA_" + ("OPENAI" if role == "openai" else "ANTHROPIC") + "_API_KEY")
    model = env_required("FREYA_" + role.upper() + "_MODEL")
    context_path = Path(os.getenv("FREYA_DISCORD_CONTEXT", "tools/discord_coordinator/project_context.md"))
    context = context_path.read_text(encoding="utf-8")
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    lock = asyncio.Semaphore(2)
    last = {}

    @client.event
    async def on_ready():
        print(f"{role} bot connected as {client.user.id}; configured channels: {len(config.channels)}")

    @client.event
    async def on_message(message):
        if not allowed(config, message.guild.id if message.guild else None, message.channel.id, message.author.bot):
            return
        mode = route(message.content, client.user in message.mentions)
        if not mode:
            return
        prompt = clean_prompt(message.content, client.user.id)
        if not prompt:
            return
        # Both bots may respond to !review; mention one bot for a single-model response.
        if mode == "review":
            prompt = "Independently review this proposal. Identify risks, alternatives, and tests. Do not assert approval.\n" + prompt
        now = asyncio.get_running_loop().time()
        user_key = (message.channel.id, message.author.id)
        if now - last.get(user_key, -1e9) < 10:
            return
        last[user_key] = now
        async with lock:
            try:
                async with message.channel.typing():
                    reply = await asyncio.wait_for(generate(role, key, model, system_prompt(role, context), prompt), timeout=55)
            except (ProviderError, asyncio.TimeoutError):
                reply = "The model request failed or timed out. Check the provider account and local logs."
            for offset in range(0, min(len(reply), 7500), 1800):
                await message.reply(reply[offset:offset + 1800], mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    await client.start(token)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True, choices=["openai", "claude"])
    args = parser.parse_args()
    asyncio.run(run(args.role))

if __name__ == "__main__":
    main()
