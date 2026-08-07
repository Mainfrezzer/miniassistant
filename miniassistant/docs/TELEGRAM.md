# Telegram Bot Setup

`chat_clients.telegram` is for the **Telegram bot only** — email config goes under the separate top-level `email:` key, never here.

**Required in config:** `chat_clients.telegram.bot_token`. Include `enabled: true` (optional, default true; use `false` to disable the bot).

**If user has no bot_token yet**, tell them:

1. Open Telegram and message **@BotFather**.
2. Send `/newbot`, pick a display name and a unique username ending in `bot`.
3. BotFather replies with the **bot token** — copy it.
4. Optional but recommended for group rooms: send `/setprivacy` to BotFather, select the bot, choose **Disable**. Without this the bot only sees @mentions and commands in groups — auto-context and `read_recent_messages` stay almost empty.
5. The bot responds in DMs (every message) and in groups when @mentioned (default mode).

**Example config block:**

```yaml
chat_clients:
  telegram:
    enabled: true
    bot_token: "123456789:THEIR_BOT_TOKEN"
```

**After you write config:** remind the user to restart the service.

---

## How it differs from Matrix/Discord (important for tools)

- **No avatar API.** The bot's profile picture canNOT be set via Bot API (no such method — 404). Only the user can set it, manually via @BotFather → `/setuserpic`. Never attempt it with curl or the bot_token; see AVATARS.md.
- **No history API.** Telegram bots cannot fetch old messages. MiniAssistant keeps its own message cache (`telegram_history.json` in the config dir, last ~300 messages per chat, restart-safe). `read_recent_messages` and `search_chat_history` read from this cache — they only see messages received **while the bot was running and privacy mode was disabled**.
- **No chat enumeration.** A chat appears in the WebUI `/rooms` page only after the bot has seen at least one message there.
- **Chat IDs are numeric**, groups are negative (e.g. `-1001234567890`). User ID == chat ID for private chats.
- Message limit is **4096 chars**; longer replies are split automatically (prefer `---` separators).

## Auth & trust

- Unknown users get an auth code; redeem in Web-UI with `/auth telegram CODE`.
- Group trust: if the user who **added the bot to the group** is authed, all group members may use the bot (same as Matrix/Discord inviter trust). The inviter is recorded when the bot is added (persisted in `telegram_inviters.json`). For groups the bot joined before this feature: remove + re-add the bot by an authed user.

## Response modes & group settings

- Per-chat modes (`always` / `mention` / `off`) live in `chat_clients.telegram.chat_modes` and are managed on the `/rooms` WebUI page. Default: DM=always, group=mention.
- Group-mode settings (context, language, tools whitelist, workspace subdir, auto-context, daily limits, model switching) live in `chat_clients.telegram.chat_settings.<chat_id>` — same shape as Matrix `room_settings`. See `GROUP_ROOMS.md`.
- Schedules/webhooks can target a chat: client `telegram` + `channel_id` = the Telegram chat ID.

**Troubleshooting:**
- Bot doesn't see group messages? Privacy mode is still on — `/setprivacy` → Disable in BotFather, then remove and re-add the bot to the group.
- Bot is offline? Check that `miniassistant` service is running and the token in config is correct (`getMe` failure is logged at startup).
- `read_recent_messages` returns nothing? The cache only fills while the bot runs; there is no way to backfill old Telegram history.
