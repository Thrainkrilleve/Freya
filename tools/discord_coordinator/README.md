# Freya Discord dual coordinator (development preview)

Two **separate Discord applications**, run as two separate processes. No changes to Project RUO. The bot is **not deployed** and has no access to Freya's memory or GitHub write permissions.

## Local setup (when owner is ready)
Python 3.12. Create an isolated virtual environment, install `tools/discord_coordinator/requirements-discord.txt`, then configure locally:
- `FREYA_DISCORD_GUILD_ID`: one authorized Discord server ID
- `FREYA_DISCORD_CHANNEL_IDS`: comma-separated authorized channel IDs
- `FREYA_DISCORD_OWNER_IDS`: comma-separated owner IDs (reserved for future approval features)
- `FREYA_DISCORD_OPENAI_TOKEN`, `FREYA_DISCORD_CLAUDE_TOKEN`: separate bot tokens
- `FREYA_OPENAI_API_KEY`, `FREYA_ANTHROPIC_API_KEY`: separately billed provider API keys
- `FREYA_OPENAI_MODEL`, `FREYA_CLAUDE_MODEL`: valid API model identifiers chosen for the accounts
- optional `FREYA_DISCORD_CONTEXT`: path to reviewed project context

Create each Discord application with the `bot` scope and permissions View Channels, Send Messages, Read Message History. Enable Message Content Intent in each application's Bot settings. Avoid Administrator permission. Do not paste secrets in GitHub, Discord, or chat.

From repository root, start in separate terminals:
```
python -m tools.discord_coordinator.bot --role openai
python -m tools.discord_coordinator.bot --role claude
```
Mention one bot to ask a question; type `!review <proposal>` in an authorized channel to request independent responses from both. Bot-authored messages are ignored, so no automated bot-to-bot loops. No slash commands, durable memory, GitHub integration, or unattended hosting yet.

## Provenance
Adapted design principles from Project RUO's `discord-readonly-bot` branch: snowflake validation, explicit guild/channel allowlisting, secret hygiene, and fail-closed behavior. RUO's attachment collector, supplier DB, and read-only permissions are intentionally not copied. The Freya bot uses a Gateway library to receive messages and post replies, which RUO does not.
