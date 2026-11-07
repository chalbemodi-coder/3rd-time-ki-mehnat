#    This file is part of the AutoAnime distribution.
#    Copyright (c) 2026 Kaif_00z
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, version 3.

import asyncio
import hashlib
import os
import re
import shlex
import shutil
import time
import anitopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from traceback import format_exc

from telethon import Button, TelegramClient, events
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    MessageNotModifiedError,
)
from telethon.sessions import StringSession

from core.bot import Bot
from core.executors import Executors
from database import DataBase
from functions.config import Var
from functions.info import AnimeInfo
from functions.schedule import ScheduleTasks
from functions.tools import Tools
from functions.utils import AdminUtils
from libs.ariawarp import Torrent
from libs.anime_identity import canonical_series_title, episode_number_key, season_number_key
from libs.logger import LOGS, Reporter
from libs.nyaa import Nyaa, NyaaRelease
from libs.subtitle_tracks import subtitle_mapping_args
from libs.torrent_files import find_new_media_file


tools = Tools()
tools.init_dir()
bot = Bot()
dB = DataBase()
bot.run_in_loop(dB.load_channel_settings())


async def _restore_user_session():
    try:
        session_string = await dB.get_user_session()
        if session_string:
            await bot.attach_user_session(session_string)
    except Exception as error:
        # Keep the bot running so the owner can authenticate again with /login.
        LOGS.error(f"Could not restore owner Telegram session: {error}")


bot.run_in_loop(_restore_user_session())
subsplease = Nyaa(dB)
torrent = Torrent()
schedule = ScheduleTasks(bot, enabled=True, source=subsplease)
admin = AdminUtils(dB, bot)
anime_task = None


def _is_owner(event):
    return bool(Var.OWNER and event.sender_id == Var.OWNER)


async def _deny_non_owner(event):
    if _is_owner(event):
        return False
    try:
        await event.reply("This command is only available to the configured bot owner.")
    except Exception:
        pass
    return True


def _ensure_anime_watcher():
    global anime_task
    if not Var.MAIN_CHANNELS:
        return
    if anime_task is None or anime_task.done():
        anime_task = bot.loop.create_task(subsplease.on_new_anime(anime))
        LOGS.info("Anime release watcher started.")


def _channel_help():
    return (
        "**Channel setup (owner only)**\n"
        "`/channels` — show current channel settings\n"
        "`/setchannel main -1001234567890` — add a main channel (maximum 2)\n"
        "`/setchannel log -1001234567890`\n"
        "`/setchannel backup -1001234567890`\n"
        "`/setchannel cloud -1001234567890`\n"
        "`/setchannel forcesub -1001234567890 temp` — per-user invite expires in 10 minutes; next issue revokes the prior link\n"
        "`/setchannel forcesub -1001234567890 fixed` — reusable invite, still requires join approval\n"
        "`/unsetchannel main -1001234567890` or `/unsetchannel main all`\n"
        "`/unsetchannel forcesub -1001234567890` or `/unsetchannel forcesub all`\n"
        "`/unsetchannel log`, `/unsetchannel backup`, `/unsetchannel cloud`\n\n"
        "Give the bot admin rights in each channel. For force-sub it needs **Invite Users**; "
        "for destination channels it needs permission to post. Force-sub invite links use join-request mode."
    )


async def _channel_summary():
    settings = await dB.get_channel_settings()
    main = ", ".join(map(str, settings["main_channels"])) or "not set"
    fsub = "\n".join(
        f"• `{item['channel_id']}` — `{item['mode']}` (join request)"
        for item in settings["force_sub_channels"]
    ) or "not set"
    return (
        "**Current channels**\n"
        f"Main (max 2): {main}\n"
        f"Log: `{settings['log_channel'] or 'not set'}`\n"
        f"Backup: `{settings['backup_channel'] or 'not set'}`\n"
        f"Cloud: `{settings['cloud_channel'] or 'not set'}`\n"
        f"Force-sub (max 6):\n{fsub}\n\n"
        + _channel_help()
    )


async def _rotate_temp_link(user_id, channel_id):
    old = await dB.get_temp_invite(user_id, channel_id)
    if old and old.get("invite_link"):
        await bot.revoke_invite_link(channel_id, old["invite_link"])
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    invite = await bot.create_force_sub_invite(
        channel_id,
        expires_at=expires_at,
        name=f"u{user_id}-10m",
    )
    await dB.save_temp_invite(user_id, channel_id, invite.invite_link, expires_at)
    return invite.invite_link


async def _cleanup_user_invite(user_id, channel_id):
    old = await dB.get_temp_invite(user_id, channel_id)
    if old and old.get("invite_link"):
        await bot.revoke_invite_link(channel_id, old["invite_link"])
    await dB.delete_temp_invite(user_id, channel_id)


def _chat_group_button():
    return Button.url("💬 𝘾𝙝𝙖𝙩 𝙂𝙧𝙤𝙪𝙥", url="https://t.me/+3vWe7Kx_G28yN2M9")

async def _claim_daily_main_poster(anime_info):
    data = anime_info.data or {}
    # Use the canonical English title so aliases (for example Blue Box/Ao no Hako)
    # share one main poster per day while each episode keeps its own side-channel post.
    title = canonical_series_title(
        await anime_info.get_english() or data.get("anime_title") or anime_info.proper_name or anime_info.name
    )
    season = season_number_key(data.get("anime_season") or 1)
    today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
    key = f"{today}:{title}:s{season}"
    claimed = await dB.claim_daily_main_post(key)
    if not claimed:
        LOGS.info(f"Skipping duplicate main-channel poster for {title} on {today}.")
    return claimed

async def _claim_daily_release(anime_info):
    data = anime_info.data or {}
    title = canonical_series_title(data.get("anime_title") or anime_info.proper_name or anime_info.name)
    season = season_number_key(data.get("anime_season") or 1)
    episode = episode_number_key(data.get("episode_number") or "")
    today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
    key = f"release:{today}:{title}:s{season}:e{episode}"
    claimed = await dB.claim_daily_main_post(key)
    if not claimed:
        LOGS.warning(f"Skipping duplicate release {title} episode {episode} on {today}.")
    return claimed

def _put_chat_group_last(buttons, chat_button):
    for row in buttons:
        row[:] = [item for item in row if item is not chat_button]
    buttons[:] = [row for row in buttons if row]
    buttons.append([chat_button])

async def _edit_poster_buttons(poster, buttons):
    try:
        return await poster.edit(buttons=buttons)
    except MessageNotModifiedError:
        # Telegram already has these exact buttons; this is a harmless no-op.
        return poster

async def _delete_private_delivery(messages, seconds=600):
    await asyncio.sleep(seconds)
    for message in messages:
        if message is None:
            continue
        try:
            await message.delete()
            LOGS.info(f"Private delivery message deleted after {seconds}s: {getattr(message, 'id', '?')}")
        except Exception as error:
            LOGS.error(f"Private delivery delete failed for {getattr(message, 'id', '?')}: {error}")

async def _expire_private_delivery(messages):
    if not messages:
        return
    if not isinstance(messages, (list, tuple)):
        messages = [messages]
    notice = None
    try:
        notice = await messages[0].reply(
            "__This private copy will be deleted in 10 minutes. Save or forward it before then.__"
        )
    except Exception as error:
        LOGS.error(f"Could not send private-delivery expiry notice: {error}")
    scheduled = [notice, *messages]
    bot.loop.create_task(_delete_private_delivery(scheduled, seconds=600))
    LOGS.info(f"Private delivery expiry scheduled for {len(messages)} message(s) in 600s.")


async def _deliver_payload(event, payload):
    """Deliver a pending payload to the user and return sent messages."""
    delivered = []
    if not payload:
        return delivered
    if payload.isdigit():
        if not Var.BACKUP_CHANNEL:
            return delivered
        message = await bot.get_messages(Var.BACKUP_CHANNEL, ids=int(payload))
        if message:
            sent = await event.reply(message, buttons=[[_chat_group_button()]])
            delivered.extend(sent if isinstance(sent, (list, tuple)) else [sent])
        return delivered
    items = await dB.get_store_items(payload)
    if not items or not Var.CLOUD_CHANNEL:
        return delivered
    for item_ids in items:
        messages = await bot.get_messages(Var.CLOUD_CHANNEL, ids=item_ids)
        if not messages:
            continue
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        sent = await event.reply(file=[message for message in messages], buttons=[[_chat_group_button()]])
        delivered.extend(sent if isinstance(sent, (list, tuple)) else [sent])
    return delivered

async def _fsub_request_is_pending(channel_id, user_id, query=""):
    """Use Telegram's live pending-request list; cached request flags alone are not access."""
    try:
        async for joiner in bot.pyro_client.get_chat_join_requests(
            int(channel_id), limit=0, query=str(query or "")
        ):
            if joiner.user and int(joiner.user.id) == int(user_id):
                return True
    except Exception as error:
        LOGS.warning(
            f"Could not verify live FSub request for {user_id} in {channel_id}; denying exemption: {error}"
        )
    return False


@bot.on(
    events.NewMessage(
        incoming=True, pattern=r"^/start(?:\s+(.+))?$", func=lambda event: event.is_private
    )
)
async def _start(event):
    xnx = await event.reply("`Please wait...`")
    payload = (event.pattern_match.group(1) or "").strip()
    await dB.add_broadcast_user(event.sender_id)
    if not Var.FORCESUB_CHANNELS:
        return await xnx.edit("Force-sub channels are not configured yet. Please contact the bot owner.")
    sender = await event.get_sender()
    request_query = str(getattr(sender, "username", None) or getattr(sender, "first_name", "") or "")
    pending_state = await dB.get_pending_delivery(event.sender_id)
    requested_channels = {int(channel_id) for channel_id in (pending_state or {}).get("requested_channels", [])}

    missing = []
    required_channel_ids = set()
    for index, item in enumerate(Var.FORCESUB_CHANNELS, start=1):
        channel_id = item["channel_id"]
        try:
            joined = await bot.is_joined(channel_id, event.sender_id)
        except Exception as error:
            LOGS.error(f"Force-sub membership check failed for {channel_id}: {error}")
            return await xnx.edit(
                "I could not check a required channel right now. Please try again shortly."
            )
        if joined:
            # Drop any stale request flag whenever a fresh membership check confirms access.
            await dB.clear_pending_request(event.sender_id, channel_id)
            await _cleanup_user_invite(event.sender_id, channel_id)
            continue
        required_channel_ids.add(channel_id)
        if channel_id in requested_channels:
            if await _fsub_request_is_pending(channel_id, event.sender_id, request_query):
                continue
            # Drop stale authorization (e.g. a request that was cancelled or approved then left).
            await dB.clear_pending_request(event.sender_id, channel_id)
            requested_channels.discard(channel_id)

        try:
            if item["mode"] == "fixed":
                link = item.get("fixed_link")
                if not link:
                    raise RuntimeError("Fixed join-request link is missing; owner must reset this force-sub channel.")
            else:
                link = await _rotate_temp_link(event.sender_id, channel_id)
        except Exception as error:
            LOGS.error(f"Could not create force-sub invite for {channel_id}: {error}")
            return await xnx.edit(
                "A force-sub invite could not be created. Please contact the bot owner."
            )
        missing.append(Button.url(f"📨 REQUEST TO JOIN {index}", url=link))

    if missing:
        if payload:
            pending = await dB.get_pending_delivery(event.sender_id)
            request_recorded = bool(
                pending and pending.get("payload") == payload and await _fsub_requirements_met(event.sender_id)
            )
            if request_recorded:
                try:
                    delivered = await _deliver_payload(event, payload)
                    if delivered:
                        await dB.delete_pending_delivery(event.sender_id)
                        await _expire_private_delivery(delivered)
                        return await xnx.edit(
                            "✅ Join request received. Your video has been sent privately; channel approval is not required."
                        )
                except Exception as error:
                    LOGS.error(f"Recorded-request delivery failed: {error}")
            await dB.save_pending_delivery(event.sender_id, payload, datetime.now(timezone.utc) + timedelta(minutes=15))
        buttons = [[button] for button in missing]
        buttons.append(
            [
                Button.url(
                    "♻️ REFRESH AFTER REQUEST",
                    url=f"https://t.me/{(await bot.get_me()).username}?start={payload}",
                )
            ]
        )
        return await xnx.edit(
            "**Please join the remaining channels to use this bot.**\n"
            "Send a join request using the buttons below. Your video will be sent immediately after the request is received; channel approval is not required.\n"
            "Temporary links expire in 10 minutes and are replaced on the next visit.",
            buttons=buttons,
        )

    if payload:
        if payload.isdigit():
            if not Var.BACKUP_CHANNEL:
                return await xnx.edit("This file link is not available right now.")
            try:
                message = await bot.get_messages(Var.BACKUP_CHANNEL, ids=int(payload))
                if not message:
                    return await xnx.edit("This file link is no longer available.")
                sent = await event.reply(message, buttons=[[_chat_group_button()]])
                await _expire_private_delivery(sent)
            except Exception as error:
                LOGS.error(f"Private file delivery failed: {error}")
                return await xnx.edit("This file could not be delivered. Please request a fresh link.")
        else:
            items = await dB.get_store_items(payload)
            sent_messages = []
            if items:
                for item_ids in items:
                    messages = await bot.get_messages(Var.CLOUD_CHANNEL, ids=item_ids)
                    if messages:
                        sent = await event.reply(file=[message for message in messages], buttons=[[_chat_group_button()]])
                        sent_messages.extend(sent if isinstance(sent, list) else [sent])
                await _expire_private_delivery(sent_messages)
    else:
        if _is_owner(event):
            await xnx.edit(
                "__Browse Admin Options:__\nUse `/channels` to configure destinations and `/login` for the owner Telegram session.",
                buttons=admin.admin_panel(),
            )
            return
        await xnx.edit(
            "**Enjoy ongoing anime releases and episodes.**",
            buttons=[[Button.url("👨‍💻 DEV", url="https://t.me/ahjin_anime")]],
        )
        return
    await xnx.delete()


async def _deliver_pending_user(user_id, payload):
    delivered = []
    if payload.isdigit() and Var.BACKUP_CHANNEL:
        msg = await bot.pyro_client.copy_message(user_id, Var.BACKUP_CHANNEL, int(payload))
        if msg:
            delivered.append(msg)
        return delivered
    items = await dB.get_store_items(payload)
    for item_ids in items or []:
        for message_id in item_ids:
            if Var.CLOUD_CHANNEL:
                msg = await bot.pyro_client.copy_message(user_id, Var.CLOUD_CHANNEL, int(message_id))
                if msg:
                    delivered.append(msg)
    return delivered

async def _fsub_requirements_met(user_id):
    settings = await dB.get_channel_settings()
    pending = await dB.get_pending_delivery(user_id)
    requested = {int(channel_id) for channel_id in (pending or {}).get("requested_channels", [])}
    required = set()
    for item in settings["force_sub_channels"]:
        channel_id = int(item["channel_id"])
        if await bot.is_joined(channel_id, user_id):
            continue
        required.add(channel_id)
    return required.issubset(requested)

@bot.pyro_client.on_chat_join_request()
async def _fsub_join_request(client, request):
    """Deliver only after Telegram reports the user's join request; approval is not required."""
    try:
        settings = await dB.get_channel_settings()
        force_sub_ids = {item["channel_id"] for item in settings["force_sub_channels"]}
        if request.chat.id in force_sub_ids:
            await dB.mark_pending_request(request.from_user.id, request.chat.id)
            pending = await dB.get_pending_delivery(request.from_user.id)
            if pending and pending.get("payload") and await _fsub_requirements_met(request.from_user.id):
                delivered = await _deliver_pending_user(request.from_user.id, str(pending["payload"]))
                if delivered:
                    await dB.delete_pending_delivery(request.from_user.id)
                    await _expire_private_delivery(delivered)
                    LOGS.info(f"Delivered video immediately after all join requests from {request.from_user.id}.")
    except Exception as error:
        LOGS.error(f"Force-sub request tracking failed: {error}")

@bot.pyro_client.on_chat_member_updated()
async def _fsub_member_update(client, update):
    """Deliver after approval/member join and clear access when the user leaves."""
    try:
        settings = await dB.get_channel_settings()
        channel_id = int(update.chat.id)
        force_sub_ids = {int(item["channel_id"]) for item in settings["force_sub_channels"]}
        if channel_id not in force_sub_ids:
            return
        member = getattr(update, "new_chat_member", None)
        user = getattr(member, "user", None) if member else None
        status = str(getattr(member, "status", "")).lower()
        if not user:
            return
        if status in {"left", "banned", "kicked", "chatmemberstatus.left", "chatmemberstatus.banned"}:
            await dB.clear_pending_request(user.id, channel_id)
            LOGS.info(f"Force-sub request invalidated after leave/removal: {user.id} from {channel_id}.")
            return
        if status in {"member", "administrator", "owner", "chatmemberstatus.member", "chatmemberstatus.administrator", "chatmemberstatus.owner"}:
            # Approval means this channel no longer needs a stored pending-request exemption.
            # Clearing it prevents an old request from unlocking videos after a later leave.
            await dB.clear_pending_request(user.id, channel_id)
            pending = await dB.get_pending_delivery(user.id)
            if pending and pending.get("payload") and await _fsub_requirements_met(user.id):
                delivered = await _deliver_pending_user(user.id, str(pending["payload"]))
                if delivered:
                    await dB.delete_pending_delivery(user.id)
                    await _expire_private_delivery(delivered)
                    LOGS.info(f"Delivered video after FSub membership became active for {user.id}.")
    except Exception as error:
        LOGS.error(f"Force-sub member/leave tracking failed: {error}")


@bot.on(events.NewMessage(incoming=True, pattern=r"^/cancel(?:@\w+)?$", func=lambda event: event.is_private))
async def _cancel_current_task(event):
    """Owner-only: stop the current release and immediately start the next scan."""
    global anime_task
    if await _deny_non_owner(event):
        return
    task = anime_task
    if task is None or task.done():
        _ensure_anime_watcher()
        return await event.reply("No active anime task was running. Next scan started.")
    anime_task = None
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    _ensure_anime_watcher()
    await event.reply("✅ Current anime task cancelled. Moving to the next task.")


@bot.on(events.NewMessage(incoming=True, pattern=r"^/login(?:@\w+)?$", func=lambda event: event.is_private))
async def _login(event):
    if await _deny_non_owner(event):
        return
    if not Var.OWNER:
        return await event.reply("Owner login is disabled: configure the numeric OWNER ID first.")
    if bot.user_client:
        try:
            if bot.user_client.is_connected() and await bot.user_client.is_user_authorized():
                return await event.reply("Owner Telegram login is already active.")
        except Exception:
            pass

    login_client = TelegramClient(
        StringSession(), Var.API_ID, Var.API_HASH
    )
    messages_to_delete = []
    login_completed = False

    async def ask_secret(conversation, text):
        # Use the conversation sender so get_response() can track the prompt.
        prompt = await conversation.send_message(text)
        messages_to_delete.append(prompt)
        response = await conversation.get_response()
        messages_to_delete.append(response)
        value = (response.raw_text or "").strip()
        try:
            await response.delete()
        except Exception:
            pass
        return value

    try:
        async with bot.conversation(event.sender_id, timeout=240) as conversation:
            phone = await ask_secret(
                conversation,
                "Send your Telegram phone number in international format. It will be deleted after reading. Send `/cancel` to stop.",
            )
            if phone.lower() == "/cancel":
                return await event.reply("Login cancelled.")
            try:
                await login_client.connect()
                sent_code = await login_client.send_code_request(phone)
            except PhoneNumberInvalidError:
                return await event.reply("That phone number is invalid. Start `/login` again with the correct number.")

            authorized = False
            for attempt in range(1, 4):
                code = await ask_secret(
                    conversation,
                    f"Enter the Telegram login code ({attempt}/3). Do not share it with anyone.",
                )
                if code.lower() == "/cancel":
                    return await event.reply("Login cancelled.")
                try:
                    await login_client.sign_in(
                        phone=phone,
                        code=code.replace(" ", ""),
                        phone_code_hash=sent_code.phone_code_hash,
                    )
                    authorized = True
                    break
                except PhoneCodeInvalidError:
                    await event.reply("That OTP is incorrect. Please enter the latest code again.")
                except PhoneCodeExpiredError:
                    sent_code = await login_client.send_code_request(phone)
                    await event.reply("That OTP expired. Telegram sent a new code; enter the latest OTP.")
                except SessionPasswordNeededError:
                    for password_attempt in range(1, 4):
                        password = await ask_secret(
                            conversation,
                            f"Enter your Telegram 2-step verification password ({password_attempt}/3). It will be deleted after reading.",
                        )
                        if password.lower() == "/cancel":
                            return await event.reply("Login cancelled.")
                        try:
                            await login_client.sign_in(password=password)
                            authorized = True
                            break
                        except PasswordHashInvalidError:
                            await event.reply("That 2FA password is incorrect. Try again.")
                    if authorized:
                        break
                    return await event.reply("Login stopped after three incorrect 2FA attempts. Run `/login` to try again.")

            if not authorized:
                return await event.reply("Login stopped after three incorrect OTP attempts. Run `/login` to try again.")

            user = await login_client.get_me()
            if user.id != Var.OWNER:
                await login_client.disconnect()
                return await event.reply(
                    "The Telegram account you logged in with does not match OWNER. No session was saved."
                )
            session_string = login_client.session.save()
            await dB.save_user_session(session_string)
            bot.user_client = login_client
            login_completed = True
            LOGS.info(f"Owner Telegram session authorized for numeric owner ID {Var.OWNER}.")
            return await event.reply(
                "Telegram login succeeded. The session is saved as plain text in MongoDB without encryption. Restrict database access."
            )
    except TimeoutError:
        return await event.reply("Login timed out. Run `/login` again when ready.")
    except Exception as error:
        LOGS.error(f"Owner Telegram login failed: {error}")
        try:
            await login_client.disconnect()
        except Exception:
            pass
        return await event.reply("Login failed safely. Check the bot logs and try `/login` again.")
    finally:
        if not login_completed:
            try:
                await login_client.disconnect()
            except Exception:
                pass
        for message in messages_to_delete:
            try:
                await message.delete()
            except Exception:
                pass


@bot.on(events.NewMessage(incoming=True, pattern=r"^/channels(?:@\w+)?$", func=lambda event: event.is_private))
async def _channels(event):
    if await _deny_non_owner(event):
        return
    await event.reply(await _channel_summary(), link_preview=False)


@bot.on(events.NewMessage(incoming=True, pattern=r"^/setchannel(?:@\w+)?(?:\s+(.+))?$", func=lambda event: event.is_private))
async def _setchannel(event):
    if await _deny_non_owner(event):
        return
    parts = (event.pattern_match.group(1) or "").split()
    if len(parts) < 2:
        return await event.reply(_channel_help())

    role = parts[0].lower()
    try:
        channel_id = int(parts[1])
    except ValueError:
        return await event.reply("Send a numeric Telegram channel ID, for example `-1001234567890`.")
    if channel_id >= 0:
        return await event.reply("Channel ID must be negative, for example `-1001234567890`.")

    mode = parts[2].lower() if len(parts) > 2 else "temp"
    if role not in {"main", "log", "backup", "cloud", "forcesub"}:
        return await event.reply(_channel_help())
    if role == "forcesub" and len(parts) > 3:
        return await event.reply("Use `/setchannel forcesub <channel_id> temp|fixed`.")
    if role == "forcesub" and mode not in {"temp", "fixed"}:
        return await event.reply("Force-sub mode must be `temp` or `fixed`.")
    if role != "forcesub" and len(parts) > 2:
        return await event.reply("Only force-sub accepts a mode: `temp` or `fixed`.")

    invite = None
    settings_saved = False
    try:
        await bot.check_channel_rights(channel_id, role)
        settings = await dB.get_channel_settings()
        current_fsub = next(
            (item for item in settings["force_sub_channels"] if item["channel_id"] == channel_id),
            None,
        )
        if (
            role == "forcesub"
            and current_fsub is None
            and len(settings["force_sub_channels"]) >= 6
        ):
            raise ValueError("You can configure at most 6 force-sub channels.")
        fixed_link = ""
        invite = None
        if role == "forcesub" and mode == "fixed":
            invite = await bot.create_force_sub_invite(channel_id, name="Auto FSub fixed")
            fixed_link = invite.invite_link

        await dB.set_channel(role, channel_id, mode=mode, fixed_link=fixed_link)
        settings_saved = True
        if role == "forcesub":
            if current_fsub and current_fsub.get("fixed_link"):
                await bot.revoke_invite_link(channel_id, current_fsub["fixed_link"])
            for old_invite in await dB.delete_temp_invites_for_channel(channel_id):
                await bot.revoke_invite_link(channel_id, old_invite.get("invite_link", ""))
        if role == "main":
            _ensure_anime_watcher()
        detail = " Join requests require channel-admin approval." if role == "forcesub" else ""
        if role == "forcesub" and mode == "temp":
            detail += " Per-user links expire in 10 minutes; a new link revokes the previous one."
        if role == "forcesub" and mode == "fixed":
            detail += " This join-request link does not expire automatically."
        await event.reply(f"Saved `{role}` channel `{channel_id}` in MongoDB.{detail}")
    except ValueError as error:
        if invite and not settings_saved:
            await bot.revoke_invite_link(channel_id, invite.invite_link)
        await event.reply(str(error))
    except Exception as error:
        if invite and not settings_saved:
            await bot.revoke_invite_link(channel_id, invite.invite_link)
        LOGS.error(f"Could not set channel role {role} for {channel_id}: {error}")
        await event.reply("Could not configure that channel. Check that the ID is correct and the bot has the required admin rights.")


@bot.on(events.NewMessage(incoming=True, pattern=r"^/unsetchannel(?:@\w+)?(?:\s+(.+))?$", func=lambda event: event.is_private))
async def _unsetchannel(event):
    if await _deny_non_owner(event):
        return
    parts = (event.pattern_match.group(1) or "").split()
    if not parts:
        return await event.reply(_channel_help())
    role = parts[0].lower()
    if role not in {"main", "log", "backup", "cloud", "forcesub"}:
        return await event.reply(_channel_help())

    channel_id = None
    if len(parts) > 1 and parts[1].lower() != "all":
        try:
            channel_id = int(parts[1])
        except ValueError:
            return await event.reply("Use a numeric channel ID or `all`.")
    if role in {"log", "backup", "cloud"}:
        channel_id = None

    try:
        settings = await dB.get_channel_settings()
        if role == "forcesub":
            targets = [
                item for item in settings["force_sub_channels"]
                if channel_id is None or item["channel_id"] == channel_id
            ]
            for item in targets:
                if item.get("fixed_link"):
                    await bot.revoke_invite_link(item["channel_id"], item["fixed_link"])
                for old_invite in await dB.delete_temp_invites_for_channel(item["channel_id"]):
                    await bot.revoke_invite_link(item["channel_id"], old_invite.get("invite_link", ""))
        await dB.unset_channel(role, channel_id)
        await event.reply(f"Removed `{role}` channel setting from MongoDB.")
    except Exception as error:
        LOGS.error(f"Could not unset channel role {role}: {error}")
        await event.reply("Could not remove that channel setting. Try `/channels` and check the channel ID.")


@bot.on(
    events.NewMessage(
        incoming=True,
        pattern=r"^/about(?:@\w+)?$",
        func=lambda event: event.is_private,
    )
)
async def _about(event):
    if await _deny_non_owner(event):
        return
    await admin._about(event)


async def _guard_callback(event):
    if _is_owner(event):
        return False
    await event.answer("Owner only", alert=True)
    return True


@bot.on(events.callbackquery.CallbackQuery(data="slog"))
async def _logs(event):
    if await _guard_callback(event):
        return
    await admin._logs(event)


@bot.on(events.callbackquery.CallbackQuery(data="sret"))
async def _restart(event):
    if await _guard_callback(event):
        return
    await admin._restart(event, schedule)


@bot.on(events.callbackquery.CallbackQuery(data="entg"))
async def _encode(event):
    if await _guard_callback(event):
        return
    await admin._encode_t(event)


@bot.on(events.callbackquery.CallbackQuery(data="sstg"))
async def _ss(event):
    if await _guard_callback(event):
        return
    await admin._ss_t(event)


@bot.on(events.callbackquery.CallbackQuery(data="butg"))
async def _button_upload(event):
    if await _guard_callback(event):
        return
    await admin._btn_t(event)


@bot.on(events.callbackquery.CallbackQuery(data="scul"))
async def _separate_upload(event):
    if await _guard_callback(event):
        return
    await admin._sep_c_t(event)


@bot.on(events.callbackquery.CallbackQuery(data="cast"))
async def _broadcast(event):
    if await _guard_callback(event):
        return
    await admin.broadcast_bt(event)


@bot.on(events.callbackquery.CallbackQuery(data="bek"))
async def _back(event):
    if await _guard_callback(event):
        return
    await event.edit(
        "** <                ADMIN PANEL                 > **",
        buttons=admin.admin_panel(),
    )


@bot.on(events.callbackquery.CallbackQuery(data="channels"))
async def _channel_setup_callback(event):
    if await _guard_callback(event):
        return
    await event.edit(await _channel_summary(), buttons=admin.back_btn(), link_preview=False)


async def _make_quality_copy(source_path, output_path, height):
    """Create a quality-specific working copy without downloading another torrent."""
    subtitle_args = shlex.join(subtitle_mapping_args(source_path))
    command = (
        f"{shlex.quote(Var.FFMPEG)} -hide_banner -loglevel error -y "
        f"-i {shlex.quote(source_path)} -map 0:v -map 0:a? {subtitle_args} "
        f"-vf scale=-2:{int(height)} -c:v libx264 -crf {shlex.quote(str(Var.CRF))} "
        f"-preset fast -threads 0 -c:a aac -c:s copy -metadata Encoded_By=@ahjin_anime "
        f"{shlex.quote(output_path)}"
    )
    process = await asyncio.create_subprocess_shell(command)
    if await process.wait() != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"Could not create {height}p working copy")


def _batch_episode_number(path):
    parsed = anitopy.parse(os.path.basename(path))
    raw = parsed.get("episode_number")
    match = re.search(r"(?:episode|ep|e|[-_ ])(\d{1,3})(?:\D|$)", os.path.basename(path), re.I)
    value = raw if raw is not None else (match.group(1) if match else None)
    try:
        return int(str(value).split("-")[0].strip())
    except (TypeError, ValueError):
        return None


def _batch_video_files(root):
    allowed = {".mkv", ".mp4", ".webm", ".avi", ".mov"}
    files = []
    for folder, _, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            if os.path.splitext(name)[1].lower() in allowed and "sample" not in name.casefold():
                try:
                    if os.path.getsize(path) > 1024 * 1024 and _batch_episode_number(path) is not None:
                        files.append(path)
                except OSError:
                    pass
    return sorted(files, key=lambda item: (_batch_episode_number(item), item.casefold()))


async def _process_current_batch(data, batch_item):
    root = os.path.join("downloads", ".batch-" + hashlib.sha256(str(data.get("uid")).encode()).hexdigest()[:16])
    os.makedirs(root, exist_ok=True)
    reporter = Reporter(bot, batch_item.title, status_channel=None)
    try:
        await reporter.alert_new_file_founded()
        # Direct VPS torrent download, matching the Sub bot's local aria2 flow.
        downloaded_batch = False
        try:
            downloaded_batch = await torrent.download_magnet(batch_item.link, root, reporter)
        except Exception as torrent_error:
            LOGS.warning(f"Direct batch torrent download failed: {torrent_error}")
        if not downloaded_batch:
            LOGS.warning(f"Today's batch download failed: {batch_item.title}")
            return False
        files = _batch_video_files(root)
        if not files:
            LOGS.warning(f"Today's batch has no episode video files: {batch_item.title}")
            return False
        parsed_batch = anitopy.parse(batch_item.title)
        anime_title = str(parsed_batch.get("anime_title") or "").strip()
        season = int(str(parsed_batch.get("anime_season") or 1).split("-")[0])
        series_key = await subsplease._canonical_series_key(anime_title, season)
        completed = 0
        for path in files:
            episode = _batch_episode_number(path)
            if not anime_title or episode is None:
                continue
            title = f"[BatchExtract] {anime_title} S{season:02d}E{episode:02d} 1080p Dual Audio"
            uid = hashlib.sha256(f"batch|{series_key}|s{season}|e{episode}".encode()).hexdigest()
            if await dB.has_episode_uploaded(anime_title, season, episode) or await dB.is_anime_uploaded(uid):
                # This episode is already safely published; remove its local
                # copy too if it was fetched again in the current batch.
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            if not await dB.claim_episode_upload(series_key, season, episode):
                LOGS.info(
                    f"Batch episode already claimed; skipping duplicate: "
                    f"{series_key} S{season:02d}E{episode:02d}"
                )
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            item = {"uid": uid, "1080p": NyaaRelease(title, "localbatch:" + os.path.abspath(path))}
            if await anime(item, _allow_batch=False):
                await dB.add_anime(uid)
                await dB.add_episode_uploaded(f"{series_key}|s{season}|e{episode}")
                completed += 1
                LOGS.info(f"Batch episode uploaded: {anime_title} S{season:02d}E{episode:02d}")
            else:
                await dB.release_episode_claim(series_key, season, episode)
        return completed > 0
    finally:
        if reporter.msg:
            try:
                await reporter.msg.delete()
            except Exception:
                pass
        shutil.rmtree(root, ignore_errors=True)


async def anime(data, _allow_batch=True):
    try:
        native_items = {q: data.get(q) for q in ("480p", "720p", "1080p") if data.get(q)}
        batch_item = next((item for item in native_items.values() if item and re.search(r"(?:batch|\d{1,2}\s*[~–-]\s*\d{1,2})", str(item.title), re.I)), None)
        if _allow_batch and batch_item:
            return await _process_current_batch(data, batch_item)
        if not native_items or not Var.MAIN_CHANNELS:
            LOGS.warning("Skipping anime release: no torrent or no main channel is configured.")
            return
        source_item = max(native_items.values(), key=lambda item: int(re.search(r"(2160|1080|720|480)", item.title).group(1)) if re.search(r"(2160|1080|720|480)", item.title) else 0)
        source_match = re.search(r"(2160|1080|720|480)", source_item.title)
        source_height = int(source_match.group(1)) if source_match else 1080
        source_title = source_item.title
        torrents = []
        # All standard buttons are encoded from one best source. The 4K button
        # is an untouched copy of that source and is never encoded.
        for quality, height in (("480p", 480), ("720p", 720), ("1080p", 1080)):
            if height <= source_height:
                title = re.sub(r"(?<!\d)(2160|1080|720|480)p?(?!\d)", quality, source_title, count=1, flags=re.I)
                torrents.append(type(source_item)(title, "local:" + quality))
        if source_height >= 1080:
            torrents.append(type(source_item)(re.sub(r"(?i)(2160|1080|720|480)p?", "4K", source_title, count=1), "original:4K"))
        work_id = abs(hash("|".join(str(item.title) for item in torrents)))
        local_batch_path = str(source_item.link)[len("localbatch:"):] if str(source_item.link).startswith("localbatch:") else None
        source_file = local_batch_path or f"downloads/.source-{work_id}.mkv"
        work_files = {480: f"downloads/.work-{work_id}-480.mkv", 720: f"downloads/.work-{work_id}-720.mkv", 1080: f"downloads/.work-{work_id}-1080.mkv", 2160: f"downloads/.work-{work_id}-4k.mkv"}
        anime_info = AnimeInfo(torrents[0].title)
        separate_channel_upload = await dB.is_separate_channel_upload()
        chat_info = None
        if separate_channel_upload:
            chat_info = await tools.get_chat_info(bot, anime_info, dB)
            if not chat_info:
                raise RuntimeError("Could not create/find the separate anime channel.")
        # Highest native source is downloaded once; native qualities from other
        # providers remain separate and are never replaced by a transcode.
        if not local_batch_path:
            base_reporter = Reporter(
                bot, source_item.title,
                status_channel=chat_info["chat_id"] if chat_info else Var.LOG_CHANNEL,
            )
            await base_reporter.alert_new_file_founded()
            try:
                before = [os.path.join("downloads", name) for name in os.listdir("downloads") if os.path.isfile(os.path.join("downloads", name))]
                started = time.time()
                base_ok = await torrent.download_magnet(source_item.link, "./downloads/", base_reporter)
                if base_ok and not os.path.isfile(source_file):
                    candidate = find_new_media_file("downloads", before, started)
                    if candidate:
                        shutil.move(candidate, source_file)
                if not os.path.isfile(source_file) or os.path.getsize(source_file) == 0:
                    data["_download_failed"] = True
                    await base_reporter.report_error(f"Download failed or no peers were available for `{source_item.title}`.", log=True)
                    if base_reporter.msg:
                        await base_reporter.msg.delete()
                    return False
            finally:
                if base_reporter.msg:
                    try:
                        await base_reporter.msg.delete()
                    except Exception:
                        pass

        # Do not claim the daily release until a quality has actually uploaded.
        # This keeps failed downloads retryable instead of creating a stale lock.
        # Do not publish the main-channel poster before the first quality is uploaded.
        # The release list is ordered 480p, 720p, 1080p when those qualities exist.
        posters = []
        side_posters = []
        uploaded_any = False
        main_buttons = [[]]
        side_buttons = [[]]
        chat_button = _chat_group_button()
        uploaded_qualities = set()
        main_chat_added = False
        side_chat_added = False
        original_upload = False
        button_upload = await dB.is_button_upload()
        first_quality = torrents[0]
        for quality_index, item in enumerate(torrents):
            try:
                link = str(item.link)
                reporter = Reporter(
                    bot, item.title,
                    status_channel=chat_info["chat_id"] if chat_info else Var.LOG_CHANNEL,
                )
                await reporter.alert_new_file_founded(
                    "Downloading..." if link.startswith("native:") else "Preparing..."
                )
                quality_match = re.search(r"(?i)(2160|1080|720|480)|4K", item.title)
                quality_height = 2160 if re.search(r"(?i)4K", item.title) else (int(quality_match.group(1)) if quality_match and quality_match.group(1) else 480)
                filename = work_files.get(quality_height, source_file)
                downloaded = False
                if link.startswith("base:"):
                    shutil.copy2(source_file, filename)
                    downloaded = True
                elif link.startswith("local:"):
                    await _make_quality_copy(source_file, filename, quality_height)
                    downloaded = True
                elif link.startswith("original:"):
                    shutil.copy2(source_file, filename)
                    downloaded = True
                elif link.startswith("native:"):
                    try:
                        before = [os.path.join("downloads", name) for name in os.listdir("downloads") if os.path.isfile(os.path.join("downloads", name))]
                        started = time.time()
                        native_item = native_items.get(f"{quality_height}p")
                        downloaded = await torrent.download_magnet(native_item.link, "./downloads/", reporter) if native_item else False
                        if downloaded and not os.path.isfile(filename):
                            candidate = find_new_media_file("downloads", before, started)
                            if candidate:
                                shutil.move(candidate, filename)
                    except Exception as native_error:
                        LOGS.warning(f"Native {quality_height}p download failed: {native_error}")
                        downloaded = False
                else:
                    LOGS.warning(f"Unknown quality source link: {link}")
                if not downloaded or not os.path.isfile(filename) or os.path.getsize(filename) == 0:
                    if quality_index == 0:
                        # Let the watcher try the same episode from another
                        # provider only when the source download itself failed.
                        data["_download_failed"] = True
                    await reporter.report_error(
                        f"Download failed or no peers were available for `{item.title}`.",
                        log=True,
                    )
                    if reporter.msg:
                        await reporter.msg.delete()
                    try:
                        if os.path.isfile(filename) and quality_index == 0:
                            os.remove(filename)
                        if os.path.isfile(source_file) and quality_index == 0:
                            os.remove(source_file)
                    except Exception:
                        pass
                    # A missing quality is optional. Continue with the next quality;
                    # any successfully uploaded quality is enough to publish the main poster.
                    LOGS.warning(f"Optional {item.title} quality unavailable; continuing with remaining qualities")
                    # Derived quality copies cannot be created without the source.
                    if quality_index == 0:
                        break
                    continue
                if separate_channel_upload and not main_chat_added:
                    main_buttons = [[
                        Button.url(
                            f"🟦 EPISODE {anime_info.data.get('episode_number', '')}".strip(),
                            url=chat_info["invite_link"],
                        )
                    ], [chat_button]]
                    main_chat_added = True
                # The 4K/original item is copied and uploaded untouched; only
                # standard 480p/720p/1080p items use the encoding path.
                is_original_quality = str(item.link).startswith("original:")
                executor = Executors(
                    bot,
                    dB,
                    {"original_upload": is_original_quality, "button_upload": button_upload},
                    filename,
                    AnimeInfo(item.title),
                    reporter,
                )
                result, button = await executor.execute()
                if result:
                    # A successful Telegram send must stay claimed even if later
                    # poster/button work raises an exception.
                    uploaded_any = True
                    # Keep the local source until all requested quality copies
                    # have been processed; it is removed after the loop below.
                    button_list = list(button) if isinstance(button, (list, tuple)) else ([button] if button else [])
                    # Separate/Rika side channels must always expose a quality
                    # button, even when the optional global BUTTON_UPLOAD switch is off.
                    if chat_info and not button_list and executor.msg_id:
                        me = await bot.get_me()
                        if me and getattr(me, "username", None):
                            button_list = [Button.url(
                                f"🟥 {executor.output_label}",
                                url=f"https://t.me/{me.username}?start={executor.msg_id}",
                            )]
                    quality_text = str(item.title).lower()
                    for quality in ("480p", "720p", "1080p"):
                        if quality in quality_text:
                            uploaded_qualities.add(quality)
                    if chat_info:
                        # Publish as soon as at least one quality has uploaded successfully.
                        for quality_button in button_list:
                            if len(side_buttons[0]) == 2:
                                side_buttons.append([quality_button])
                            else:
                                side_buttons[0].append(quality_button)
                        ready_for_publish = bool(uploaded_qualities)
                        if ready_for_publish:
                            _put_chat_group_last(side_buttons, chat_button)
                            side_chat_added = True
                            if not side_posters:
                                try:
                                    side_posters = await tools._poster(
                                        bot, anime_info, chat_info["chat_id"]
                                    )
                                    if not isinstance(side_posters, list):
                                        side_posters = [side_posters]
                                except Exception as poster_error:
                                    LOGS.warning(f"Side poster skipped; video upload succeeded: {poster_error}")
                                    side_posters = []
                            for poster in side_posters:
                                await _edit_poster_buttons(poster, side_buttons)
                            if not posters and await _claim_daily_main_poster(anime_info):
                                try:
                                    posters = await tools._poster(bot, anime_info)
                                    if not isinstance(posters, list):
                                        posters = [posters]
                                except Exception as poster_error:
                                    LOGS.warning(f"Main poster skipped; video upload succeeded: {poster_error}")
                                    posters = []
                            for poster in posters:
                                await _edit_poster_buttons(poster, main_buttons)
                    else:
                        for quality_button in button_list:
                            if len(main_buttons[0]) == 2:
                                main_buttons.append([quality_button])
                            else:
                                main_buttons[0].append(quality_button)
                        if uploaded_qualities:
                            _put_chat_group_last(main_buttons, chat_button)
                            main_chat_added = True
                            if not posters and await _claim_daily_main_poster(anime_info):
                                posters = await tools._poster(bot, anime_info)
                                if not isinstance(posters, list):
                                    posters = [posters]
                            for poster in posters:
                                await _edit_poster_buttons(poster, main_buttons)
                    await executor.further_work()
                    continue
                await executor.cleanup_files()
                await reporter.report_error(button, log=True)
                if reporter.msg:
                    await reporter.msg.delete()
            except Exception:
                await reporter.report_error(str(format_exc()), log=True)
                if reporter.msg:
                    await reporter.msg.delete()
        try:
            if not local_batch_path and os.path.isfile(source_file):
                os.remove(source_file)
        except Exception as cleanup_error:
            LOGS.warning(f"Source quality cleanup failed: {cleanup_error}")
        if uploaded_any:
            await _claim_daily_release(anime_info)
        return uploaded_any
    except Exception:
        LOGS.error(str(format_exc()))
        return bool(locals().get("uploaded_any", False))


# Channels can be configured after the bot starts; start the release watcher as soon as a main channel exists.
_ensure_anime_watcher()

try:
    bot.run()
except KeyboardInterrupt:
    subsplease._exit()
