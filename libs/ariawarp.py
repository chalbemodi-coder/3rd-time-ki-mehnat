import asyncio
import re
import time
from telethon.errors.rpcerrorlist import MessageNotModifiedError
from libs.logger import LOGS


class Torrent:
    def __init__(self) -> None:
        # Wait up to 15 minutes for late seeds/peers and use DHT/LPD/tracker retries.
        self.cmd = (
            "aria2c '''{link}''' -x 10 -j 10 --seed-time=0 "
            "--bt-stop-timeout=120 --bt-tracker-connect-timeout=15 "
            "--bt-tracker-interval=60 --bt-enable-lpd=false --enable-dht=false --disable-ipv6=true "
            "--timeout=30 --connect-timeout=15 "
            "--max-tries=1 --retry-wait=0 --summary-interval=1 "
            "--show-console-readout=false -d '{path}'"
        )

    async def bash(self, cmd, reporter=None):
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        last_update = 0
        diagnostics = []
        while True:
            line_bytes = await process.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode(errors="ignore").strip()
            lower_line = line.lower()
            if any(
                marker in lower_line
                for marker in ("error", "exception", "failed", "invalid", "ambiguous", "usage:", "did you mean")
            ):
                safe_line = re.sub(r"(?i)magnet:\?[^\s]+", "magnet:<redacted>", line)
                diagnostics.append(safe_line[:320])
            if reporter and ("SPD:" in line or "DL:" in line):
                now = time.time()
                if now - last_update >= 8:
                    per_match = re.search(r"\((\d+)%\)", line)
                    size_match = re.search(r"([\d.]+\w+)\s*/\s*([\d.]+\w+)", line)
                    eta_match = re.search(r"ETA:([^\s\]]+)", line)
                    spd = re.search(r"SPD:([\d.]+\w+)", line)
                    dl = re.search(r"DL:([\d.]+\w+)", line)
                    per = int(per_match.group(1)) if per_match else 0
                    size_info = (
                        f"{size_match.group(1)} / {size_match.group(2)}"
                        if size_match else "Unknown"
                    )
                    speed_info = spd.group(1) if spd else (dl.group(1) if dl else "0B/s")
                    if not speed_info.endswith("/s"):
                        speed_info += "/s"
                    eta_info = eta_match.group(1) if eta_match else "∞"
                    blocks = min(20, per // 5)
                    progress_bar = "`[{0}{1}] {2}%\n\n`".format(
                        "●" * blocks, " " * (20 - blocks), per
                    )
                    text = (
                        "**📥 Downloading Anime...**\n\n"
                        f"**Name:** `{reporter.file_name}`\n"
                        f"**Size:** `{size_info}`\n"
                        f"**Speed:** `{speed_info}`\n"
                        f"**ETA:** `{eta_info}`\n\n"
                        f"**Progress:**\n{progress_bar}"
                    )
                    try:
                        if getattr(reporter, "msg", None):
                            await reporter.msg.edit(text)
                        else:
                            await reporter.edit(text)
                    except MessageNotModifiedError:
                        pass
                    except Exception as error:
                        LOGS.error(f"Aria progress edit failed: {error}")
                    last_update = now
        await process.wait()
        if process.returncode != 0:
            detail = " | ".join(diagnostics[-8:]) or "aria2 returned a non-zero exit status without diagnostic text"
            LOGS.error(f"aria2 failed with exit code {process.returncode}: {detail}")
        return process.returncode == 0

    async def download_magnet(self, link: str, path: str, reporter=None):
        return await self.bash(self.cmd.format(link=link, path=path), reporter)
