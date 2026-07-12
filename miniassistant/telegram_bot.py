"""
Telegram-Bot: Long-Polling via Bot API (raw HTTPS, kein Extra-Package), bei Nachricht
(DM oder @-Mention) entweder Auth-Code senden oder (wenn autorisiert) KI-Antwort.
Typing-Indicator, Markdown (Telegram legacy-Markdown mit Plain-Fallback).

Besonderheit vs. Matrix/Discord: die Bot API hat KEINE History-Endpoints — der Bot
sieht nur Nachrichten, die er live empfängt. read_recent_messages/search_chat_history/
Auto-Context laufen daher über einen eigenen Nachrichten-Cache, der in
<config_dir>/telegram_history.json persistiert wird (restart-fest, best-effort).
Für Gruppen-Auto-Context muss der Privacy-Mode des Bots via BotFather deaktiviert
sein (/setprivacy → Disable), sonst sieht der Bot nur Mentions/Commands.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096
_HISTORY_MAX_PER_CHAT = 300
_HISTORY_BODY_MAX = 1500  # gespeicherte Bodies kappen — Cache ist Kontext-Quelle, kein Archiv

# Module-level Referenzen für thread-safe Zugriff von außen (status_update, notify, WebUI)
_api_base: str | None = None  # https://api.telegram.org/bot<token>
_bot_id: int | None = None
_bot_username: str = ""

_lock = threading.Lock()
# chat_id (str) -> list[{sender, display, body, ts, message_id}] älteste→neueste
_history: dict[str, list[dict[str, Any]]] = {}
_history_dirty = False
_history_last_save = 0.0
# chat_id (str) -> {title, type, username, last_seen}
_known_chats: dict[str, dict[str, Any]] = {}
# chat_id (str) -> inviter user_id (str) | None (= bekannt, kein Inviter ermittelbar)
_inviter_cache: dict[str, str | None] = {}


# ---------------------------------------------------------------------------
# Persistenz (History-Cache, bekannte Chats, Inviter)
# ---------------------------------------------------------------------------

def _config_dir_path() -> Path | None:
    try:
        from miniassistant.config import get_config_dir
        return Path(get_config_dir())
    except Exception:
        return None


def _load_json_file(name: str) -> dict[str, Any]:
    d = _config_dir_path()
    if d is None:
        return {}
    p = d / name
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Telegram: %s laden fehlgeschlagen: %s", name, e)
        return {}


def _save_json_file(name: str, data: dict[str, Any]) -> None:
    d = _config_dir_path()
    if d is None:
        return
    try:
        p = d / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        logger.debug("Telegram: %s speichern fehlgeschlagen: %s", name, e)


def _load_state() -> None:
    global _history, _known_chats, _inviter_cache
    with _lock:
        h = _load_json_file("telegram_history.json")
        _history = {str(k): v for k, v in h.items() if isinstance(v, list)}
        _known_chats = {str(k): v for k, v in _load_json_file("telegram_chats.json").items() if isinstance(v, dict)}
        raw_inv = _load_json_file("telegram_inviters.json")
        _inviter_cache = {str(k): (v if isinstance(v, str) else None) for k, v in raw_inv.items()}
    if _history or _known_chats:
        logger.info("Telegram: State geladen (%d Chats mit History, %d bekannte Chats, %d Inviter)",
                    len(_history), len(_known_chats), len(_inviter_cache))


def _save_history_maybe(force: bool = False) -> None:
    """Debounced History-Save (max. alle 3s) — Cache ist best-effort, kein Archiv."""
    global _history_dirty, _history_last_save
    with _lock:
        if not _history_dirty:
            return
        now = time.time()
        if not force and (now - _history_last_save) < 3.0:
            return
        snapshot = {k: list(v) for k, v in _history.items()}
        _history_dirty = False
        _history_last_save = now
    _save_json_file("telegram_history.json", snapshot)


def _record_message(chat_id: str, sender: str, display: str, body: str,
                    ts_ms: int, message_id: str) -> None:
    global _history_dirty
    body = (body or "").strip()
    if not body:
        return
    if len(body) > _HISTORY_BODY_MAX:
        body = body[:_HISTORY_BODY_MAX] + "…"
    entry = {"sender": sender, "display": display, "body": body, "ts": ts_ms, "message_id": str(message_id)}
    with _lock:
        items = _history.setdefault(str(chat_id), [])
        items.append(entry)
        if len(items) > _HISTORY_MAX_PER_CHAT:
            del items[: len(items) - _HISTORY_MAX_PER_CHAT]
        _history_dirty = True
    _save_history_maybe()


def _record_chat(chat: dict[str, Any]) -> None:
    cid = str(chat.get("id") or "")
    if not cid:
        return
    title = chat.get("title") or ""
    if not title:
        title = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or (chat.get("username") or cid)
    entry = {
        "title": str(title),
        "type": str(chat.get("type") or ""),
        "username": str(chat.get("username") or ""),
        "last_seen": int(time.time()),
    }
    with _lock:
        prev = _known_chats.get(cid)
        _known_chats[cid] = entry
        changed = prev != entry
        snapshot = dict(_known_chats) if changed else None
    if snapshot is not None:
        _save_json_file("telegram_chats.json", snapshot)


def _set_inviter(chat_id: str, inviter_id: str | None) -> None:
    with _lock:
        _inviter_cache[str(chat_id)] = inviter_id
        snapshot = dict(_inviter_cache)
    _save_json_file("telegram_inviters.json", snapshot)


def get_chat_inviter(chat_id: str) -> str | None:
    return _inviter_cache.get(str(chat_id))


# ---------------------------------------------------------------------------
# Sync-API (thread-safe — Bot API ist plain HTTPS, kein laufender Client nötig)
# ---------------------------------------------------------------------------

def _api_url(method: str) -> str | None:
    if not _api_base:
        return None
    return f"{_api_base}/{method}"


def _api_call(method: str, timeout: float = 15, **params: Any) -> dict[str, Any] | None:
    """Sync Bot-API-Call. Gibt result-Dict zurück oder None bei Fehler."""
    url = _api_url(method)
    if not url:
        return None
    try:
        r = httpx.post(url, json=params, timeout=timeout)
        data = r.json()
        if not data.get("ok"):
            logger.debug("Telegram %s fehlgeschlagen: %s", method, data.get("description"))
            return None
        return data.get("result")
    except Exception as e:
        logger.debug("Telegram %s exception: %s", method, e)
        return None


def is_running() -> bool:
    return bool(_api_base)


def send_message_to_chat(chat_id: str, message: str) -> bool:
    """Thread-safe: sendet eine Textnachricht in einen Telegram-Chat.
    Markdown mit Plain-Fallback, Split bei 4096 Zeichen. Recorded in History-Cache."""
    if not _api_base or not message:
        return False
    ok_any = False
    for chunk in _split_message(message, TELEGRAM_MAX_LEN):
        res = _api_call("sendMessage", chat_id=chat_id, text=chunk, parse_mode="Markdown")
        if res is None:
            # Markdown-Parse-Fehler (unbalancierte */_) → plain erneut
            res = _api_call("sendMessage", chat_id=chat_id, text=chunk)
        if res is not None:
            ok_any = True
            _record_message(str(chat_id), _bot_sender_name(), _bot_username or "Bot",
                            chunk, int(time.time() * 1000), str(res.get("message_id") or ""))
    return ok_any


def set_chat_typing(chat_id: str) -> bool:
    """Thread-safe: Typing-Indikator (hält ~5s, Caller refresht bei Bedarf)."""
    return _api_call("sendChatAction", timeout=10, chat_id=chat_id, action="typing") is not None


def leave_chat(chat_id: str) -> tuple[bool, str]:
    """Bot verlässt einen Telegram-Chat (Gruppe). Thread-safe."""
    if not _api_base:
        return False, "Telegram-Bot läuft nicht"
    res = _api_call("leaveChat", chat_id=chat_id)
    if res is None:
        return False, "leaveChat fehlgeschlagen"
    with _lock:
        _known_chats.pop(str(chat_id), None)
        snapshot = dict(_known_chats)
    _save_json_file("telegram_chats.json", snapshot)
    return True, "ok"


def list_chats() -> list[dict[str, Any]]:
    """Bekannte Telegram-Chats (aus Registry — Bot API kann Chats nicht enumerieren).
    Ein Chat erscheint erst, nachdem der Bot dort mindestens eine Nachricht gesehen hat."""
    with _lock:
        items = {k: dict(v) for k, v in _known_chats.items()}
    out: list[dict[str, Any]] = []
    for cid, meta in items.items():
        ctype = meta.get("type") or ""
        out.append({
            "id": cid,
            "name": meta.get("title") or cid,
            "kind": "dm" if ctype == "private" else "group",
            "type": ctype,
            "username": meta.get("username") or "",
            "inviter": _inviter_cache.get(cid),
        })
    out.sort(key=lambda c: (c["kind"] != "dm", str(c["name"]).lower()))
    return out


def fetch_recent_messages(chat_id: str, limit: int = 20, skip_message_id: str | None = None) -> list[dict[str, Any]]:
    """Letzte `limit` Nachrichten aus dem History-Cache (älteste→neueste).
    Cache sieht nur, was der Bot live empfangen hat (Privacy-Mode beachten)."""
    if limit <= 0:
        return []
    limit = min(limit, 100)
    with _lock:
        items = list(_history.get(str(chat_id)) or [])
    if skip_message_id:
        items = [m for m in items if str(m.get("message_id")) != str(skip_message_id)]
    return items[-limit:]


def search_chat_history(chat_id: str, query: str, max_scan: int = 200, context_lines: int = 2) -> dict[str, Any]:
    """Sucht im History-Cache nach `query` (case-insensitive substring; mit `/.../` regex).
    Gleiches Ergebnisformat wie discord_bot.search_chat_history."""
    if not query:
        return {"hits": [], "scanned": 0, "query": query, "diagnostic": "empty query"}
    max_scan = max(10, min(int(max_scan), 500))
    context_lines = max(0, min(int(context_lines), 5))

    import re as _re
    is_regex = len(query) >= 3 and query.startswith("/") and query.endswith("/")
    pat = None
    if is_regex:
        try:
            pat = _re.compile(query[1:-1], _re.IGNORECASE)
        except _re.error:
            return {"hits": [], "scanned": 0, "query": query, "diagnostic": "invalid regex"}
    q_lower = query.lower() if not is_regex else None

    with _lock:
        all_messages = list(_history.get(str(chat_id)) or [])
    all_messages = all_messages[-max_scan:]

    def _match(body: str) -> bool:
        if not body:
            return False
        if pat is not None:
            return bool(pat.search(body))
        return q_lower in body.lower()

    hit_indices = [i for i, m in enumerate(all_messages) if _match(m.get("body") or "")]
    included: set[int] = set()
    for i in hit_indices:
        for j in range(max(0, i - context_lines), min(len(all_messages), i + context_lines + 1)):
            included.add(j)
    hits = []
    for i in sorted(included):
        m = dict(all_messages[i])
        m["is_hit"] = i in hit_indices
        hits.append(m)
    if len(hits) > 40:
        hits = hits[:40]
    diag = None
    if not all_messages:
        diag = "no cached messages for this chat (Telegram bots cannot fetch history; cache fills as messages arrive)"
    return {"hits": hits, "match_count": len(hit_indices), "scanned": len(all_messages), "query": query, "diagnostic": diag}


def get_user_profile(user_id: str, save_dir: str, chat_id: str | None = None) -> dict[str, Any]:
    """Holt display_name + Profilbild eines Telegram-Nutzers. Avatar → save_dir/<id>.jpg.
    chat_id: wenn gesetzt, Membership-Gate via getChatMember — Profile von
    Nicht-Mitgliedern werden NICHT aufgelöst (kein globaler Lookup im Group-Mode)."""
    if not _api_base:
        return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": "telegram bot not running"}
    uid = str(user_id or "").strip()
    if not uid.lstrip("-").isdigit():
        return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": "invalid telegram user_id (expected numeric)"}

    display = ""
    if chat_id:
        member = _api_call("getChatMember", chat_id=chat_id, user_id=int(uid))
        if not member:
            return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": "No row found: user is not in this chat"}
        u = member.get("user") or {}
        display = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or (u.get("username") or uid)
    else:
        # Ohne Chat-Kontext gibt es keinen User-Lookup in der Bot API — nur Avatar-Versuch.
        display = uid

    out: dict[str, Any] = {"display_name": str(display), "avatar_path": "", "avatar_url": ""}
    photos = _api_call("getUserProfilePhotos", user_id=int(uid), limit=1)
    sizes = ((photos or {}).get("photos") or [[]])
    if not sizes or not sizes[0]:
        return out
    file_id = (sizes[0][-1] or {}).get("file_id")  # größte Variante
    if not file_id:
        return out
    finfo = _api_call("getFile", file_id=file_id)
    fpath = (finfo or {}).get("file_path")
    if not fpath:
        return out
    token_part = _api_base.rsplit("/bot", 1)[-1] if _api_base else ""
    url = f"https://api.telegram.org/file/bot{token_part}/{fpath}"
    try:
        r = httpx.get(url, timeout=20)
        r.raise_for_status()
        ext = ".jpg"
        if fpath.lower().endswith(".png"):
            ext = ".png"
        elif fpath.lower().endswith(".webp"):
            ext = ".webp"
        p = Path(save_dir) / f"{uid}{ext}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
        out["avatar_path"] = str(p)
    except Exception as e:
        out["error"] = f"avatar download failed: {e}"
    return out


def _bot_sender_name() -> str:
    return f"@{_bot_username}" if _bot_username else "bot"


def _split_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Teilt eine lange Nachricht in Chunks. Bevorzugt: --- Trenner, dann Zeilenumbruch, dann Leerzeichen."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n---", 0, max_len)
        if split_at > 0:
            chunks.append(text[:split_at].rstrip())
            text = text[split_at:].lstrip("\n-").lstrip()
            continue
        split_at = text.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = text.rfind(" ", 0, max_len)
        if split_at < max_len // 4:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# ---------------------------------------------------------------------------
# Chat-Response (gleiches Muster wie discord_bot._get_chat_response)
# ---------------------------------------------------------------------------

def _get_chat_response(
    config: dict[str, Any],
    tg_user_id: str,
    user_message: str,
    sessions: dict[str, Any],
    images: list[dict[str, Any]] | None = None,
    chat_id: str | None = None,
    is_group: bool = False,
    user_display: str = "",
) -> str:
    """Synchroner Aufruf: Session per (chat_id, tg_user_id), handle_user_input.
    Gibt ausschließlich den sichtbaren Content zurück – KEIN Thinking."""
    # Pro Turn shallow-copy des Config-Dicts: verhindert Race auf config["_chat_context"]
    # zwischen parallelen Triggern (gleicher Grund wie discord_bot).
    config = dict(config)
    from miniassistant.chat_loop import create_session, handle_user_input, is_chat_command
    from miniassistant.slot_cache import derive_conv_id as _sc_derive
    from miniassistant.group_rooms import (
        get_room_settings, build_group_chat_context, session_key as _sess_key,
        ensure_default_group_settings, get_auto_context_settings, format_auto_context,
        wrap_current_message,
    )
    if chat_id and is_group:
        ensure_default_group_settings(config, "telegram", chat_id, is_group=True)
    rs = get_room_settings(config, "telegram", chat_id)
    _sc_conv_id = _sc_derive("telegram", channel_id=chat_id, user_id=tg_user_id) if chat_id else None
    base_ctx: dict[str, Any] = {"platform": "telegram", "channel_id": chat_id, "user_id": tg_user_id}
    if user_display:
        base_ctx["user_display"] = user_display
    if _sc_conv_id:
        base_ctx["conv_id"] = _sc_conv_id
        base_ctx["slot_cache_endpoint"] = "telegram"
    ctx = build_group_chat_context(base_ctx, rs) if chat_id else base_ctx
    # Auto-Context im Group-Mode: letzte N Nachrichten vor user_message prependen.
    # NICHT bei Slash-Befehlen (siehe discord_bot: Command-Parser ist ^-verankert).
    if ctx.get("group_mode") and chat_id and not is_chat_command(user_message):
        ac_count, ac_max, ac_age = get_auto_context_settings(rs)
        if ac_count > 0:
            try:
                prev = fetch_recent_messages(chat_id, limit=ac_count + 1)
                if prev and (prev[-1].get("body") or "").strip() == user_message.strip():
                    prev = prev[:-1]
                prev = prev[-ac_count:] if len(prev) > ac_count else prev
                blk = format_auto_context(prev, max_chars=ac_max, bot_sender=_bot_sender_name(), max_age_min=ac_age)
                from miniassistant.room_images import format_block as _ri_block
                _img_blk = _ri_block(config, ctx)
                if blk or _img_blk:
                    _who_now = user_display or tg_user_id
                    user_message = wrap_current_message(blk, _who_now, user_message, images_block=_img_blk)
            except Exception as _ac_err:
                logger.debug("Telegram auto-context failed: %s", _ac_err)
    session_key = _sess_key(chat_id, tg_user_id, bool(ctx.get("group_mode")))
    if chat_id:
        config["_chat_context"] = ctx
    # Group-Mode: stateless — jeder Turn frische Session.
    if ctx.get("group_mode") or session_key not in sessions:
        session = create_session(config, None)
        session["system_prompt"] = (
            session.get("system_prompt", "") +
            "\n\nTelegram: Max 4096 Zeichen/Nachricht. Laengere Antworten mit `---` trennen, werden automatisch aufgeteilt."
        )
        sessions[session_key] = session
    session = sessions[session_key]
    if chat_id:
        session["chat_context"] = ctx
    result = handle_user_input(session, user_message, allow_new_session=True, images=images)
    if ctx.get("group_mode"):
        sessions.pop(session_key, None)
    else:
        sessions[session_key] = result[1]
    ai_content = result[4] if len(result) > 4 else None
    thinking = result[3] if len(result) > 3 else None
    if ai_content:
        return ai_content.strip()
    if not thinking:
        return (result[0] or "").strip()
    return ""


# ---------------------------------------------------------------------------
# Bot-Loop
# ---------------------------------------------------------------------------

async def run_telegram_bot(config: dict[str, Any]) -> None:
    """Läuft als asyncio-Task: Long-Polling getUpdates, bei Nachricht Auth-Code oder KI."""
    global _api_base, _bot_id, _bot_username

    tg_cfg = (config.get("chat_clients") or {}).get("telegram")
    if not tg_cfg or not tg_cfg.get("bot_token"):
        return
    if not tg_cfg.get("enabled", True):
        logger.info("Telegram-Bot deaktiviert (telegram.enabled: false).")
        return

    bot_token = tg_cfg["bot_token"]
    api_base = f"https://api.telegram.org/bot{bot_token}"

    from miniassistant.chat_auth import get_or_generate_code, is_authorized
    from miniassistant.chat_loop import SessionLRU

    _load_state()

    def _is_trusted(sender_id: str, chat_id: str, is_group: bool, config_dir: str | None) -> tuple[bool, str | None]:
        """(trusted, via_inviter). True wenn User selbst authed ODER Gruppen-Inviter authed."""
        if is_authorized("telegram", sender_id, config_dir):
            return True, None
        if not is_group:
            return False, None
        inviter = _inviter_cache.get(str(chat_id))
        if inviter and is_authorized("telegram", inviter, config_dir):
            return True, inviter
        return False, None

    tg_sessions: Any = SessionLRU(max_size=200)
    _pending_images: dict[str, list[dict[str, Any]]] = {}

    async with httpx.AsyncClient(timeout=httpx.Timeout(65, connect=15)) as client:

        async def _api(method: str, **params: Any) -> Any:
            try:
                r = await client.post(f"{api_base}/{method}", json=params)
                data = r.json()
                if not data.get("ok"):
                    logger.debug("Telegram %s: %s", method, data.get("description"))
                    return None
                return data.get("result")
            except Exception as e:
                logger.debug("Telegram %s exception: %s", method, e)
                return None

        async def _download_file(file_id: str) -> bytes | None:
            finfo = await _api("getFile", file_id=file_id)
            fpath = (finfo or {}).get("file_path")
            if not fpath:
                return None
            try:
                r = await client.get(f"https://api.telegram.org/file/bot{bot_token}/{fpath}")
                r.raise_for_status()
                return r.content
            except Exception as e:
                logger.warning("Telegram: File-Download fehlgeschlagen (%s): %s", fpath, e)
                return None

        async def _send(chat_id: str, text: str, reply_to: int | None = None) -> None:
            for chunk in _split_message(text, TELEGRAM_MAX_LEN):
                params: dict[str, Any] = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
                if reply_to is not None:
                    params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
                res = await _api("sendMessage", **params)
                if res is None:
                    params.pop("parse_mode", None)
                    res = await _api("sendMessage", **params)
                if res is not None:
                    _record_message(str(chat_id), _bot_sender_name(), _bot_username or "Bot",
                                    chunk, int(time.time() * 1000), str(res.get("message_id") or ""))
                reply_to = None  # nur erster Chunk als Reply

        me = await _api("getMe")
        if not me:
            logger.error("Telegram: getMe fehlgeschlagen — ungültiges Bot-Token?")
            return
        _bot_id = int(me.get("id"))
        _bot_username = str(me.get("username") or "")
        _api_base = api_base
        logger.info("Telegram-Bot gestartet als @%s (ID: %s)", _bot_username, _bot_id)

        async def _keep_typing(chat_id: str, stop: asyncio.Event) -> None:
            """Typing-Action hält nur ~5s — während Verarbeitung alle 4.5s refreshen."""
            while not stop.is_set():
                await _api("sendChatAction", chat_id=chat_id, action="typing")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=4.5)
                except asyncio.TimeoutError:
                    pass

        async def _handle_message(msg: dict[str, Any]) -> None:
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            chat_type = str(chat.get("type") or "")
            if not chat_id:
                return
            frm = msg.get("from") or {}
            sender_id = str(frm.get("id") or "")
            if not sender_id or (_bot_id is not None and int(frm.get("id") or 0) == _bot_id):
                return
            is_dm = chat_type == "private"
            is_group = chat_type in ("group", "supergroup")
            display = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) \
                or (frm.get("username") or sender_id)
            sender_name = f"@{frm['username']}" if frm.get("username") else sender_id

            _record_chat(chat)

            # Bot wurde in Gruppe hinzugefügt → Adder als Inviter persistieren (Gruppen-Trust).
            for nm in (msg.get("new_chat_members") or []):
                if _bot_id is not None and int(nm.get("id") or 0) == _bot_id:
                    _set_inviter(chat_id, sender_id or None)
                    logger.info("Telegram: zu Chat %s hinzugefügt von User %s", chat_id, sender_id)
                    return

            body = (msg.get("text") or msg.get("caption") or "").strip()

            # History-Cache füttern (auch Nicht-Trigger-Nachrichten in Gruppen — Auto-Context-Quelle)
            _cache_body = body
            _img_marker_names: list[str] = []
            if msg.get("photo"):
                _img_marker_names.append(f"photo_{msg.get('message_id')}.jpg")
            _doc0 = msg.get("document") or {}
            if str(_doc0.get("mime_type") or "").startswith("image/"):
                _img_marker_names.append(_doc0.get("file_name") or f"image_{msg.get('message_id')}")
            if _img_marker_names:
                _marker = " ".join(f"[📷 Bild: {n}]" for n in _img_marker_names)
                _cache_body = f"{_cache_body} {_marker}".strip() if _cache_body else _marker
            ts_ms = int((msg.get("date") or time.time()) * 1000)
            if _cache_body:
                _record_message(chat_id, sender_name, display, _cache_body, ts_ms, str(msg.get("message_id") or ""))

            # Chat-Mode: always / mention / off. Default: DM=always, Gruppe=mention.
            mention_token = f"@{_bot_username}".lower() if _bot_username else ""
            is_mentioned = bool(mention_token and mention_token in body.lower())
            reply_msg = msg.get("reply_to_message") or {}
            reply_from_id = int(((reply_msg.get("from") or {}).get("id")) or 0)
            if not is_mentioned and _bot_id is not None and reply_from_id == _bot_id:
                is_mentioned = True  # Reply auf Bot-Nachricht zählt als Mention
            ch_modes = ((config.get("chat_clients") or {}).get("telegram") or {}).get("chat_modes") or {}
            mode = (ch_modes.get(chat_id) or "").strip().lower()
            if mode not in ("always", "mention", "off"):
                mode = "always" if is_dm else "mention"
            if mode == "off":
                return
            if mode == "mention" and not (is_dm or is_mentioned):
                return
            # Gruppe + always: Reply auf fremde (Nicht-Bot-)Nachricht = Mensch-zu-Mensch → ignorieren.
            if mode == "always" and not is_dm and not is_mentioned and reply_msg and \
                    reply_from_id and (_bot_id is None or reply_from_id != _bot_id):
                logger.info("Telegram: Chat %s mode=always — Reply an anderen User, ignoriert", chat_id)
                return

            # Per-User-Tageslimit in Gruppen
            if not is_dm:
                from miniassistant.group_rooms import check_user_daily_limit as _chk
                _dl = _chk(config, "telegram", chat_id, sender_id)
                if not _dl[0]:
                    logger.info("Telegram: %s über Tageslimit in %s (notify=%s)", sender_id, chat_id, _dl[1])
                    if _dl[1]:
                        await _send(chat_id, "Du hast dein tägliches Nachrichten-Limit in diesem Chat erreicht — morgen geht's weiter.", reply_to=msg.get("message_id"))
                    return
                if _dl[2] is not None:
                    body += (
                        f"\n\n[System notice: this user has {_dl[2]} message(s) left today in this chat "
                        f"(per-user daily limit). Briefly mention this at the end of your reply, in the user's language.]"
                    )

            # Bot-Mention aus Text entfernen; /cmd@botname → /cmd normalisieren
            if _bot_username:
                import re as _re_m
                body = _re_m.sub(rf"@{_re_m.escape(_bot_username)}\b", "", body, flags=_re_m.IGNORECASE).strip()
                body = _re_m.sub(rf"^(/\w+)@{_re_m.escape(_bot_username)}\b", r"\1", body, flags=_re_m.IGNORECASE)

            # Bild-, Audio- und Dokument-Anhänge laden
            msg_images: list[dict[str, Any]] = []
            msg_docs: list[dict[str, Any]] = []
            audio_file_id: str | None = None
            from miniassistant.documents import is_supported as _doc_supported, extract_document as _doc_extract
            _doc_max_chars = int(config.get("doc_max_chars") or 200000)
            _doc_max_pages = int(config.get("doc_max_pages_render") or 10)

            async def _collect_photo(m: dict[str, Any]) -> None:
                sizes = m.get("photo") or []
                if not sizes:
                    return
                fid = (sizes[-1] or {}).get("file_id")  # größte Variante
                if not fid:
                    return
                data = await _download_file(fid)
                if data:
                    import base64 as _b64
                    msg_images.append({"mime_type": "image/jpeg", "data": _b64.b64encode(data).decode("ascii")})

            await _collect_photo(msg)
            doc = msg.get("document") or {}
            if doc:
                mime = str(doc.get("mime_type") or "").lower()
                fname = doc.get("file_name") or ""
                if mime.startswith("image/") or fname.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                    data = await _download_file(doc.get("file_id"))
                    if data:
                        import base64 as _b64
                        msg_images.append({"mime_type": mime if mime.startswith("image/") else "image/png",
                                           "data": _b64.b64encode(data).decode("ascii")})
                elif mime.startswith("audio/"):
                    audio_file_id = doc.get("file_id")
                elif _doc_supported(mime, fname):
                    data = await _download_file(doc.get("file_id"))
                    if data:
                        d = _doc_extract(data, fname, mime, max_chars=_doc_max_chars, max_pages_render=_doc_max_pages)
                        if d.get("error"):
                            await _send(chat_id, f"Dokument `{fname}` konnte nicht gelesen werden: {d['error']}", reply_to=msg.get("message_id"))
                            return
                        msg_docs.append(d)
                        if d.get("images"):
                            msg_images.extend(d["images"])
            if msg.get("voice"):
                audio_file_id = (msg.get("voice") or {}).get("file_id")
            elif msg.get("audio"):
                audio_file_id = (msg.get("audio") or {}).get("file_id")

            # Reply-to-Image: User antwortet auf ältere Nachricht mit Bild → Bild mit anhängen
            if reply_msg:
                await _collect_photo(reply_msg)
                rdoc = reply_msg.get("document") or {}
                if str(rdoc.get("mime_type") or "").startswith("image/"):
                    data = await _download_file(rdoc.get("file_id"))
                    if data:
                        import base64 as _b64
                        msg_images.append({"mime_type": rdoc.get("mime_type"), "data": _b64.b64encode(data).decode("ascii")})

            config_dir = config.get("_config_dir")
            loop = asyncio.get_event_loop()

            # Audio-Nachricht: STT → Agent → TTS
            if audio_file_id:
                from miniassistant.config import get_voice_stt_url, get_voice_tts_url, get_voice_language, get_voice_tts_voice
                trusted, via_inviter = _is_trusted(sender_id, chat_id, is_group, config_dir)
                if not trusted:
                    code = get_or_generate_code("telegram", sender_id, config_dir)
                    await _send(chat_id, f"Nicht freigeschaltet. Auth-Code: **{code}**", reply_to=msg.get("message_id"))
                    return
                if via_inviter:
                    logger.debug("Telegram: User %s trusted via Chat-Inviter %s", sender_id, via_inviter)
                stt_url = get_voice_stt_url(config)
                if not stt_url:
                    await _send(chat_id, "Sprachfunktion nicht konfiguriert (voice.stt.url fehlt).", reply_to=msg.get("message_id"))
                    return
                audio_bytes = await _download_file(audio_file_id)
                if not audio_bytes:
                    await _send(chat_id, "Sprachnachricht konnte nicht geladen werden.", reply_to=msg.get("message_id"))
                    return
                try:
                    from miniassistant import wyoming_client as _wyoming
                    lang = get_voice_language(config)
                    transcript = await loop.run_in_executor(None, lambda: _wyoming.transcribe(audio_bytes, stt_url, language=lang))
                except Exception as e:
                    logger.exception("Telegram Audio: STT fehlgeschlagen")
                    await _send(chat_id, f"Spracherkennung fehlgeschlagen: {e}", reply_to=msg.get("message_id"))
                    return
                if not transcript:
                    await _send(chat_id, "Konnte Sprachnachricht nicht erkennen.", reply_to=msg.get("message_id"))
                    return
                logger.info("Telegram Audio: Transkript von %s: %s", sender_id, transcript[:80])
                stop_typing = asyncio.Event()
                typing_task = asyncio.create_task(_keep_typing(chat_id, stop_typing))
                try:
                    response = await loop.run_in_executor(
                        None,
                        lambda: _get_chat_response(config, sender_id, f"[Voice] {transcript}", tg_sessions,
                                                   chat_id=chat_id, is_group=is_group, user_display=display),
                    )
                except Exception as e:
                    logger.exception("Telegram Audio: Agent fehlgeschlagen")
                    await _send(chat_id, f"Fehler: {e}", reply_to=msg.get("message_id"))
                    return
                finally:
                    stop_typing.set()
                    await typing_task
                if not response:
                    return
                from miniassistant.voice_format import format_for_voice
                voice_text, visual_content = format_for_voice(response)
                tts_url = get_voice_tts_url(config)
                sent_audio = False
                if tts_url and voice_text:
                    try:
                        tts_voice = get_voice_tts_voice(config)
                        wav_bytes = await loop.run_in_executor(None, lambda: _wyoming.synthesize(voice_text, tts_url, voice=tts_voice))
                        files = {"audio": ("response.wav", wav_bytes, "audio/wav")}
                        r = await client.post(f"{api_base}/sendAudio", data={"chat_id": chat_id}, files=files)
                        sent_audio = bool(r.json().get("ok"))
                        if sent_audio:
                            from miniassistant import agent_actions_log as _aal
                            _aal.log_voice_sent(config, chars=len(voice_text), voice=tts_voice or "", bytes_sent=len(wav_bytes))
                    except Exception:
                        logger.exception("Telegram Audio: TTS fehlgeschlagen")
                if not sent_audio and voice_text:
                    await _send(chat_id, voice_text, reply_to=msg.get("message_id"))
                if visual_content:
                    await _send(chat_id, visual_content)
                return

            # Bild ohne Text → Pending speichern, User fragen
            if msg_images and not body and not msg_docs:
                _pending_images.setdefault(sender_id, []).extend(msg_images)
                await _send(chat_id, "Bild empfangen. Was soll ich damit machen?", reply_to=msg.get("message_id"))
                return

            if not body and not msg_images and not msg_docs:
                return

            # /start (Telegram-Standard beim ersten Kontakt) → wie Auth-Flow behandeln
            if body.strip().lower() == "/start":
                trusted, _ = _is_trusted(sender_id, chat_id, is_group, config_dir)
                if trusted:
                    await _send(chat_id, "Hallo! Ich bin bereit — schreib mir einfach.")
                else:
                    code = get_or_generate_code("telegram", sender_id, config_dir)
                    await _send(chat_id,
                                f"Du bist noch nicht freigeschaltet. Dein Auth-Code: **{code}**\n\n"
                                f"Gib in der Web-UI ein: `/auth telegram {code}`")
                return

            # /stop, /abort, /abbruch: Token-basiert (auch ':' statt '/')
            import re as _re_cancel
            _cancel_tokens = set(_re_cancel.split(r"\s+", body.strip().lower()))
            _cancel_tokens |= {("/" + t[1:]) for t in _cancel_tokens if t.startswith(":") and len(t) > 1}
            _cancel_hit = _cancel_tokens & {"/stop", "/abort", "/abbruch"}
            if _cancel_hit:
                _cancel_cmd = sorted(_cancel_hit)[0]
                from miniassistant.cancellation import request_cancel
                level = "stop" if _cancel_cmd == "/stop" else "abort"
                cancel_key = f"chan:{chat_id}" if (not is_dm and chat_id) else sender_id
                request_cancel(cancel_key, level)
                logger.info("Telegram: %s von %s — Cancellation (%s, key=%s)", body[:40], sender_id, level, cancel_key)
                reply = "⏹ Verarbeitung wird abgebrochen…" if level == "abort" else "⏸ Verarbeitung wird nach aktuellem Schritt gestoppt…"
                await _send(chat_id, reply, reply_to=msg.get("message_id"))
                return

            # Pending Images abholen
            if not msg_images and sender_id in _pending_images:
                msg_images = _pending_images.pop(sender_id)

            logger.info("Telegram: Nachricht von %s (%s): %.80s", display, sender_id, body)

            trusted, via_inviter = _is_trusted(sender_id, chat_id, is_group, config_dir)
            if not trusted:
                code = get_or_generate_code("telegram", sender_id, config_dir)
                logger.info("Telegram: Auth-Code an %s gesendet (Code redacted)", sender_id)
                await _send(chat_id,
                            f"Du bist noch nicht freigeschaltet. Dein Auth-Code: **{code}**\n\n"
                            f"Gib in der Web-UI ein: `/auth telegram {code}`",
                            reply_to=msg.get("message_id"))
                return
            if via_inviter:
                logger.debug("Telegram: %s trusted via Chat-Inviter %s", sender_id, via_inviter)

            # Dokument-Blöcke an User-Text anhängen
            if msg_docs:
                from miniassistant.documents import format_document_block as _fmt_doc
                doc_blocks = [_fmt_doc(d) for d in msg_docs]
                doc_text = "\n\n".join(b for b in doc_blocks if b)
                if doc_text:
                    body = f"{doc_text}\n\n{body}".strip() if body else f"{doc_text}\n\nBitte uebersetze oder fasse das Dokument zusammen."

            images_param = msg_images if msg_images else None

            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(_keep_typing(chat_id, stop_typing))
            try:
                reply = await loop.run_in_executor(
                    None,
                    lambda s=sender_id, b=body, imgs=images_param: _get_chat_response(
                        config, s, b, tg_sessions, images=imgs, chat_id=chat_id, is_group=is_group, user_display=display),
                )
            except Exception as e:
                logger.exception("Telegram KI-Antwort fehlgeschlagen: %s", e)
                reply = f"Fehler bei der Verarbeitung: {e}"
            finally:
                stop_typing.set()
                await typing_task

            if not reply:
                return
            from miniassistant.scheduler import _SILENT_SENTINELS
            if reply.strip() in _SILENT_SENTINELS:
                logger.info("Telegram: Antwort ist Silent-Sentinel (%s) — kein Send", reply.strip())
                return
            await _send(chat_id, reply, reply_to=msg.get("message_id") if not is_dm else None)

        # my_chat_member Updates: Bot zu Gruppe hinzugefügt/entfernt (zuverlässiger als new_chat_members)
        async def _handle_my_chat_member(upd: dict[str, Any]) -> None:
            chat = upd.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            if not chat_id:
                return
            _record_chat(chat)
            new_status = ((upd.get("new_chat_member") or {}).get("status") or "")
            actor = str(((upd.get("from") or {}).get("id")) or "")
            if new_status in ("member", "administrator") and actor:
                if _inviter_cache.get(chat_id) is None:
                    _set_inviter(chat_id, actor)
                    logger.info("Telegram: Chat %s — Inviter %s (my_chat_member)", chat_id, actor)
            elif new_status in ("left", "kicked"):
                with _lock:
                    _known_chats.pop(chat_id, None)
                    snapshot = dict(_known_chats)
                _save_json_file("telegram_chats.json", snapshot)
                logger.info("Telegram: aus Chat %s entfernt (%s)", chat_id, new_status)

        async def _safe_handle(m: dict[str, Any]) -> None:
            try:
                await _handle_message(m)
            except Exception:
                logger.exception("Telegram: Message-Handler fehlgeschlagen")

        offset = 0
        logger.info("Telegram-Bot Long-Polling startet (Token: %s…)", bot_token[:8] if len(bot_token) > 8 else "***")
        try:
            while True:
                try:
                    updates = await _api("getUpdates", offset=offset, timeout=50,
                                         allowed_updates=["message", "my_chat_member"])
                except asyncio.CancelledError:
                    raise
                if updates is None:
                    await asyncio.sleep(5)
                    continue
                for upd in updates:
                    offset = max(offset, int(upd.get("update_id") or 0) + 1)
                    try:
                        if upd.get("message"):
                            # Handler als Task: langsame KI-Antwort blockiert Polling nicht
                            asyncio.create_task(_safe_handle(upd["message"]))
                        elif upd.get("my_chat_member"):
                            await _handle_my_chat_member(upd["my_chat_member"])
                    except Exception:
                        logger.exception("Telegram: Update-Verarbeitung fehlgeschlagen")
                _save_history_maybe()
        except asyncio.CancelledError:
            _save_history_maybe(force=True)
            raise
        except Exception as e:
            logger.exception("Telegram-Bot Fehler: %s", e)
        finally:
            _save_history_maybe(force=True)
