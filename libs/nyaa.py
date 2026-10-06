# Nyaa RSS source for Hindi-audio anime releases.
import asyncio
import hashlib
import re
from traceback import format_exc

import aiohttp
from feedparser import parse

from database import LOGS, DataBase


class Nyaa:
    """Poll Nyaa RSS and process new Hindi-audio torrents without repeats."""

    QUALITY_ORDER = {"480p": 0, "720p": 1, "1080p": 2, "2160p": 3}

    def __init__(self, db: DataBase, feed_url: str, interval: int = 300):
        self.db = db
        self.feed_url = feed_url
        self.interval = max(60, interval)
        self.started_with_480 = False

    @staticmethod
    def _valid_title(title: str) -> bool:
        value = title.lower()
        # Hindi releases may be marked as Hindi Audio, Hindi Dub, Dubs or Multi-Audio.
        if not re.search(r"hindi[ -]?(?:audio|dub|dubs)\b|hindi.*\bdubs?\b", value):
            return False
        # Exclude non-video and whole-season torrents: this worker handles episodes.
        blocked = ("[batch]", " batch", "complete series", "complete season", "movie pack")
        if any(word in value for word in blocked):
            return False
        if re.search(r"\b(raw|esub|subbed)[ -]?only\b", value):
            return False
        return True

    @classmethod
    def _quality(cls, title: str) -> str:
        value = title.lower().replace("4k", "2160p")
        match = re.search(r"(?<!\d)(2160|1080|720|480|360)p(?!\d)", value)
        return f"{match.group(1)}p" if match else "Unknown"

    @classmethod
    def _quality_rank(cls, title: str) -> int:
        return cls.QUALITY_ORDER.get(cls._quality(title), 99)

    async def rss_feed_data(self):
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self.feed_url) as response:
                    response.raise_for_status()
                    content = await response.read()
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, parse, content)
        except Exception:
            LOGS.error(f"[Nyaa] RSS fetch failed: {format_exc()}")
            return None

    async def _load_started_state(self):
        if self.started_with_480:
            return True
        try:
            self.started_with_480 = bool(
                await self.db.opts_db.find_one({"_id": "NYAA_480_STARTED"})
            )
        except Exception:
            pass
        return self.started_with_480

    async def feed_optimizer(self):
        await self._load_started_state()
        feed = await self.rss_feed_data()
        if not feed:
            return None
        candidates = []
        for entry in feed.entries:
            title = (getattr(entry, "title", "") or "").strip()
            if not title or not self._valid_title(title):
                continue
            info_hash = (getattr(entry, "nyaa_infohash", "") or "").strip().lower()
            link = getattr(entry, "link", "") or ""
            if not link or not info_hash:
                continue
            quality_rank = self._quality_rank(title)
            if quality_rank > 3:  # Ignore 360p/unknown; supported range starts at 480p.
                continue
            uid = hashlib.sha256(info_hash.encode()).hexdigest()
            if await self.db.is_anime_uploaded(uid):
                continue
            # Lower quality is intentionally selected first; 4K is last.
            candidates.append((quality_rank, entry))
        if not candidates:
            return None
        # Never begin at 720p/1080p: wait until an unprocessed 480p release exists.
        # Once the first 480p has been processed, continue in ascending quality order.
        if not self.started_with_480:
            if not any(rank == 0 for rank, _ in candidates):
                return None
            selected_rank = 0
        else:
            selected_rank = min(rank for rank, _ in candidates)
        entry = next(entry for rank, entry in candidates if rank == selected_rank)
        title = entry.title.strip()
        info_hash = entry.nyaa_infohash.strip().lower()
        return {
            "uid": hashlib.sha256(info_hash.encode()).hexdigest(),
            "title": title,
            "link": entry.link,
            "info_hash": info_hash,
            "quality": self._quality(title),
        }

    async def on_new_anime(self, function):
        cycle = 0
        while True:
            try:
                data = await self.feed_optimizer()
                if data:
                    await function(data)
                    # Hash-based ID means the same torrent/video is never downloaded twice.
                    await self.db.add_anime(data["uid"])
                    if data.get("quality") == "480p":
                        self.started_with_480 = True
                        await self.db.opts_db.update_one(
                            {"_id": "NYAA_480_STARTED"},
                            {"$set": {"started": True}},
                            upsert=True,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGS.error(f"[Nyaa Loop] Error in cycle {cycle}: {format_exc()}")
            cycle += 1
            await asyncio.sleep(self.interval)
