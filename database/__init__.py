#    This file is part of the AutoAnime distribution.
#    Copyright (c) 2026 Kaif_00z
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, version 3.

import sys
import re
from datetime import datetime, timezone
from traceback import format_exc

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

from functions.config import Var
from functions.session_store import decrypt_session
from libs.anime_identity import (
    canonical_channel_title,
    canonical_episode_id,
    canonical_series_title,
    legacy_episode_id,
    normalize_title,
    season_number_key,
    series_titles_match,
)
from libs.logger import LOGS


CHANNEL_SETTINGS_ID = "CHANNEL_SETTINGS"
OWNER_SESSION_ID = "OWNER_SESSION"


def _channel_id(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result < 0 else 0


def normalize_channel_settings(data):
    data = data or {}
    main_channels = []
    for item in data.get("main_channels", []):
        channel_id = _channel_id(item)
        if channel_id and channel_id not in main_channels:
            main_channels.append(channel_id)
        if len(main_channels) == 2:
            break

    force_sub_channels = []
    seen = set()
    for item in data.get("force_sub_channels", []):
        if not isinstance(item, dict):
            continue
        channel_id = _channel_id(item.get("channel_id", item.get("id")))
        if not channel_id or channel_id in seen:
            continue
        seen.add(channel_id)
        mode = "fixed" if item.get("mode") == "fixed" else "temp"
        force_sub_channels.append(
            {
                "channel_id": channel_id,
                "mode": mode,
                "fixed_link": str(item.get("fixed_link") or "") if mode == "fixed" else "",
            }
        )
        if len(force_sub_channels) == 6:
            break

    return {
        "main_channels": main_channels,
        "log_channel": _channel_id(data.get("log_channel")),
        "backup_channel": _channel_id(data.get("backup_channel")),
        "cloud_channel": _channel_id(data.get("cloud_channel")),
        "force_sub_channels": force_sub_channels,
    }


class DataBase:
    def __init__(self):
        try:
            if not Var.MONGO_SRV:
                raise ValueError("MONGO_SRV is required to connect to MongoDB")
            LOGS.info("Trying To Connect With MongoDB")
            self.client = AsyncIOMotorClient(Var.MONGO_SRV)
            self.db = self.client[Var.DATABASE_NAME]
            self.file_info_db = self.db["fileInfo"]
            self.channel_info_db = self.db["animeChannelInfo"]
            self.opts_db = self.db["opts"]
            self.file_store_db = self.db["fileStore"]
            self.broadcast_db = self.db["broadcastInfo"]
            self.user_session_db = self.db["userSessions"]
            self.fsub_invite_db = self.db["forceSubInvites"]
            self.fsub_pending_db = self.db["forceSubPending"]
            self.daily_main_posts_db = self.db["dailyMainPosts"]
            self.episode_history_db = self.db["episodeHistory"]
            LOGS.info("Successfully Connected With MongoDB")
        except Exception as error:
            LOGS.exception(format_exc())
            LOGS.critical(str(error))
            sys.exit(1)

    async def load_channel_settings(self):
        try:
            await self.fsub_invite_db.create_index(
                "expires_at", expireAfterSeconds=0, name="fsub_invite_expiry"
            )
            await self.fsub_pending_db.create_index(
                "expires_at", expireAfterSeconds=0, name="fsub_pending_expiry"
            )
        except Exception as error:
            LOGS.warning(f"Could not create optional force-sub invite TTL index: {error}")

        data = await self.opts_db.find_one({"_id": CHANNEL_SETTINGS_ID})
        if data:
            settings = normalize_channel_settings(data)
        else:
            # One-time migration for deployments that previously used channel env vars.
            legacy_fsub = []
            if Var.LEGACY_FORCESUB_CHANNEL:
                legacy_fsub.append(
                    {
                        "channel_id": Var.LEGACY_FORCESUB_CHANNEL,
                        "mode": "temp",
                        "fixed_link": "",
                    }
                )
            settings = normalize_channel_settings(
                {
                    "main_channels": [Var.LEGACY_MAIN_CHANNEL]
                    if Var.LEGACY_MAIN_CHANNEL
                    else [],
                    "log_channel": Var.LEGACY_LOG_CHANNEL,
                    "backup_channel": Var.LEGACY_BACKUP_CHANNEL,
                    "cloud_channel": Var.LEGACY_CLOUD_CHANNEL,
                    "force_sub_channels": legacy_fsub,
                }
            )
            await self.opts_db.update_one(
                {"_id": CHANNEL_SETTINGS_ID},
                {"$set": settings},
                upsert=True,
            )

        self._apply_channel_settings(settings)
        return settings

    async def get_channel_settings(self):
        data = await self.opts_db.find_one({"_id": CHANNEL_SETTINGS_ID})
        if not data:
            return await self.load_channel_settings()
        settings = normalize_channel_settings(data)
        self._apply_channel_settings(settings)
        return settings

    async def save_channel_settings(self, settings):
        settings = normalize_channel_settings(settings)
        await self.opts_db.update_one(
            {"_id": CHANNEL_SETTINGS_ID},
            {"$set": settings},
            upsert=True,
        )
        self._apply_channel_settings(settings)
        return settings

    @staticmethod
    def _apply_channel_settings(settings):
        Var.MAIN_CHANNELS = list(settings["main_channels"])
        Var.MAIN_CHANNEL = Var.MAIN_CHANNELS[0] if Var.MAIN_CHANNELS else 0
        Var.LOG_CHANNEL = settings["log_channel"]
        Var.BACKUP_CHANNEL = settings["backup_channel"]
        Var.CLOUD_CHANNEL = settings["cloud_channel"]
        Var.FORCESUB_CHANNELS = list(settings["force_sub_channels"])
        Var.FORCESUB_CHANNEL = (
            Var.FORCESUB_CHANNELS[0]["channel_id"] if Var.FORCESUB_CHANNELS else 0
        )
        Var.FORCESUB_CHANNEL_LINK = (
            Var.FORCESUB_CHANNELS[0].get("fixed_link", "")
            if Var.FORCESUB_CHANNELS
            else ""
        )

    async def set_channel(self, role, channel_id, *, mode="temp", fixed_link=""):
        settings = await self.get_channel_settings()
        role = role.lower()
        channel_id = _channel_id(channel_id)
        if not channel_id:
            raise ValueError("Use a negative Telegram channel ID, such as -1001234567890.")

        if role == "main":
            main = settings["main_channels"]
            if channel_id not in main and len(main) >= 2:
                raise ValueError("You can configure at most 2 main channels.")
            if channel_id not in main:
                main.append(channel_id)
        elif role == "forcesub":
            entries = settings["force_sub_channels"]
            existing = next((i for i in entries if i["channel_id"] == channel_id), None)
            mode = "fixed" if mode == "fixed" else "temp"
            if existing:
                existing.update(mode=mode, fixed_link=fixed_link if mode == "fixed" else "")
            else:
                if len(entries) >= 6:
                    raise ValueError("You can configure at most 6 force-sub channels.")
                entries.append(
                    {
                        "channel_id": channel_id,
                        "mode": mode,
                        "fixed_link": fixed_link if mode == "fixed" else "",
                    }
                )
        elif role in {"log", "backup", "cloud"}:
            settings[f"{role}_channel"] = channel_id
        else:
            raise ValueError("Role must be main, log, backup, cloud, or forcesub.")
        return await self.save_channel_settings(settings)

    async def unset_channel(self, role, channel_id=None):
        settings = await self.get_channel_settings()
        role = role.lower()
        if role == "main":
            if channel_id is None:
                settings["main_channels"] = []
            else:
                settings["main_channels"] = [
                    item for item in settings["main_channels"] if item != channel_id
                ]
        elif role == "forcesub":
            if channel_id is None:
                settings["force_sub_channels"] = []
            else:
                settings["force_sub_channels"] = [
                    item
                    for item in settings["force_sub_channels"]
                    if item["channel_id"] != channel_id
                ]
        elif role in {"log", "backup", "cloud"}:
            settings[f"{role}_channel"] = 0
        else:
            raise ValueError("Role must be main, log, backup, cloud, or forcesub.")
        return await self.save_channel_settings(settings)

    async def get_temp_invite(self, user_id, channel_id):
        return await self.fsub_invite_db.find_one(
            {"_id": f"{int(user_id)}:{int(channel_id)}"}
        )

    async def save_temp_invite(self, user_id, channel_id, link, expires_at):
        await self.fsub_invite_db.update_one(
            {"_id": f"{int(user_id)}:{int(channel_id)}"},
            {
                "$set": {
                    "user_id": int(user_id),
                    "channel_id": int(channel_id),
                    "invite_link": link,
                    "expires_at": expires_at,
                }
            },
            upsert=True,
        )

    async def delete_temp_invite(self, user_id, channel_id):
        await self.fsub_invite_db.delete_one(
            {"_id": f"{int(user_id)}:{int(channel_id)}"}
        )

    async def get_temp_invites_for_channel(self, channel_id):
        cursor = self.fsub_invite_db.find({"channel_id": int(channel_id)})
        return await cursor.to_list(length=None)

    async def delete_temp_invites_for_channel(self, channel_id):
        docs = await self.get_temp_invites_for_channel(channel_id)
        await self.fsub_invite_db.delete_many({"channel_id": int(channel_id)})
        return docs

    async def save_pending_delivery(self, user_id, payload, expires_at):
        await self.fsub_pending_db.update_one(
            {"_id": int(user_id)},
            {
                "$set": {
                    "user_id": int(user_id),
                    "payload": str(payload or ""),
                    "expires_at": expires_at,
                }
            },
            upsert=True,
        )

    async def get_pending_delivery(self, user_id):
        return await self.fsub_pending_db.find_one({"_id": int(user_id)})

    async def mark_pending_request(self, user_id, channel_id):
        await self.fsub_pending_db.update_one(
            {"_id": int(user_id)},
            {"$addToSet": {"requested_channels": int(channel_id)}},
            upsert=True,
        )

    async def delete_pending_delivery(self, user_id):
        # Preserve requested_channels so the same request is not shown again.
        await self.fsub_pending_db.update_one(
            {"_id": int(user_id)},
            {"$unset": {"payload": "", "expires_at": ""}},
        )
    async def clear_pending_request(self, user_id, channel_id):
        await self.fsub_pending_db.update_one(
            {"_id": int(user_id)},
            {"$pull": {"requested_channels": int(channel_id)}},
        )

    async def save_user_session(self, session_string):
        session_string = str(session_string or "")
        if not session_string:
            raise ValueError("Refusing to save an empty Telegram session.")
        await self.user_session_db.update_one(
            {"_id": OWNER_SESSION_ID},
            {
                "$set": {
                    "session_string": session_string,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$unset": {"encrypted_session": ""},
            },
            upsert=True,
        )

    async def get_user_session(self):
        data = await self.user_session_db.find_one({"_id": OWNER_SESSION_ID})
        if not data:
            return None
        session_string = data.get("session_string")
        if session_string:
            return str(session_string)

        # Read and migrate an older encrypted record when its previous key is available.
        encrypted = data.get("encrypted_session")
        if not encrypted:
            return None
        session_string = decrypt_session(encrypted)
        await self.user_session_db.update_one(
            {"_id": OWNER_SESSION_ID},
            {
                "$set": {
                    "session_string": session_string,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$unset": {"encrypted_session": ""},
            },
        )
        return session_string

    async def clear_user_session(self):
        await self.user_session_db.delete_one({"_id": OWNER_SESSION_ID})

    async def claim_daily_main_post(self, key):
        """Atomically allow only the first main-channel post for a daily anime key."""
        try:
            await self.daily_main_posts_db.insert_one({
                "_id": key,
                "created_at": datetime.now(timezone.utc),
            })
            return True
        except DuplicateKeyError:
            return False

    async def is_episode_uploaded(self, key):
        return bool(await self.episode_history_db.find_one({"_id": key, "status": {"$ne": "processing"}}))

    async def has_episode_uploaded(self, anime_title, season, episode):
        """Check canonical episode history, including old seasonless alias keys."""
        canonical_id = canonical_episode_id(anime_title, season, episode)
        legacy_id = legacy_episode_id(anime_title, episode)
        if not canonical_id:
            return False
        if await self.episode_history_db.find_one({"_id": canonical_id, "status": {"$ne": "processing"}}):
            return True
        if await self.episode_history_db.find_one({"_id": legacy_id, "status": {"$ne": "processing"}}):
            return True

        episode_key = re.search(r"\d+", str(episode or ""))
        if not episode_key:
            return False
        suffix = rf"\|e0*{int(episode_key.group(0))}$"
        target = canonical_series_title(anime_title)
        season_key = season_number_key(season)
        async for item in self.episode_history_db.find(
            {"_id": {"$regex": suffix, "$options": "i"}, "status": {"$ne": "processing"}}, {"_id": 1}
        ):
            stored = str(item.get("_id") or "")
            match = re.match(r"^(.*?)(?:\|s(\d+))?\|e0*\d+$", stored, re.I)
            if not match:
                continue
            stored_title, stored_season = match.groups()
            if stored_season and str(int(stored_season)) != season_key:
                continue
            if series_titles_match(stored_title, target):
                return True
        return False

    async def claim_episode_upload(self, anime_title, season, episode):
        """Atomically reserve a canonical episode before any Telegram send."""
        key = canonical_episode_id(anime_title, season, episode)
        if not key:
            return False
        try:
            await self.episode_history_db.insert_one(
                {
                    "_id": key,
                    "status": "processing",
                    "anime_title": str(anime_title or ""),
                    "season": season_number_key(season),
                    "episode": str(episode),
                    "created_at": datetime.now(timezone.utc),
                }
            )
            return True
        except DuplicateKeyError:
            return False

    async def release_episode_claim(self, anime_title, season, episode):
        """Release only an uncompleted reservation after a confirmed no-send failure."""
        key = canonical_episode_id(anime_title, season, episode)
        if key:
            await self.episode_history_db.delete_one(
                {"_id": key, "status": "processing"}
            )

    async def add_episode_uploaded(self, key):
        await self.episode_history_db.update_one(
            {"_id": key},
            {
                "$set": {
                    "status": "uploaded",
                    "uploaded_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {
                    "_id": key,
                    "created_at": datetime.now(timezone.utc),
                },
            },
            upsert=True,
        )

    async def is_daily_release_claimed(self, key):
        return bool(await self.daily_main_posts_db.find_one({"_id": key}))

    async def _daily_episode_claims(self, date_key, anime_title, season=None):
        target = canonical_series_title(anime_title)
        season_key = str(int(re.search(r"\d+", str(season or 1)).group(0)))
        prefix = rf"^release:{re.escape(str(date_key))}:"
        async for item in self.daily_main_posts_db.find(
            {"_id": {"$regex": prefix}}
        ):
            key = str(item.get("_id") or "")
            match = re.match(r"^release:[^:]+:(.*):s(\d+):e(\d+)$", key)
            if not match:
                continue
            stored_title, stored_season, stored_episode = match.groups()
            if canonical_series_title(stored_title) != target:
                continue
            if season is not None and str(int(stored_season)) != season_key:
                continue
            yield int(stored_episode)

    async def is_daily_episode_claimed(self, date_key, anime_key, episode, season=None):
        try:
            current = int(re.search(r"\d+", str(episode)).group(0))
        except (AttributeError, ValueError):
            return False
        async for claimed_episode in self._daily_episode_claims(date_key, anime_key, season):
            if claimed_episode == current:
                return True
        return False

    async def has_higher_daily_episode(self, date_key, anime_key, episode, season=None):
        """Reject a stale lower episode after a newer episode was claimed today."""
        try:
            current = int(re.search(r"\d+", str(episode)).group(0))
        except (AttributeError, ValueError):
            return False
        async for claimed_episode in self._daily_episode_claims(date_key, anime_key, season):
            if claimed_episode > current:
                return True
        return False

    async def add_anime(self, uid):
        data = await self.file_info_db.find_one({"_id": uid})
        if not data:
            await self.file_info_db.insert_one({"_id": uid})

    async def toggle_separate_channel_upload(self):
        data = await self.opts_db.find_one({"_id": "SEPARATE_CHANNEL_UPLOAD"})
        value = not (data or {}).get("switch", False)
        await self.opts_db.update_one(
            {"_id": "SEPARATE_CHANNEL_UPLOAD"}, {"$set": {"switch": value}}, upsert=True
        )

    async def is_separate_channel_upload(self):
        data = await self.opts_db.find_one({"_id": "SEPARATE_CHANNEL_UPLOAD"})
        return (data or {}).get("switch", False)

    async def toggle_original_upload(self):
        data = await self.opts_db.find_one({"_id": "OG_UPLOAD"})
        value = not (data or {}).get("switch", False)
        await self.opts_db.update_one(
            {"_id": "OG_UPLOAD"}, {"$set": {"switch": value}}, upsert=True
        )

    async def is_original_upload(self):
        data = await self.opts_db.find_one({"_id": "OG_UPLOAD"})
        return (data or {}).get("switch", False)

    async def toggle_button_upload(self):
        data = await self.opts_db.find_one({"_id": "BUTTON_UPLOAD"})
        value = not (data or {}).get("switch", False)
        await self.opts_db.update_one(
            {"_id": "BUTTON_UPLOAD"}, {"$set": {"switch": value}}, upsert=True
        )

    async def is_button_upload(self):
        data = await self.opts_db.find_one({"_id": "BUTTON_UPLOAD"})
        return (data or {}).get("switch", False)

    async def is_anime_uploaded(self, uid):
        return bool(await self.file_info_db.find_one({"_id": uid}))

    async def add_anime_channel_info(self, title, data):
        await self.channel_info_db.update_one(
            {"_id": title}, {"$set": {"data": data}}, upsert=True
        )

    async def get_anime_channel_info(self, title):
        data = await self.channel_info_db.find_one({"_id": title})
        return (data or {}).get("data", {})

    async def get_anime_channel_candidates_for_series(self, anime_title):
        """List legacy channel records matching a canonical anime identity."""
        target = canonical_series_title(anime_title)
        preferred = normalize_title(canonical_channel_title(anime_title))
        matches = []
        async for item in self.channel_info_db.find({}):
            stored_title = str(item.get("_id") or "")
            # Historical keys commonly ended in Sxx, and some also appended an
            # episode title. Strip that suffix before comparing series identity.
            series_part = re.sub(r"\s+s\d{1,2}\b.*$", "", stored_title, flags=re.I)
            if canonical_series_title(series_part) != target:
                continue
            rank = 0 if normalize_title(series_part) == preferred else 1
            chat_id = str((item.get("data") or {}).get("chat_id") or "")
            matches.append((rank, normalize_title(series_part), chat_id, item))
        matches.sort(key=lambda match: match[:3])
        return [(match[3] or {}).get("data", {}) for match in matches]

    async def get_anime_channel_info_for_series(self, anime_title, season=None):
        """Return the best-ranked legacy channel across aliases and seasons."""
        candidates = await self.get_anime_channel_candidates_for_series(anime_title)
        return candidates[0] if candidates else {}

    async def store_items(self, _hash, _list):
        await self.file_store_db.update_one(
            {"_id": _hash}, {"$set": {"data": _list}}, upsert=True
        )

    async def get_store_items(self, _hash):
        data = await self.file_store_db.find_one({"_id": _hash})
        return (data or {}).get("data", [])

    async def add_broadcast_user(self, user_id):
        await self.broadcast_db.update_one(
            {"_id": int(user_id)}, {"$setOnInsert": {"_id": int(user_id)}}, upsert=True
        )

    async def get_broadcast_user(self):
        data = self.broadcast_db.find()
        return [i["_id"] for i in await data.to_list(length=None)]

    async def toggle_ss_upload(self):
        data = await self.opts_db.find_one({"_id": "SS_UPLOAD"})
        value = not (data or {}).get("switch", True)
        await self.opts_db.update_one(
            {"_id": "SS_UPLOAD"}, {"$set": {"switch": value}}, upsert=True
        )

    async def is_ss_upload(self):
        data = await self.opts_db.find_one({"_id": "SS_UPLOAD"})
        return (data or {}).get("switch", True)
