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

import asyncio
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import time
from traceback import format_exc

import aiofiles
import aiohttp
import requests
from html_telegraph_poster import TelegraphPoster
from telethon.errors.rpcerrorlist import MessageNotModifiedError
from libs.anime_identity import canonical_channel_title, canonical_series_title
from libs.subtitle_tracks import subtitle_mapping_args

from functions.config import Var
from libs.logger import LOGS

ENCODE_POLL_INTERVAL = 5


class Tools:
    def __init__(self):
        pass

    async def async_searcher(
        self,
        url: str,
        post: bool = None,
        headers: dict = None,
        params: dict = None,
        json: dict = None,
        data: dict = None,
        ssl=None,
        re_json: bool = False,
        re_content: bool = False,
        real: bool = False,
        *args,
        **kwargs,
    ):
        async with aiohttp.ClientSession(headers=headers) as client:
            if post:
                data = await client.post(
                    url, json=json, data=data, ssl=ssl, *args, **kwargs
                )
            else:
                data = await client.get(url, params=params, ssl=ssl, *args, **kwargs)
            if re_json:
                return await data.json()
            if re_content:
                return await data.read()
            if real:
                return data
            return await data.text()

    async def cover_dl(self, link):
        try:
            if not link:
                return None
            async with aiohttp.ClientSession() as client:
                response = await client.get(link, timeout=aiohttp.ClientTimeout(total=20))
                if response.status != 200:
                    return None
                content_type = (response.headers.get("Content-Type") or "").lower()
                if content_type and not content_type.startswith("image/"):
                    return None
                image = await response.read()
            if not image:
                return None
            clean_link = link.split("?")[0]
            filename = clean_link.split("/")[-1]
            if len(filename) > 30 or not filename.lower().endswith(
                (".jpg", ".png", ".jpeg")
            ):
                filename = hashlib.md5(link.encode()).hexdigest() + ".jpg"
            fn = f"thumbs/{filename}"
            async with aiofiles.open(fn, "wb") as file:
                await file.write(image)
            # Telegram poster layout: keep the older wide presentation across all bots.
            # Normalize every source cover to 16:9 instead of sending a tall raw poster.
            wide_fn = f"thumbs/.wide-{hashlib.md5(link.encode()).hexdigest()}.jpg"
            try:
                process = await asyncio.create_subprocess_exec(
                    Var.FFMPEG,
                    "-hide_banner", "-loglevel", "error", "-y",
                    "-i", fn,
                    "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,setsar=1",
                    "-frames:v", "1", "-q:v", "2", wide_fn,
                )
                if await process.wait() == 0 and os.path.isfile(wide_fn) and os.path.getsize(wide_fn) > 0:
                    return wide_fn
            except Exception as resize_error:
                LOGS.warning(f"Wide poster normalization failed; using source cover: {resize_error}")
            return fn
        except Exception as error:
            LOGS.exception(format_exc())
            LOGS.error(str(error))

    async def mediainfo(self, file, bot):
        try:
            process = await asyncio.create_subprocess_shell(
                f"mediainfo '''{file}''' --Output=HTML",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            out = stdout.decode()
            client = TelegraphPoster(use_api=True)
            client.create_api_token("Mediainfo")
            page = client.post(
                title="Mediainfo",
                author=((await bot.get_me()).first_name),
                author_url=f"https://t.me/{((await bot.get_me()).username)}",
                text=out,
            )
            return page.get("url")
        except Exception as error:
            LOGS.exception(format_exc())
            LOGS.error(str(error))

    async def _poster(self, bot, anime_info, channel_id=None, buttons=None, use_bot=False):
        thumb = await self.cover_dl((await anime_info.get_cover()))
        if not thumb:
            thumb = await self.cover_dl((await anime_info.get_poster()))
        caption = await anime_info.get_caption()
        return await bot.upload_poster(
            thumb or "assest/poster_not_found.jpg",
            caption,
            channel_id if channel_id else None,
            buttons=buttons,
            use_bot=True,
        )

    async def _most_established_channel(self, candidates):
        """Choose the most populated existing alias channel; tie order is stable."""
        if len(candidates) < 2 or not Var.BOT_TOKEN:
            return candidates[0] if candidates else {}
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async def member_count(chat_info):
                chat_id = chat_info.get("chat_id")
                if not chat_id:
                    return -1
                try:
                    url = f"https://api.telegram.org/bot{Var.BOT_TOKEN}/getChatMemberCount"
                    async with session.get(url, params={"chat_id": chat_id}) as response:
                        result = await response.json()
                    value = result.get("result") if result.get("ok") else None
                    return int(value) if value is not None else -1
                except Exception:
                    return -1

            counts = await asyncio.gather(*(member_count(item) for item in candidates))
        best_index = max(range(len(candidates)), key=lambda index: counts[index])
        return candidates[best_index]

    async def _channel_matches_title(self, bot, chat_info, expected_title):
        """Reject stale mappings when a Telegram channel was renamed to another anime."""
        chat_id = (chat_info or {}).get("chat_id")
        if not chat_id:
            return False
        try:
            chat = await bot.pyro_client.get_chat(chat_id)
            actual = canonical_series_title(str(getattr(chat, "title", "")))
            expected = canonical_series_title(expected_title)
            return actual == expected
        except Exception:
            return False

    async def _valid_channel_candidates(self, bot, candidates, expected_title):
        valid = []
        for candidate in candidates:
            if await self._channel_matches_title(bot, candidate, expected_title):
                valid.append(candidate)
        return valid

    async def get_chat_info(self, bot, anime_info, dB):
        try:
            data = anime_info.data or {}
            raw_title = str(data.get("anime_title") or anime_info.proper_name or anime_info.name)
            english_title = str(await anime_info.get_english() or raw_title).strip()
            channel_title = canonical_channel_title(english_title or raw_title)
            channel_key = channel_title
            chat_info = await dB.get_anime_channel_info(channel_key)
            if chat_info and not await self._channel_matches_title(bot, chat_info, channel_title):
                chat_info = {}
            if not chat_info:
                candidates = await dB.get_anime_channel_candidates_for_series(channel_title)
                if not candidates and raw_title.casefold() != english_title.casefold():
                    candidates = await dB.get_anime_channel_candidates_for_series(raw_title)
                candidates = await self._valid_channel_candidates(bot, candidates, channel_title)
                chat_info = await self._most_established_channel(candidates)
            if not chat_info:
                chat_id = await bot.create_channel(
                    channel_title,
                    (await self.cover_dl((await anime_info.get_poster()))),
                )
                chat_info = {"chat_id": chat_id, "invite_link": ""}
                # Persist the created chat before invite creation so a transient
                # invite API error cannot make the next source create another chat.
                await dB.add_anime_channel_info(channel_key, chat_info)
            if chat_info.get("chat_id") and not chat_info.get("invite_link"):
                chat_id = chat_info["chat_id"]
                invite_link = await bot.generate_invite_link(chat_id)
                chat_info["invite_link"] = invite_link
            # Persist the canonical alias so all sources/episodes reuse one chat.
            await dB.add_anime_channel_info(channel_key, chat_info)
            return chat_info
        except BaseException:
            LOGS.error(str(format_exc()))

    def init_dir(self):
        if not os.path.exists("thumb.jpg"):
            content = requests.get(Var.THUMB).content
            with open("thumb.jpg", "wb") as f:
                f.write(content)
        if not os.path.isdir("encode/"):
            os.mkdir("encode/")
        if not os.path.isdir("thumbs/"):
            os.mkdir("thumbs/")
        if not os.path.isdir("downloads/"):
            os.mkdir("downloads/")

    def hbs(self, size):
        if not size:
            return ""
        power = 2**10
        raised_to_pow = 0
        dict_power_n = {0: "B", 1: "K", 2: "M", 3: "G", 4: "T", 5: "P"}
        while size > power:
            size /= power
            raised_to_pow += 1
        return str(round(size, 2)) + " " + dict_power_n[raised_to_pow] + "B"

    def ts(self, milliseconds: int) -> str:
        seconds, milliseconds = divmod(int(milliseconds), 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        tmp = (
            ((str(days) + "d:") if days else "")
            + ((str(hours) + "h:") if hours else "")
            + ((str(minutes) + "m:") if minutes else "")
            + ((str(seconds) + "s:") if seconds else "")
            + ((str(milliseconds) + "ms:") if milliseconds else "")
        )
        return tmp[:-1]

    async def rename_file(self, dl, out):
        try:
            os.rename(dl, out)
        except BaseException:
            return False, format_exc()
        return True, out

    async def bash_(self, cmd, run_code=0):
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        err = stderr.decode().strip() or None
        out = stdout.decode().strip()
        if not run_code and err:
            if match := re.match("\\/bin\\/sh: (.*): ?(\\w+): not found", err):
                return out, f"{match.group(2).upper()}_NOT_FOUND"
        return out, err

    async def frame_counts(self, dl):
        try:
            _x, _y = await self.bash_(
                f'mediainfo --fullscan """{dl}""" | grep "Frame count"'
            )
            if _y and _y.endswith("NOT_FOUND"):
                LOGS.error(f"ERROR: `{_y}`")
            elif _x and ":" in _x:
                try:
                    count_str = _x.split(":")[1].strip().split("\n")[0].strip()
                    count_str = re.sub(r"\D", "", count_str)
                    if count_str.isdigit():
                        return int(count_str)
                except Exception:
                    pass
        except Exception as e:
            LOGS.error(f"mediainfo frame count failed: {e}")

        try:
            _x, _ = await self.bash_(
                f'ffprobe -v error -select_streams v:0 -count_packets -show_entries stream=nb_read_packets -of csv=p=0 """{dl}"""'
            )
            _x = _x.strip()
            if _x.isdigit():
                return int(_x)
        except Exception:
            pass

        try:
            duration = await self.genss(dl)
            if duration and duration > 0:
                return int(duration * 23.976)
        except Exception:
            pass

        return 27000

    async def compress(self, dl, out, log_msg, target_height=None, max_size_mb=None):
        total_frames = await self.frame_counts(dl)
        if not total_frames:
            return False, "Unable to Count The Frames!"
        _progress = f"progress-{time.time()}.txt"
        # Dual bot has no VIDEO_HEIGHT config; executor passes the quality profile.
        target_height = int(target_height or 720)
        duration = max(1, await self.genss(dl))
        # Use the explicit per-quality limit; fall back to 400 MB for Dual.
        size_limit_mb = float(max_size_mb or 400)
        total_budget_k = (size_limit_mb * 1024 * 8 * 0.90) / duration
        # Auto repo's AUDIO_BITRATE_K is not present in Dual config.
        audio_k = max(24, min(192, int(total_budget_k * 0.30)))
        video_k = max(24, int(total_budget_k - (audio_k * 2)))
        subtitle_args = shlex.join(subtitle_mapping_args(dl))
        cmd = f'''{
            Var.FFMPEG} -hide_banner -loglevel quiet -progress """{_progress}""" -i """{dl}""" -metadata "Encoded By"="https://github.com/chalbemodi-coder/Auto/" -preset fast -c:v libx264 -b:v {video_k}k -maxrate {video_k}k -bufsize {video_k * 2}k -vf "scale=-2:min({target_height}\\,ih),format=yuv420p" -map 0:v -c:a aac -b:a {audio_k}k -map 0:a -c:s copy {subtitle_args} """{out}""" -y'''
        process = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        wait_task = asyncio.create_task(process.wait())
        stderr_task = asyncio.create_task(process.stderr.read())
        d_time = time.time()
        _new_log_msg = log_msg
        elapse = 0
        while not wait_task.done():
            await asyncio.sleep(ENCODE_POLL_INTERVAL)

            # ffmpeg init is slow in low end server so, hehe :)
            if (time.time() - d_time) > 60:
                if not os.path.exists(out) or os.path.getsize(out) == 0:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    await wait_task
                    await stderr_task
                    try:
                        os.remove(out)
                    except OSError:
                        pass
                    try:
                        os.remove(_progress)
                    except OSError:
                        pass
                    return (
                        False,
                        "Unable To Encode This Video (Process timed out with 0-byte output file)!",
                    )

            if not os.path.exists(_progress):
                continue

            try:
                with open(_progress, "r") as fil:
                    text = fil.read()
            except Exception:
                continue

            frames = re.findall("frame=(\\d+)", text)
            size = re.findall("total_size=(\\d+)", text)
            speed = 0

            if len(frames):
                elapse = int(frames[-1])
            if len(size):
                size = int(size[-1])
                per = elapse * 100 / int(total_frames)
                time_diff = time.time() - int(d_time)
                speed = round(elapse / time_diff, 2)
            if int(speed) != 0:
                some_eta = ((int(total_frames) - elapse) / speed) * 1000
                file_name = dl.rsplit("/", 1)[-1]
                text = (
                    f"**Successfully Downloaded The Anime**\n\n"
                    f"**File Name:** ```{file_name}```\n\n"
                    f"**STATUS:** \n"
                )
                progress_str = "`[{0}{1}] {2}%\n\n`".format(
                    "".join("●" for _ in range(math.floor(per / 5))),
                    "".join("" for _ in range(20 - math.floor(per / 5))),
                    round(per, 2),
                )
                e_size = f"{self.hbs(size)} of ~{self.hbs((size / per) * 100)}"
                eta = f"~{self.ts(some_eta)}"
                if log_msg:
                    try:
                        _new_log_msg = await log_msg.edit(
                            text + progress_str + "`" + e_size + "`" + "\n\n`" + eta + "`"
                        )
                    except MessageNotModifiedError:
                        pass
        return_code = await wait_task
        stderr_output = await stderr_task
        if return_code != 0:
            detail = stderr_output.decode(errors="ignore").strip()
            try:
                os.remove(out)
            except OSError:
                pass
            try:
                os.remove(_progress)
            except OSError:
                pass
            message = f"Unable To Encode This Video (FFmpeg exited with code {return_code})"
            if detail:
                message += f": {detail[-300:]}"
            return False, message
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            try:
                os.remove(out)
            except OSError:
                pass
            try:
                os.remove(_progress)
            except OSError:
                pass
            return False, "Unable To Encode This Video (FFmpeg produced no output file)!"
        try:
            os.remove(_progress)
        except OSError:
            pass
        return True, _new_log_msg

    async def genss(self, file):
        try:
            process = subprocess.Popen(
                [shutil.which("mediainfo"), file, "--Output=JSON"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            stdout, stderr = process.communicate()
            out = stdout.decode().strip()
            z = json.loads(out)

            tracks = z.get("media", {}).get("track")
            if not tracks:
                tracks = []

            duration_str = None
            for track in tracks:
                if track and "Duration" in track:
                    duration_str = str(track["Duration"])
                    break

            if duration_str:
                try:
                    return int(float(duration_str))
                except Exception:
                    pass
                if "." in duration_str:
                    try:
                        return int(duration_str.split(".")[0])
                    except Exception:
                        pass

            try:
                proc = subprocess.Popen(
                    [
                        shutil.which("ffprobe"),
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        file,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                stdout, _ = proc.communicate()
                f_dur = stdout.decode().strip()
                if f_dur:
                    return int(float(f_dur))
            except Exception:
                pass
        except Exception as e:
            LOGS.error(f"[genss] Error parsing duration: {e}")

        return 1140  # assume 19 min playback duration

    def stdr(self, seconds: int) -> str:
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if len(str(minutes)) == 1:
            minutes = "0" + str(minutes)
        if len(str(hours)) == 1:
            hours = "0" + str(hours)
        if len(str(seconds)) == 1:
            seconds = "0" + str(seconds)
        dur = (
            ((str(hours) + ":") if hours else "00:")
            + ((str(minutes) + ":") if minutes else "00:")
            + ((str(seconds)) if seconds else "")
        )
        return dur

    async def duration_s(self, file):
        tsec = await self.genss(file)
        x = round(tsec / 5)
        y = round(tsec / 5 + 30)
        pin = self.stdr(x)
        if y < tsec:
            pon = self.stdr(y)
        else:
            pon = self.stdr(tsec)
        return pin, pon

    async def gen_ss_sam(self, _hash, filename):
        try:
            ss_path, sp_path = None, None
            os.mkdir(_hash)
            tsec = await self.genss(filename)
            fps = 10 / tsec
            ncmd = f"ffmpeg -i '{filename}' -vf fps={fps} -vframes 10 '{_hash}/pic%01d.png'"
            process = await asyncio.create_subprocess_shell(
                ncmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()
            ss, dd = await self.duration_s(filename)
            __ = filename.split(".mkv")[-2]
            out = __ + "_sample.mkv"
            _ncmd = f'ffmpeg -i """{filename}""" -preset fast -ss {ss} -to {dd} -c:v libx265 -crf 27 -map 0:v -c:a aac -map 0:a -c:s copy -map 0:s? """{out}""" -y'
            process = await asyncio.create_subprocess_shell(
                _ncmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            er = stderr.decode().strip()
            try:
                if er:
                    if not os.path.exists(out) or os.path.getsize(out) == 0:
                        LOGS.error(str(er))
                        return (ss_path, sp_path)
            except BaseException:
                pass
            return _hash, out
        except Exception as error:
            LOGS.error(str(error))
            LOGS.exception(format_exc())
