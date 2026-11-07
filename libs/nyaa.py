import asyncio
import hashlib
import html
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from itertools import count
from traceback import format_exc
from urllib.parse import quote

import aiohttp
import anitopy
from decouple import config
from feedparser import parse

from database import LOGS, DataBase
from libs.anime_identity import (
    canonical_series_title,
    episode_number_key,
    normalize_title,
    season_number_key,
    series_titles_match,
)


class NyaaRelease:
    """Small release object compatible with the existing download flow."""

    def __init__(self, title, link, **metadata):
        self.title = title
        self.link = link
        for key, value in metadata.items():
            setattr(self, key, value)


class Nyaa:
    """Poll Nyaa for dual-audio/English-dub anime releases.

    Nyaa availability is release-dependent: a title is emitted only when its
    torrent name advertises dual audio, English audio, or an English dub.
    """

    RSS_ENDPOINT = "https://nyaa.si/?page=rss&c=1_2&q={}"
    ANIMETOSHO_RSS_ENDPOINT = "https://feed.animetosho.net/rss2?magnet_only=1"
    TSUKIHIME_API = "https://api.tsukihime.org/v1/search/torrents"
    ACG_RSS_ENDPOINT = "https://acg.rip/team/223.xml"
    ANIBT_RSS_ENDPOINT = "https://anibt.net/rss/magnets.xml"
    NEKOBT_RSS_ENDPOINT = "https://nekobt.to/api/torznab/api?t=search&q={}"
    DUB_TERMS = re.compile(r"dual[ ._-]*audio|multi[ ._-]*audio|english[ ._-]*(dub|audio)|en(?:g(?:lish)?)?[ ._-]*(?:dub|audio)", re.I)
    RESOLUTION = re.compile(r"(?<!\d)(2160|1080|720|480|576|sd)p?(?!\d)", re.I)

    def __init__(self, dB: DataBase):
        self.db = dB
        self.query = config("NYAA_QUERY", default="Dual Audio")
        self._seen = set()
        # Feed optimizers admit only current-IST-day releases; episodeHistory
        # blocks already-published episodes even if another provider reindexes them.
        self._pending = {}
        # Provider-specific UIDs can represent the same anime episode.
        # Track failures per identity/provider so another website can be tried.
        self._failed_identities = defaultdict(set)
        self.retry_delay = 15 * 60
        self._tsuki_cache = None
        self._tsuki_cache_at = 0.0
        # Official AniList airing dates used to reject old episodes re-indexed today.
        self._airing_cache = {}
        self._anilist_title_cache = {}
        self._anilist_series_title_by_id = {}

    def digest(self, value: str):
        return hashlib.sha256(value.encode()).hexdigest()
    @staticmethod
    def _valid_episode_title(title):
        # TsukiHime can list old BD/OVA and season packs as newly indexed items.
        # The bot must publish only a single, current episode release.
        if re.search(r"\bS00E\d", title, re.I):
            return False
        if re.search(r"\b(?:OVA|SPECIAL|BATCH)\b", title, re.I):
            return False
        if re.search(r"\bS\d{1,2}E\d{1,3}\s*[-–]\s*E?\d", title, re.I):
            return False
        return True

    @staticmethod
    def _is_current_batch_title(title):
        """Recognize season/range batches; publication-date filters remain strict."""
        text = str(title or "")
        return bool(re.search(r"\b(?:batch|\d{1,2}\s*[~–-]\s*\d{1,2})\b", text, re.I))

    def _exit(self):
        LOGS.info("Stopping The Bot...")
        try:
            for folder in ("downloads", "thumbs", "encode"):
                shutil.rmtree(folder, ignore_errors=True)
        except Exception:
            LOGS.error(format_exc())
        sys.exit(0)

    @classmethod
    def _resolution(cls, title):
        matches = cls.RESOLUTION.findall(title or "")
        if not matches:
            return None
        value = int(matches[-1])
        if value >= 2160:
            return "1080p"
        if value >= 1080:
            return "1080p"
        if value >= 720:
            return "720p"
        return "480p"

    @staticmethod
    def _entry_magnet(entry):
        for key in ("nyaa_magneturi", "nyaa_magnetURI", "magneturi", "magnet"):
            value = entry.get(key)
            if value and str(value).startswith("magnet:"):
                return html.unescape(str(value))
        for value in entry.get("links", []):
            href = value.get("href", "")
            if href.startswith("magnet:"):
                return html.unescape(href)
        # AnimeTosho exposes magnets in RSS enclosure elements.
        for value in entry.get("enclosures", []) or []:
            href = value.get("href") or value.get("url") or ""
            if str(href).startswith("magnet:"):
                return html.unescape(str(href))
        info_hash = str(entry.get("nyaa_infohash") or "").strip()
        if info_hash:
            title = quote(str(entry.get("title") or ""))
            return f"magnet:?xt=urn:btih:{info_hash}&dn={title}"
        return ""

    async def _detail_magnet(self, session, url):
        if not url or url.startswith("magnet:"):
            return url
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                body = await response.text(errors="ignore")
            match = re.search(r"magnet:\?xt=urn:btih:[^\"'<>\s]+", html.unescape(body))
            return match.group(0) if match else ""
        except Exception as error:
            LOGS.warning(f"Could not read Nyaa torrent page {url}: {error}")
            return ""

    @staticmethod
    def _bdecode(data, pos=0):
        token = data[pos:pos + 1]
        if token == b"i":
            end = data.index(b"e", pos + 1)
            return int(data[pos + 1:end]), end + 1
        if token == b"l":
            pos += 1
            result = []
            while data[pos:pos + 1] != b"e":
                value, pos = Nyaa._bdecode(data, pos)
                result.append(value)
            return result, pos + 1
        if token == b"d":
            pos += 1
            result = {}
            while data[pos:pos + 1] != b"e":
                key, pos = Nyaa._bdecode(data, pos)
                value, pos = Nyaa._bdecode(data, pos)
                result[key] = value
            return result, pos + 1
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        start = colon + 1
        return data[start:start + length], start + length

    @classmethod
    async def _torrent_url_to_magnet(cls, session, url, title):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    return ""
                payload = await response.read()
            root, _ = cls._bdecode(payload)
            # Reparse the top-level dictionary to preserve the exact bencoded info bytes.
            pos = 1
            info_raw = None
            while payload[pos:pos + 1] != b"e":
                key, pos = cls._bdecode(payload, pos)
                value_start = pos
                _, pos = cls._bdecode(payload, pos)
                if key == b"info":
                    info_raw = payload[value_start:pos]
                    break
            if not info_raw:
                return ""
            info_hash = hashlib.sha1(info_raw).hexdigest()
            return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}"
        except Exception as error:
            LOGS.warning(f"Could not convert torrent file from {url}: {error}")
            return ""

    @staticmethod
    def _torrent_url(entry, source):
        if source == "anibt":
            value = entry.get("anibt_torrenturl")
            if value:
                return str(value)
        for item in entry.get("links", []) or []:
            href = str(item.get("href") or "")
            if href.endswith(".torrent") or item.get("type") == "application/x-bittorrent":
                return href
        page = str(entry.get("link") or "")
        if source == "acg":
            match = re.search(r"/t/(\d+)", page)
            if match:
                return f"https://acg.rip/t/{match.group(1)}.torrent"
        return ""

    async def _public_source_optimizer(self, endpoint, source, target_identity=None, allow_seen=False):
        loop = asyncio.get_running_loop()
        feed = await loop.run_in_executor(None, parse, endpoint)
        if not feed or not feed.entries:
            return None
        today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()
        candidates = defaultdict(dict)
        async with aiohttp.ClientSession(headers={"User-Agent": "AutoDualAudio/1.0"}) as session:
            for entry in feed.entries:
                title = str(entry.get("title") or "").strip()
                if not title or not self._valid_episode_title(title) or not self.DUB_TERMS.search(title):
                    continue
                published = entry.get("published_parsed")
                if not published:
                    continue
                published_at = datetime(*published[:6], tzinfo=timezone.utc)
                if published_at.astimezone(ZoneInfo("Asia/Kolkata")).date() != today:
                    continue
                if re.search(r"\[?batch\]?", title, re.I):
                    continue
                resolution = self._resolution(title)
                parsed = anitopy.parse(title)
                anime = parsed.get("anime_title")
                episode = parsed.get("episode_number")
                if not anime or not episode or isinstance(episode, (list, tuple)) or not resolution:
                    continue
                torrent_url = self._torrent_url(entry, source)
                magnet = await self._torrent_url_to_magnet(session, torrent_url, title)
                if not magnet:
                    continue
                identity = (
                    canonical_series_title(anime),
                    season_number_key(parsed.get("anime_season") or 1),
                    episode_number_key(episode),
                )
                if target_identity and not await self._identities_match(title, identity, target_identity):
                    continue
                candidates[identity].setdefault(
                    resolution,
                    NyaaRelease(title, magnet, provider=source),
                )
        for identity, releases in candidates.items():
            sample = next(iter(releases.values()))
            parsed = anitopy.parse(sample.title)
            if not await self._official_episode_is_today_for_title(
                parsed.get("anime_title") or identity[0],
                parsed.get("episode_number") or identity[2],
                today,
                parsed.get("anime_season") or identity[1],
            ):
                LOGS.info(f"Skipping non-current official episode from {source}: {identity[0]} episode {identity[2]}")
                continue
            if await self.db.has_episode_uploaded(*identity):
                LOGS.info(f"Skipping previously uploaded episode from {source}: {identity[0]} S{identity[1]} E{identity[2]}")
                continue
            titles = sorted(item.title for item in releases.values())
            uid = self.digest(f"{str(source).casefold()}|" + "|".join(titles))
            if (uid in self._seen and not allow_seen) or await self.db.is_anime_uploaded(uid):
                continue
            self._seen.add(uid)
            LOGS.info(f"Selected today's {source.upper()} release: {identity[0]} episode {identity[2]}")
            result = {"uid": uid}
            result.update(releases)
            return result
        return None

    async def _feed_from_url(self, url):
        # Never let an unavailable RSS provider block the next source.
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "AutoDualAudio/1.0"},
            ) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    payload = await response.read()
            return parse(payload)
        except Exception as error:
            LOGS.warning(f"RSS source unavailable: {url} ({type(error).__name__})")
            return None

    async def _rss_entries(self):
        return await self._feed_from_url(self.RSS_ENDPOINT.format(quote(self.query)))

    async def _official_episode_is_today(self, anime_id, episode, today):
        """Require the official airing date to be today when TsukiHime gives an AniList id."""
        try:
            anime_id = int(anime_id)
            episode = int(re.search(r"\d+", str(episode)).group(0))
        except (TypeError, ValueError, AttributeError):
            return False
        if anime_id not in self._airing_cache:
            query = """query($id:Int,$page:Int){Media(id:$id,type:ANIME){airingSchedule(page:$page,perPage:100){nodes{episode airingAt}}}}"""
            try:
                async with aiohttp.ClientSession(headers={"User-Agent": "AutoDualAudio/1.0"}) as session:
                    async with session.post(
                        "https://graphql.anilist.co",
                        json={"query": query, "variables": {"id": anime_id, "page": max(1, (episode - 1) // 100 + 1)}},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as response:
                        payload = await response.json() if response.status == 200 else {}
                nodes = (((payload.get("data") or {}).get("Media") or {}).get("airingSchedule") or {}).get("nodes") or []
                dates = {}
                for node in nodes:
                    try:
                        ep = int(node.get("episode"))
                        date = datetime.fromtimestamp(int(node.get("airingAt")), timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
                        dates[ep] = date
                    except (TypeError, ValueError, OSError):
                        continue
                self._airing_cache[anime_id] = dates
            except Exception as error:
                LOGS.warning(f"Official airing-date check failed for AniList {anime_id}: {error}")
                self._airing_cache[anime_id] = None
        dates = self._airing_cache.get(anime_id)
        # Fail closed when AniList is unavailable: a source re-indexed today is
        # not sufficient proof that the episode itself is current.
        return bool(dates) and dates.get(episode) == today.isoformat()

    async def _anilist_media_id_for_title(self, anime_title, season=1):
        title = str(anime_title or "").strip()
        if not title:
            return None
        season_key = season_number_key(season)
        explicit_season = re.search(
            r"\b(?:season|part|cour)\s*\d+|\b\d+(?:st|nd|rd|th)\s+season\b",
            title,
            re.I,
        )
        base_query = normalize_title(title)
        query_title = base_query
        if season_key != "1" and not explicit_season:
            query_title = f"{base_query} season {season_key}"
        cache_key = normalize_title(query_title)
        cached = self._anilist_title_cache.get(cache_key)
        if cached:
            ttl = 1800 if cached[1] else 60
            if time.monotonic() - cached[0] < ttl:
                return cached[1]

        query = """query($search:String){Media(search:$search,type:ANIME){id title{english romaji native} synonyms}}"""
        media_id = None
        request_succeeded = False
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": "AutoDualAudio/1.0"}) as session:
                async with session.post(
                    "https://graphql.anilist.co",
                    json={"query": query, "variables": {"search": query_title}},
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as response:
                    payload = await response.json() if response.status == 200 else {}
                    request_succeeded = response.status == 200
            media = ((payload.get("data") or {}).get("Media") or {})
            names = [
                *((media.get("title") or {}).get(key) for key in ("english", "romaji", "native")),
                *(media.get("synonyms") or []),
            ]
            accepted_names = {base_query, normalize_title(query_title)}
            title_key = canonical_series_title(title)
            if media.get("id") and any(
                normalize_title(name) in accepted_names
                or (season_key == "1" and canonical_series_title(name) == title_key)
                or series_titles_match(name, title)
                for name in names if name
            ):
                media_id = int(media["id"])
                preferred = (media.get("title") or {}).get("english") or (media.get("title") or {}).get("romaji") or title
                self._anilist_series_title_by_id[media_id] = canonical_series_title(preferred)
        except Exception as error:
            # Keep transport failures retryable soon; they are not proof of a bad title.
            LOGS.warning(f"AniList title lookup failed for {title}: {type(error).__name__}")
            self._anilist_title_cache[cache_key] = (time.monotonic(), None)
        if request_succeeded:
            self._anilist_title_cache[cache_key] = (time.monotonic(), media_id)
        return media_id

    async def _official_episode_is_current_release(self, anime_id, episode, today):
        """Accept only the episode whose official AniList airing date is today."""
        try:
            episode_number = int(re.search(r"\d+", str(episode)).group(0))
            anime_id = int(anime_id)
        except (TypeError, ValueError, AttributeError):
            return False
        await self._official_episode_is_today(anime_id, episode_number, today)
        dates = self._airing_cache.get(anime_id)
        return bool(dates) and dates.get(episode_number) == today.isoformat()

    async def _official_episode_is_today_for_title(self, anime_title, episode, today, season=1):
        """Validate a same-day dub without mistaking its Japanese episode lag for an old release."""
        try:
            episode_number = int(re.search(r"\d+", str(episode)).group(0))
        except (AttributeError, ValueError):
            return False
        media_id = await self._anilist_media_id_for_title(anime_title, season)
        if not media_id:
            LOGS.info(f"Skipping unresolved AniList title to prevent stale episode: {anime_title}")
            return False
        return await self._official_episode_is_current_release(media_id, episode_number, today)

    async def _canonical_series_key(self, anime_title, season=1):
        title = str(anime_title or "").strip()
        season_key = season_number_key(season)
        query_title = normalize_title(title)
        if season_key != "1" and not re.search(
            r"\b(?:season|part|cour)\s*\d+|\b\d+(?:st|nd|rd|th)\s+season\b",
            title,
            re.I,
        ):
            query_title = f"{query_title} season {season_key}"
        media_id = await self._anilist_media_id_for_title(title, season_key)
        if media_id:
            return self._anilist_series_title_by_id.get(
                int(media_id), canonical_series_title(anime_title),
            )
        return canonical_series_title(anime_title)

    async def _identities_match(self, candidate_title, identity, target_identity):
        """Match provider aliases only when season/episode and AniList identity agree."""
        if not target_identity or identity == target_identity:
            return True
        if identity[1:] != target_identity[1:]:
            return False
        parsed = anitopy.parse(str(candidate_title or ""))
        candidate_title = parsed.get("anime_title")
        if not candidate_title:
            return False
        candidate_id = await self._anilist_media_id_for_title(candidate_title, identity[1])
        target_id = await self._anilist_media_id_for_title(target_identity[0], target_identity[1])
        return candidate_id is not None and candidate_id == target_id

    async def _tsukihime_optimizer(self, target_identity=None):
        """Find today's English/Dual-Audio releases from TsukiHime."""
        now = datetime.now(timezone.utc)
        if self._tsuki_cache is None or (now.timestamp() - self._tsuki_cache_at) >= 60:
            try:
                params = {"q": "dual audio", "limit": 100, "offset": 0}
                async with aiohttp.ClientSession(headers={"User-Agent": "AutoDualAudio/1.0"}) as session:
                    async with session.get(self.TSUKIHIME_API, params=params, timeout=aiohttp.ClientTimeout(total=20)) as response:
                        self._tsuki_cache = await response.json() if response.status == 200 else {"results": []}
                self._tsuki_cache_at = now.timestamp()
            except Exception as error:
                LOGS.warning(f"Could not read TsukiHime API: {error}")
                self._tsuki_cache = {"results": []}
                self._tsuki_cache_at = now.timestamp()
        today = now.astimezone(ZoneInfo("Asia/Kolkata")).date()
        candidates = defaultdict(dict)
        for entry in (self._tsuki_cache or {}).get("results", []):
            title = str(entry.get("name") or "").strip()
            is_batch = self._is_current_batch_title(title)
            if not title or (not self._valid_episode_title(title) and not is_batch) or not self.DUB_TERMS.search(title):
                continue
            try:
                source_date = datetime.fromtimestamp(int(entry.get("source_date") or 0), timezone.utc)
            except (TypeError, ValueError, OSError):
                continue
            if source_date.astimezone(ZoneInfo("Asia/Kolkata")).date() != today:
                continue
            parsed = anitopy.parse(title)
            anime = parsed.get("anime_title")
            episode = parsed.get("episode_number")
            resolution = self._resolution(title)
            btih = str(entry.get("btih") or "").strip()
            if not anime or (not episode and not is_batch) or isinstance(episode, (list, tuple)) or not resolution or not btih:
                continue
            anime_meta = entry.get("anime") or {}
            anime_id = anime_meta.get("id")
            batch_count = int(entry.get("filecount") or 0)
            if is_batch:
                # A batch is accepted only when every episode represented by its
                # files is officially scheduled for today's IST date.
                if not anime_id or batch_count < 2 or not await self._official_episode_is_today(anime_id, 1, today):
                    continue
                official_dates = self._airing_cache.get(int(anime_id)) or {}
                if any(official_dates.get(ep) != today.isoformat() for ep in range(1, batch_count + 1)):
                    LOGS.info(f"Skipping mixed/old batch release: {anime}")
                    continue
                episode_key = "batch"
            else:
                if not anime_id or not await self._official_episode_is_current_release(
                    anime_id, episode, today,
                ):
                    LOGS.info(f"Skipping old re-indexed release: {anime} episode {episode}")
                    continue
                episode_key = str(episode).strip()
            identity = (
                canonical_series_title(anime),
                season_number_key(parsed.get("anime_season") or 1),
                episode_number_key(episode_key),
            )
            if target_identity and not await self._identities_match(title, identity, target_identity):
                continue
            # Seedr needs the info-hash magnet; it handles the source torrent remotely.
            link = f"magnet:?xt=urn:btih:{btih}&dn={quote(title)}"
            candidates[identity].setdefault(
                resolution,
                NyaaRelease(
                    title, link, is_batch=is_batch, batch_count=batch_count,
                    anime_id=anime_id, provider="TsukiHime",
                ),
            )
        for identity, releases in candidates.items():
            if identity[2] and await self.db.has_episode_uploaded(*identity):
                LOGS.info(f"Skipping previously uploaded episode from TsukiHime: {identity[0]} S{identity[1]} E{identity[2]}")
                continue
            titles = sorted(item.title for item in releases.values())
            uid = self.digest("tsuki|" + "|".join(titles))
            if uid in self._seen or await self.db.is_anime_uploaded(uid):
                continue
            if releases:
                self._seen.add(uid)
                LOGS.info(f"Selected today's TsukiHime release: {identity[0]} episode {identity[2]}")
                result = {"uid": uid}
                result.update(releases)
                return result
        return None

    async def _rss_optimizer(self, feed, source_label="Nyaa", target_identity=None, allow_seen=False):
        candidates = defaultdict(dict)
        today_ist = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()
        for entry in feed.entries:
            title = str(entry.get("title") or "").strip()
            is_batch = self._is_current_batch_title(title)
            if not title or (not self._valid_episode_title(title) and not is_batch) or not self.DUB_TERMS.search(title):
                continue
            published = entry.get("published_parsed")
            if not published:
                continue
            published_date = datetime(*published[:6], tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()
            if published_date != today_ist:
                continue
            # Same-day batches are intentionally retained; bot.py extracts each file.
            resolution = self._resolution(title); parsed = anitopy.parse(title)
            anime = parsed.get("anime_title"); episode = parsed.get("episode_number")
            if not anime or not episode or isinstance(episode,(list,tuple)) or not resolution:
                continue
            identity = (
                canonical_series_title(anime),
                season_number_key(parsed.get("anime_season") or 1),
                episode_number_key(episode),
            )
            if target_identity and not await self._identities_match(title, identity, target_identity):
                continue
            candidates[identity].setdefault(resolution, entry)
        async with aiohttp.ClientSession(headers={"User-Agent":"AutoDualAudio/1.0"}) as session:
            for identity,releases in candidates.items():
                sample = next(iter(releases.values()))
                parsed = anitopy.parse(str(sample.get("title") or ""))
                if not await self._official_episode_is_today_for_title(
                    parsed.get("anime_title") or identity[0],
                    parsed.get("episode_number") or identity[2],
                    today_ist,
                    parsed.get("anime_season") or identity[1],
                ):
                    LOGS.info(f"Skipping non-current official episode from {source_label}: {identity[0]} episode {identity[2]}")
                    continue
                if await self.db.has_episode_uploaded(*identity):
                    LOGS.info(f"Skipping previously uploaded episode from {source_label}: {identity[0]} S{identity[1]} E{identity[2]}")
                    continue
                titles=sorted(str(item.get("title") or "") for item in releases.values())
                uid=self.digest(f"{source_label.casefold()}|" + "|".join(titles))
                if (uid in self._seen and not allow_seen) or await self.db.is_anime_uploaded(uid):
                    continue
                result={"uid":uid}
                for resolution,entry in releases.items():
                    magnet=self._entry_magnet(entry)
                    if not magnet:
                        magnet=await self._detail_magnet(session,entry.get("link",""))
                    if magnet:
                        result[resolution] = NyaaRelease(
                            entry.get("title", ""), magnet, provider=source_label,
                        )
                if len(result)>1:
                    self._seen.add(uid); LOGS.info(f"Selected today's {source_label} release: {identity[0]} episode {identity[2]}"); return result
        return None

    async def feed_optimizer(self):
        # Query every configured provider with a bounded timeout, then merge
        # native quality entries for the same current episode.
        today_key = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
        now = datetime.now(timezone.utc).timestamp()
        for uid, item in list(self._pending.items()):
            if item.get("date") != today_key:
                del self._pending[uid]
                LOGS.info(f"Dropping stale pending release: {uid[:10]}")
                continue
            if item["retry_at"] <= now:
                del self._pending[uid]
                LOGS.info(f"Retrying today's pending release: {uid[:10]}")
                return item["data"]

        async def run_source(label, factory):
            try:
                data = await asyncio.wait_for(factory(), timeout=22)
                if data:
                    LOGS.info(f"Quality source returned: {label}")
                return data
            except asyncio.TimeoutError:
                LOGS.warning(f"Quality source timed out after 22s: {label}")
            except Exception as error:
                LOGS.warning(f"Quality source failed ({label}): {error}")
            return None

        async def nyaa():
            feed = await self._rss_entries()
            return await self._rss_optimizer(feed, "Nyaa") if feed and feed.entries else None
        async def animetosho():
            feed = await self._feed_from_url(self.ANIMETOSHO_RSS_ENDPOINT)
            return await self._rss_optimizer(feed, "AnimeTosho") if feed and feed.entries else None
        async def tsukihime():
            return await self._tsukihime_optimizer()
        async def acg():
            return await self._public_source_optimizer(self.ACG_RSS_ENDPOINT, "acg")
        async def anibt():
            return await self._public_source_optimizer(self.ANIBT_RSS_ENDPOINT, "anibt")

        async def nekobt():
            feed = await self._feed_from_url(self.NEKOBT_RSS_ENDPOINT.format(quote(self.query)))
            return await self._rss_optimizer(feed, "Nekobt") if feed and feed.entries else None
        results = []
        for label, factory in (("Nyaa", nyaa), ("AnimeTosho", animetosho), ("TsukiHime", tsukihime), ("ACG", acg), ("AniBT", anibt), ("Nekobt", nekobt)):
            data = await run_source(label, factory)
            if data:
                results.append((label, data))

        if results:
            target_identity = self._data_identity(results[0][1])
            merged = dict(results[0][1])
            merged_sources = [results[0][0]]
            for label, data in results[1:]:
                if target_identity and self._data_identity(data) != target_identity:
                    continue
                merged_sources.append(label)
                for quality in ("480p", "720p", "1080p"):
                    if not merged.get(quality) and data.get(quality):
                        merged[quality] = data[quality]
            title_list = [str(item.title) for key, item in merged.items() if key != "uid" and getattr(item, "title", None)]
            merged["uid"] = self.digest("merged|" + "|".join(sorted(title_list)))
            LOGS.info(f"Merged current episode quality sources: {', '.join(merged_sources)}; qualities: {', '.join(q for q in ('480p','720p','1080p') if merged.get(q))}")
            return merged
        return None

    async def _alternative_source_optimizer(self, target_identity, excluded_sources):
        """Find today's same episode from another provider after a torrent failure."""
        excluded_sources = set(excluded_sources or ())
        if "Nyaa" not in excluded_sources:
            feed = await self._rss_entries()
            if feed and feed.entries:
                data = await self._rss_optimizer(feed, "Nyaa", target_identity, allow_seen=True)
                if data:
                    return data
        if "AnimeTosho" not in excluded_sources:
            feed = await self._feed_from_url(self.ANIMETOSHO_RSS_ENDPOINT)
            if feed and feed.entries:
                data = await self._rss_optimizer(feed, "AnimeTosho", target_identity, allow_seen=True)
                if data:
                    return data
        if "TsukiHime" not in excluded_sources:
            data = await self._tsukihime_optimizer(target_identity)
            if data:
                return data
        if "acg" not in excluded_sources:
            data = await self._public_source_optimizer(
                self.ACG_RSS_ENDPOINT, "acg", target_identity,
            )
            if data:
                return data
        if "anibt" not in excluded_sources:
            data = await self._public_source_optimizer(
                self.ANIBT_RSS_ENDPOINT, "anibt", target_identity, allow_seen=True,
            )
            if data:
                return data
        if "Nekobt" not in excluded_sources:
            feed = await self._feed_from_url(self.NEKOBT_RSS_ENDPOINT.format(quote(self.query)))
            if feed and feed.entries:
                data = await self._rss_optimizer(feed, "Nekobt", target_identity, allow_seen=True)
                if data:
                    return data
        return None

    def defer_release(self, data):
        uid = data.get("uid")
        if not uid:
            return
        previous = self._pending.get(uid, {})
        attempts = int(previous.get("attempts", 0)) + 1
        # A dead Seedr/torrent must not monopolize the watcher forever.
        # Try one retry, then drop it and continue with newer releases.
        if attempts > 1:
            LOGS.warning(
                f"Dropping failed release after {attempts - 1} retry: {uid[:10]}"
            )
            self._pending.pop(uid, None)
            return
        self._pending[uid] = {
            "data": data,
            "attempts": attempts,
            "date": datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat(),
            "retry_at": datetime.now(timezone.utc).timestamp() + self.retry_delay,
        }
        LOGS.warning(
            f"Queued Nyaa release for one retry in 15 minutes (attempt {attempts}): {uid[:10]}"
        )

    def _data_identity(self, data):
        for key, item in data.items():
            if key == "uid" or not getattr(item, "title", None):
                continue
            parsed = anitopy.parse(str(item.title))
            anime = parsed.get("anime_title")
            episode = parsed.get("episode_number")
            if anime and episode and not isinstance(episode, (list, tuple)):
                return (
                    canonical_series_title(anime),
                    season_number_key(parsed.get("anime_season") or 1),
                    episode_number_key(episode),
                )
        return None

    async def _resolved_data_identity(self, data):
        for key, item in data.items():
            if key == "uid" or not getattr(item, "title", None):
                continue
            parsed = anitopy.parse(str(item.title))
            anime = parsed.get("anime_title")
            episode = parsed.get("episode_number")
            if anime and episode and not isinstance(episode, (list, tuple)):
                season = season_number_key(parsed.get("anime_season") or 1)
                series = await self._canonical_series_key(anime, season)
                return (series, season, episode_number_key(episode))
        return None

    @staticmethod
    def _data_provider(data):
        for key, item in data.items():
            if key != "uid" and getattr(item, "title", None):
                return str(getattr(item, "provider", "Nyaa"))
        return "Nyaa"

    async def on_new_anime(self, function):
        for cycle in count():
            claim_acquired = False
            identity = None
            try:
                data = await self.feed_optimizer()
                if data:
                    identity = await self._resolved_data_identity(data)
                    if identity:
                        today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
                        if await self.db.has_episode_uploaded(*identity):
                            LOGS.warning(f"Skipping previously uploaded episode before download: {identity[0]} S{identity[1]} E{identity[2]}")
                            await asyncio.sleep(30)
                            continue
                        if await self.db.is_daily_episode_claimed(today, identity[0], identity[2], identity[1]):
                            LOGS.warning(f"Skipping duplicate episode before download: {identity[0]} S{identity[1]} E{identity[2]}")
                            await asyncio.sleep(30)
                            continue
                        if await self.db.has_higher_daily_episode(today, identity[0], identity[2], identity[1]):
                            LOGS.warning(f"Skipping stale older episode after newer today's release: {identity[0]} S{identity[1]} E{identity[2]}")
                            await asyncio.sleep(30)
                            continue
                    failed_sources = self._failed_identities[identity] if identity else set()
                    provider = self._data_provider(data)
                    if identity and provider in failed_sources:
                        data = await self._alternative_source_optimizer(identity, failed_sources)
                        if not data:
                            LOGS.info(
                                f"No untried same-day source for {identity[0]} "
                                f"S{identity[1]} E{identity[2]} yet"
                            )
                            await asyncio.sleep(30)
                            continue
                        provider = self._data_provider(data)

                    if identity:
                        claim_acquired = await self.db.claim_episode_upload(*identity)
                        if not claim_acquired:
                            LOGS.warning(
                                f"Skipping episode already claimed by another worker: "
                                f"{identity[0]} S{identity[1]} E{identity[2]}"
                            )
                            await asyncio.sleep(30)
                            continue

                    original_uid = data.get("uid")
                    completed = False
                    while data:
                        completed = await function(data)
                        if completed:
                            # Persist both provider UID and normalized anime/episode identity.
                            await self.db.add_anime(data["uid"])
                            if identity:
                                await self.db.add_episode_uploaded(
                                    f"{identity[0]}|s{identity[1]}|e{identity[2]}"
                                )
                                claim_acquired = False
                            break
                        # Only retry another source when the torrent download itself
                        # failed. Upload/encoding/config errors are not source failures.
                        if not identity or not data.get("_download_failed"):
                            break
                        failed_sources = self._failed_identities[identity]
                        failed_sources.add(provider)
                        LOGS.warning(
                            f"{provider} torrent failed for {identity[0]} "
                            f"S{identity[1]} E{identity[2]}; trying another same-day source"
                        )
                        alternative = await self._alternative_source_optimizer(
                            identity, failed_sources,
                        )
                        if not alternative:
                            LOGS.warning(
                                f"No alternate same-day source available for {identity[0]} "
                                f"S{identity[1]} E{identity[2]}"
                            )
                            break
                        if await self._resolved_data_identity(alternative) != identity:
                            LOGS.warning("Rejected fallback release because episode identity changed")
                            break
                        data = alternative
                        provider = self._data_provider(data)
                    if not completed:
                        self._pending.pop(original_uid, None)
                        if claim_acquired and identity:
                            await self.db.release_episode_claim(*identity)
                            claim_acquired = False
            except KeyboardInterrupt:
                self._exit()
            except Exception:
                # A processing claim is kept on unexpected exceptions: a Telegram
                # send may have succeeded just before the error, so releasing it
                # could publish the same episode twice.
                LOGS.error(f"[Nyaa dual-audio loop] Error in cycle {cycle}: {format_exc()}")
            await asyncio.sleep(30)
