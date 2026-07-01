
from __future__ import annotations

import base64
import logging
import struct
import uuid

from pyrogram import Client, filters, types
from pyrogram.errors import (
    ApiIdInvalid,
    AuthKeyUnregistered,
    FloodWait,
    RPCError,
    SessionExpired,
    SessionRevoked,
)
from pyrogram.handlers import MessageHandler

from config import Config
from db import add_session

LOGGER = logging.getLogger(__name__)

# Pyrogram session string formats:
#   v2 new format:  >BI?256sQ?  = 271 bytes  (dc_id + api_id + test_mode + auth_key + user_id + is_bot)
#   v1 old 64-bit:  >B?256sQ?   = 267 bytes  (no api_id)
#   v1 old 32-bit:  >B?256sI?   = 263 bytes  (no api_id, 32-bit user_id)

SESSION_FORMATS = [
    (">BI?256sQ?", 271, "v2", True),
    (">B?256sQ?",  267, "v1_64", False),
    (">B?256sI?",  263, "v1_32", False),
]


def _decode_session_string(raw_string: str) -> dict | None:
    clean = raw_string.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    if not clean:
        return None
    try:
        raw = base64.urlsafe_b64decode(clean + "=" * (-len(clean) % 4))
    except Exception:
        return None
    for fmt, expected_size, label, has_api_id in SESSION_FORMATS:
        if len(raw) == expected_size:
            try:
                unpacked = struct.unpack(fmt, raw)
                if has_api_id:
                    dc_id, api_id, test_mode, auth_key, user_id, is_bot = unpacked
                else:
                    dc_id, test_mode, auth_key, user_id, is_bot = unpacked
                    api_id = 0
                return {
                    "dc_id": dc_id, "api_id": api_id, "test_mode": test_mode,
                    "auth_key": auth_key, "user_id": user_id, "is_bot": is_bot,
                    "label": label,
                }
            except struct.error:
                continue
    return None


def _convert_to_v2_format(decoded: dict) -> str:
    api_id = decoded["api_id"] if decoded["api_id"] else Config.API_ID
    packed = struct.pack(
        ">BI?256sQ?",
        decoded["dc_id"], api_id, decoded["test_mode"],
        decoded["auth_key"], decoded["user_id"], decoded["is_bot"],
    )
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


def _normalize_chat_id(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


async def save_session(raw_session_string: str) -> bool | None:
    """Validate and save session. Supports Pyrogram v1 and v2 strings."""
    me = None
    client = None
    started = False
    session_string = raw_session_string.strip().replace("\n", "").replace("\r", "").replace(" ", "")

    decoded = _decode_session_string(session_string)
    if decoded is None:
        LOGGER.error("Session string format unrecognized.")
        return False

    # Convert v1 to v2 for Pyrogram v2 client compatibility
    if decoded["label"] in ("v1_64", "v1_32"):
        LOGGER.info("Converting %s session to v2 for user_id=%s", decoded["label"], decoded["user_id"])
        session_string = _convert_to_v2_format(decoded)

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
    except struct.error as e:
        LOGGER.error("Struct error: %s", e)
        return False
    except AuthKeyUnregistered:
        LOGGER.warning("Session key unregistered.")
        return False
    except SessionRevoked:
        LOGGER.warning("Session revoked.")
        return False
    except SessionExpired:
        LOGGER.warning("Session expired.")
        return False
    except ApiIdInvalid:
        LOGGER.warning("API_ID mismatch.")
        return False
    except FloodWait as e:
        LOGGER.warning("Flood wait %s", getattr(e, "value", "?"))
        return False
    except RPCError as e:
        LOGGER.error("RPCError: %s", getattr(e, "ID", "UNKNOWN"))
        return False
    except Exception as e:
        LOGGER.exception("Unexpected error: %s", e)
        return False
    finally:
        if started and client:
            try:
                await client.stop()
            except Exception:
                pass

    if me is None:
        return False

    try:
        # Store original string for portability
        await add_session(raw_session_string.strip(), me.first_name, me.phone_number or str(me.id))
    except Exception:
        LOGGER.exception("DB save failed.")
        return None
    return True


async def test_all_sessions() -> None:
    from db import deactivate_session, get_active_sessions
    try:
        sessions = await get_active_sessions()
    except Exception:
        LOGGER.exception("Failed to load sessions.")
        return
    if not sessions:
        LOGGER.warning("No sessions loaded.")
        return
    for row in sessions:
        s = row.get("string", "")
        phone = row.get("phone") or row.get("name") or "?"
        if not s:
            continue
        ok = await _validate(s)
        if not ok:
            LOGGER.warning("Deactivating invalid session: %s", phone)
            try:
                await deactivate_session(phone)
            except Exception:
                pass


async def _validate(session_string: str) -> bool:
    try:
        async with Client(
            f"val_{uuid.uuid4().hex}", api_id=Config.API_ID, api_hash=Config.API_HASH,
            session_string=session_string, in_memory=True,
        ) as app:
            await app.get_me()
        return True
    except RPCError:
        return False


async def _auto_session_val(client: Client, message: types.Message) -> None:
    try:
        if not message.from_user or not message.text:
            return
        from db import get_active_sessions, get_settings
        conf = await get_settings()
        sg = _normalize_chat_id(conf.get("session_group"))
        if not sg or message.chat.id != sg:
            return
        raw = message.text.strip()
        if not raw:
            await message.reply("❌ Empty session string.")
            return
        result = await save_session(raw)
        if result is True:
            active = await get_active_sessions()
            await message.reply(f"✅ Session added.\n📊 Active: {len(active)}")
        elif result is None:
            await message.reply("⚠️ Validated but DB save failed.")
        else:
            await message.reply("❌ Session invalid/expired.")
    except RPCError:
        await message.reply("❌ Session invalid/expired.")
    except Exception:
        LOGGER.exception("Auto session val failed.")
        await message.reply("❌ Error occurred.")


def register_session_ingest(app: Client) -> None:
    LOGGER.info("Registering session ingestion.")
    app.add_handler(MessageHandler(_auto_session_val, filters.text & filters.group), group=4)