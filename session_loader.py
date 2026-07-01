from __future__ import annotations

import logging
import uuid

from pyrogram import Client, filters, types
from pyrogram.errors import (
    AuthKeyUnregistered,
    SessionRevoked,
    SessionExpired,
    FloodWait,
    ApiIdInvalid,
    AccessTokenInvalid,
    RPCError,
)
from pyrogram.handlers import MessageHandler

from config import Config
from db import add_session

LOGGER = logging.getLogger(__name__)


def _normalize_chat_id(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


async def validate_session(session_string: str) -> bool:
    """Validate a Pyrogram session string by calling get_me()."""
    try:
        async with Client(
            name=f"session_check_{uuid.uuid4().hex}",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            session_string=session_string,
            in_memory=True,
        ) as app:
            await app.get_me()
        return True
    except RPCError:
        return False


async def save_session(session_string: str) -> bool | None:
    """Validate and upsert a session as active.

    Returns:
        True  → session saved successfully
        False → session invalid/expired (RPC error from Telegram)
        None  → session valid but DB write failed
    """
    me = None
    client = None
    started = False
    try:
        client = Client(
            f"session_save_{uuid.uuid4().hex}",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            session_string=session_string,
            in_memory=True,
        )
        await client.start()
        started = True
        me = await client.get_me()
    except AuthKeyUnregistered:
        LOGGER.warning("Session key is not registered (AUTH_KEY_UNREGISTERED).")
        return False
    except SessionRevoked:
        LOGGER.warning("Session has been revoked.")
        return False
    except SessionExpired:
        LOGGER.warning("Session has expired.")
        return False
    except ApiIdInvalid:
        LOGGER.warning("API_ID or API_HASH mismatch with session string.")
        return False
    except AccessTokenInvalid:
        LOGGER.warning("Bot token is invalid (for bot sessions).")
        return False
    except FloodWait as e:
        LOGGER.warning("Flood wait %ss while validating session.", getattr(e, "value", "unknown"))
        return False
    except RPCError as e:
        LOGGER.error(
            "RPC error validating session: %s (%s)", 
            getattr(e, "ID", "UNKNOWN"), 
            str(e),
        )
        return False
    except Exception as e:
        LOGGER.exception("Unexpected error validating session: %s", e)
        return False
    finally:
        if started and client:
            try:
                await client.stop()
            except Exception:
                LOGGER.exception("Failed to stop temporary session client.")

    # If we got here, session is valid — store it
    if me is None:
        return False

    try:
        await add_session(session_string, me.first_name, me.phone_number or str(me.id))
    except Exception:
        LOGGER.exception("Failed to store session in DB.")
        return None

    LOGGER.info("Session added for %s (%s).", me.first_name, me.phone_number)
    return True


async def test_all_sessions() -> None:
    """Validate existing sessions and deactivate invalid ones."""
    from db import deactivate_session, get_active_sessions

    try:
        sessions = await get_active_sessions()
    except Exception:
        LOGGER.exception("Failed to load sessions for validation.")
        return

    if not sessions:
        LOGGER.warning("No sessions loaded yet.")
        return

    for row in sessions:
        session_string = row.get("string")
        phone = row.get("phone") or row.get("name") or "unknown"
        if not session_string:
            LOGGER.warning("Session missing string for %s.", phone)
            continue

        try:
            ok = await validate_session(session_string)
        except Exception:
            LOGGER.exception("Session validation failed for %s.", phone)
            continue

        if not ok:
            LOGGER.warning("Deactivating invalid session for %s.", phone)
            try:
                await deactivate_session(phone)
            except Exception:
                LOGGER.exception("Failed to deactivate invalid session for %s.", phone)


async def _auto_session_val(client: Client, message: types.Message) -> None:
    """Auto-validate session strings posted in the configured session group."""
    try:
        if not message.from_user or not message.text:
            return

        from db import get_active_sessions, get_settings

        conf = await get_settings()
        session_group = _normalize_chat_id(conf.get("session_group"))
        if not session_group or message.chat.id != session_group:
            return

        raw = message.text.strip()
        if not raw:
            await message.reply("❌ Empty session string.")
            return

        result = await save_session(raw)

        if result is True:
            active_sessions = await get_active_sessions()
            await message.reply(
                f"✅ Session added successfully.\n"
                f"📊 Active Sessions: {len(active_sessions)}",
            )
        elif result is None:
            await message.reply(
                "⚠️ Session validated but failed to save. The database may be down.",
            )
        else:
            await message.reply(
                "❌ Session invalid or expired. Make sure:\n"
                "• The session string was generated with the same API_ID/API_HASH\n"
                "• The session hasn't been revoked (try generating a fresh one)\n"
                "• The session is a Pyrogram v2 format session string\n"
                "Try generating a new session string and resend it.",
            )
    except RPCError:
        await message.reply("❌ Session invalid or expired. Try again.")
    except Exception:
        LOGGER.exception("Auto session validation failed.")
        await message.reply("❌ An unexpected error occurred. Check logs for details.")


def register_session_ingest(app: Client) -> None:
    LOGGER.info("Registering session ingestion handlers.")
    app.add_handler(
        MessageHandler(_auto_session_val, filters.text & filters.group),
        group=4,
    )