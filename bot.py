#    This file is part of the AutoAnime distribution.
#    Copyright (c) 2026 Kaif_00z
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, version 3.
#
#    This program is distributed in the hope that it will be useful, but
#    WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
#    General Public License for more details.
#
# License can be found in <
# https://github.com/kaif-00z/AutoAnimeBot/blob/main/LICENSE > .

# if you are using this following code then don't forgot to give proper
# credit to t.me/kAiF_00z (github.com/kaif-00z)

import glob
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from traceback import format_exc

from telethon import Button, TelegramClient, events, utils
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneNumberInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import UpdateBotChatInviteRequester

from core.bot import Bot
from core.executors import Executors
from database import DataBase
from functions.info import AnimeInfo
from functions.schedule import ScheduleTasks, Var
from functions.tools import Tools, asyncio
from functions.utils import AdminUtils
from libs.ariawarp import Torrent
from libs.logger import LOGS, Reporter
from libs.nyaa import Nyaa

tools = Tools()
tools.init_dir()
bot = Bot()
dB = DataBase()
nyaa = Nyaa(dB, Var.NYAA_FEED_URL, Var.NYAA_CHECK_INTERVAL)
torrent = Torrent()
schedule = ScheduleTasks(bot)
admin = AdminUtils(dB, bot)
_session_login = {}


async def forcesub_pairs():
    """Return unique static + MongoDB-managed ForceSub channels."""
    pairs = []
    if Var.FORCESUB_CHANNEL:
        pairs.append((Var.FORCESUB_CHANNEL, Var.FORCESUB_CHANNEL_LINK))
    for item in Var.FORCESUB_CHANNELS.split(","):
        item = item.strip()
        if not item:
            continue
        ch_id, _, ch_link = item.partition("|")
        try:
            pairs.append((int(ch_id.strip()), ch_link.strip()))
        except ValueError:
            continue
    pairs.extend(await dB.get_forcesub_channels())
    disabled = await dB.get_disabled_forcesub_channels()
    unique = {}
    for channel_id, link in pairs:
        if int(channel_id) in disabled:
            continue
        unique[int(channel_id)] = link or unique.get(int(channel_id), "")
    return list(unique.items())


async def fresh_invite(ch_id, fallback_link, user_id):
    """Make a join-request invite link that Telegram expires after 10 minutes."""
    try:
        peer = await bot.get_input_entity(ch_id)
        r = await bot(
            ExportChatInviteRequest(
                peer=peer,
                expire_date=datetime.now(timezone.utc) + timedelta(minutes=10),
                request_needed=True,
                title=f"fsub {user_id}",
            )
        )
        return r.link
    except Exception:
        LOGS.error(f"Could not create invite link for {ch_id}: {format_exc()}")
        return fallback_link or None


async def fsub_ok(ch_id, user_id):
    """True if the user is a member, or has already sent a join request."""
    if await bot.is_joined(ch_id, user_id):
        return True
    return await dB.has_join_request(ch_id, user_id)


@bot.on(events.Raw(UpdateBotChatInviteRequester))
async def _join_request(update):
    try:
        await dB.add_join_request(utils.get_peer_id(update.peer), update.user_id)
    except Exception:
        LOGS.error(str(format_exc()))


@bot.on(
    events.NewMessage(
        incoming=True,
        pattern=r"^/(addfsub|delfsub|listfsub)(?:@\w+)?(?:\s|$)",
        func=lambda e: e.is_private,
    )
)
async def _manage_fsub(event):
    """Owner-only dynamic ForceSub management.

    Commands:
      /addfsub -100123456789 [fallback-invite-link]
      /delfsub -100123456789
      /listfsub
    The actual user-facing link is freshly generated and expires after 10 minutes.
    """
    if event.sender_id != Var.OWNER:
        return await event.reply("❌ केवल bot owner यह command चला सकता है।")
    command = event.pattern_match.group(1).lower()
    parts = event.raw_text.split()
    if command == "listfsub":
        channels = await forcesub_pairs()
        if not channels:
            return await event.reply("ℹ️ अभी कोई ForceSub channel configured नहीं है।")
        lines = ["**ForceSub channels:**"]
        for channel_id, _ in channels:
            lines.append(f"• `{channel_id}`")
        return await event.reply("\n".join(lines))
    if len(parts) < 2:
        return await event.reply(
            f"Usage: `/{command} -100123456789`\n"
            "`/addfsub` में optional fallback invite link भी दे सकते हैं।"
        )
    try:
        channel_id = int(parts[1])
        if channel_id >= 0:
            raise ValueError
    except ValueError:
        return await event.reply("❌ सही Telegram channel ID दें, जैसे `-1001234567890`।")
    try:
        await bot.get_input_entity(channel_id)
    except Exception:
        return await event.reply(
            "❌ Channel नहीं मिला। Bot को उस channel में admin बनाकर सही ID भेजें।"
        )
    if command == "addfsub":
        fallback = parts[2] if len(parts) > 2 else ""
        await dB.add_forcesub_channel(channel_id, fallback)
        return await event.reply(
            f"✅ ForceSub channel `{channel_id}` add हो गया।\n"
            "हर user के लिए join-request link 10 मिनट में expire होगा।"
        )
    removed = await dB.remove_forcesub_channel(channel_id)
    return await event.reply(
        (f"✅ ForceSub channel `{channel_id}` remove हो गया।" if removed
         else f"ℹ️ Dynamic ForceSub में `{channel_id}` नहीं मिला।")
    )


@bot.on(
    events.NewMessage(
        incoming=True,
        pattern=r"^/(sessionlogin|sessionlogout|sessionstatus)(?:@\w+)?(?:\s|$)",
        func=lambda e: e.is_private,
    )
)
async def _session_command(event):
    """Owner-only hidden Telegram user-session controls."""
    if event.sender_id != Var.OWNER:
        return
    command = event.pattern_match.group(1).lower()
    if command == "sessionstatus":
        if bot.user_client and bot.user_client.is_connected():
            return await event.reply("✅ Telegram user session connected है।")
        return await event.reply("ℹ️ Telegram user session connected नहीं है।")
    if command == "sessionlogout":
        _session_login.clear()
        if bot.user_client:
            try:
                await bot.user_client.disconnect()
            except Exception:
                pass
            bot.user_client = None
        try:
            os.remove(Var.SESSION_FILE)
        except FileNotFoundError:
            pass
        return await event.reply("✅ Telegram user session logout और local session हट गया।")
    if bot.user_client and bot.user_client.is_connected():
        return await event.reply("ℹ️ Session पहले से connected है। पहले `/sessionlogout` करें।")
    try:
        client = TelegramClient(StringSession(), Var.API_ID, Var.API_HASH)
        await client.connect()
        _session_login.clear()
        _session_login.update({"client": client, "state": "phone"})
        await event.reply(
            "🔐 Session login शुरू है। अपना Telegram phone number भेजें, जैसे `+919876543210`.\n"
            "यह flow केवल owner के private chat में काम करता है।"
        )
    except Exception:
        _session_login.clear()
        LOGS.error(f"Session login start failed: {format_exc()}")
        await event.reply("❌ Session login शुरू नहीं हो सका। Logs check करें।")


@bot.on(
    events.NewMessage(
        incoming=True,
        func=lambda e: (
            e.is_private
            and e.sender_id == Var.OWNER
            and not e.raw_text.strip().startswith("/")
        ),
    )
)
async def _session_input(event):
    """Consume phone/code/2FA only while the owner has an active login flow."""
    if not _session_login:
        return
    value = event.raw_text.strip()
    try:
        await event.delete()  # Do not leave phone/code/2FA messages in the chat.
    except Exception:
        pass
    client = _session_login.get("client")
    state = _session_login.get("state")
    try:
        if state == "phone":
            phone = re.sub(r"[^0-9+]", "", value)
            if not re.fullmatch(r"\+\d{7,15}", phone):
                return await event.respond(
                    "❌ Phone number format गलत है। Country code के साथ भेजें, जैसे `+919876543210`."
                )
            sent = await client.send_code_request(phone)
            _session_login.update({"phone": phone, "phone_code_hash": sent.phone_code_hash, "state": "code"})
            return await event.respond("📩 Telegram code भेजें। Spaces हों तो भी चलेगा।")
        if state == "code":
            code = re.sub(r"\D", "", value)
            if not re.fullmatch(r"\d{4,8}", code):
                return await event.respond("❌ OTP केवल digits में भेजें, जैसे `12345`।")
            try:
                await client.sign_in(
                    phone=_session_login["phone"],
                    code=code,
                    phone_code_hash=_session_login["phone_code_hash"],
                )
            except SessionPasswordNeededError:
                _session_login["state"] = "password"
                return await event.respond("🔑 2FA password भेजें।")
            except PhoneCodeInvalidError:
                return await event.respond("❌ OTP गलत है या expire हो चुका है। `/sessionlogin` से नया OTP लें।")
            except PhoneCodeExpiredError:
                _session_login.clear()
                return await event.respond("❌ OTP expire हो गया। फिर `/sessionlogin` चलाएँ और नया code डालें।")
        elif state == "password":
            try:
                await client.sign_in(password=value)
            except PasswordHashInvalidError:
                return await event.respond("❌ 2FA password गलत है। सही password फिर भेजें।")
        else:
            return
        session_string = client.session.save()
        with open(Var.SESSION_FILE, "w", encoding="utf-8") as session_file:
            session_file.write(session_string)
        os.chmod(Var.SESSION_FILE, 0o600)
        bot.user_client = client
        _session_login.clear()
        await event.respond("✅ Telegram user session login सफल हुआ और protected local file में save है।")
    except PhoneNumberInvalidError:
        return await event.respond(
            "❌ Telegram ने phone number reject किया। वही number country code के साथ फिर भेजें।"
        )
    except FloodWaitError as exc:
        _session_login.clear()
        try:
            await client.disconnect()
        except Exception:
            pass
        return await event.respond(
            f"❌ Telegram ने बहुत requests की वजह से रोक दिया। {exc.seconds} seconds बाद फिर `/sessionlogin` चलाएँ।"
        )
    except Exception as exc:
        LOGS.error(f"Session login step failed at state={state}: {type(exc).__name__}: {format_exc()}")
        _session_login.clear()
        try:
            await client.disconnect()
        except Exception:
            pass
        await event.respond(
            f"❌ Login step fail हुआ ({type(exc).__name__})। फिर `/sessionlogin` चलाएँ। Logs में exact कारण save है।"
        )


@bot.on(
    events.NewMessage(
        incoming=True, pattern="^/start ?(.*)", func=lambda e: e.is_private
    )
)
async def _start(event):
    xnx = await event.reply("`Please Wait...`")
    msg_id = event.pattern_match.group(1)
    await dB.add_broadcast_user(event.sender_id)
    not_joined = []
    for ch_id, ch_link in await forcesub_pairs():
        try:
            if not await fsub_ok(ch_id, event.sender_id):
                link = await fresh_invite(ch_id, ch_link, event.sender_id)
                if link:
                    not_joined.append(link)
        except Exception:
            LOGS.error(f"Force-sub check failed for {ch_id}: {format_exc()}")
    if not_joined:
        rows = [
            [Button.url(f"🚀 REQUEST TO JOIN {i}", url=link)]
            for i, link in enumerate(not_joined, 1)
        ]
        rows.append(
            [
                Button.url(
                    "♻️ REFRESH",
                    url=f"https://t.me/{((await bot.get_me()).username)}?start={msg_id}",
                )
            ]
        )
        return await xnx.edit(
            "**Please Join The Following Channel(s) To Use This Bot 🫡**\n"
            "__Send a join request with the button(s) below, then tap REFRESH. "
            "Links expire in 10 minutes.__",
            buttons=rows,
        )
    if msg_id:
        if msg_id.isdigit():
            msg = await bot.get_messages(Var.BACKUP_CHANNEL, ids=int(msg_id))
            sent_msg = await event.reply(msg)
            if Var.DELETE_FILES_FROM_PMS:
                notice = await sent_msg.reply(
                    "__This file will be automatically deleted after 10 minutes.\nPlease save or forward it immediately.__"
                )
                asyncio.create_task(bot.delete_after([notice, sent_msg]))
        else:
            items = await dB.get_store_items(msg_id)
            if items:
                for id in items:
                    msg = await bot.get_messages(Var.CLOUD_CHANNEL, ids=id)
                    if msg:
                        await event.reply(file=[i for i in msg])
    else:
        if event.sender_id == Var.OWNER:
            return await xnx.edit(
                "__Browse Admin Options:__",
                buttons=admin.admin_panel(),
            )
        await event.reply(
            f"**Enjoy Ongoing Anime's Best Encode 24/7 🫡**",
        )
    await xnx.delete()


@bot.on(
    events.NewMessage(incoming=True, pattern="^/about", func=lambda e: e.is_private)
)
async def _(e):
    await admin._about(e)


@bot.on(events.callbackquery.CallbackQuery(data="slog"))
async def _(e):
    await admin._logs(e)


@bot.on(events.callbackquery.CallbackQuery(data="sret"))
async def _(e):
    await admin._restart(e, schedule)


@bot.on(events.callbackquery.CallbackQuery(data="entg"))
async def _(e):
    await admin._encode_t(e)


@bot.on(events.callbackquery.CallbackQuery(data="sstg"))
async def _(e):
    await admin._ss_t(e)


@bot.on(events.callbackquery.CallbackQuery(data="butg"))
async def _(e):
    await admin._btn_t(e)


@bot.on(events.callbackquery.CallbackQuery(data="scul"))
async def _(e):
    await admin._sep_c_t(e)


@bot.on(events.callbackquery.CallbackQuery(data="cast"))
async def _(e):
    await admin.broadcast_bt(e)


@bot.on(events.callbackquery.CallbackQuery(data="bek"))
async def _(e):
    await e.edit(
        "** <                ADMIN PANEL                 > **",
        buttons=admin.admin_panel(),
    )


async def anime(data):
    """Download and process one Nyaa Dual-Audio torrent release."""
    reporter = None
    try:
        title = data["title"]
        anime_info = AnimeInfo(title)
        poster = await tools._poster(bot, anime_info)
        if await dB.is_separate_channel_upload():
            chat_info = await tools.get_chat_info(bot, anime_info, dB)
            if chat_info:
                await poster.edit(
                    buttons=[[Button.url(
                        f"EPISODE {anime_info.data.get('episode_number', '')}".strip(),
                        url=chat_info["invite_link"],
                    )]]
                )
                poster = await tools._poster(bot, anime_info, chat_info["chat_id"])

        safe_name = re.sub(r"[^A-Za-z0-9._ -]+", " ", title).strip()[:180]
        reporter = Reporter(bot, safe_name or data["info_hash"][:12])
        await reporter.alert_new_file_founded()
        download_dir = os.path.join("./downloads", data["info_hash"][:16])
        os.makedirs(download_dir, exist_ok=True)
        await torrent.download_torrent(data["link"], download_dir, reporter)

        # Nyaa torrents can contain one or many episode files. Process media files only.
        media_files = []
        for path in glob.glob(os.path.join(download_dir, "**", "*"), recursive=True):
            if os.path.isfile(path) and path.lower().endswith((".mkv", ".mp4", ".webm", ".avi")):
                media_files.append(path)
        if not media_files:
            raise RuntimeError("Nyaa torrent finished but no video file was found")

        original_upload = await dB.is_original_upload()
        button_upload = await dB.is_button_upload()
        buttons = [[]]
        for input_file in sorted(media_files):
            info = AnimeInfo(os.path.basename(input_file))
            exe = Executors(
                bot, dB, {"original_upload": original_upload, "button_upload": button_upload},
                input_file, info, reporter,
            )
            result, button = await exe.execute()
            if result and button:
                if len(buttons[0]) == 2:
                    buttons.append([button])
                else:
                    buttons[0].append(button)
                await poster.edit(buttons=buttons)
            elif not result:
                await reporter.report_error(button, log=True)
            await exe.further_work()
        shutil.rmtree(download_dir, ignore_errors=True)
    except BaseException:
        LOGS.error(str(format_exc()))
        if reporter and hasattr(reporter, "msg") and reporter.msg:
            await reporter.report_error(str(format_exc()), log=True)



try:
    bot.loop.create_task(nyaa.on_new_anime(anime))
    bot.run()
except KeyboardInterrupt:
    LOGS.info("Nyaa worker stopped.")
