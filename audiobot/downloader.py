from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

MAX_SOURCE_BYTES = 300 * 1024 * 1024
PREFERRED_AUDIO_QUALITY = "192"
SHRINK_BITRATES = (128, 96, 64, 48, 32)
TEMP_SUFFIXES = {".part", ".ytdl", ".temp", ".json"}


class DownloadFailed(Exception):
    pass


@dataclass(frozen=True)
class AudioResult:
    path: Path
    title: str
    duration: int | None
    uploader: str | None
    webpage_url: str
    video_id: str


class AudioDownloader:
    def __init__(
        self,
        *,
        cookies_file: Path | None = None,
        js_runtimes: dict[str, dict] | None = None,
    ) -> None:
        self.cookies_file = cookies_file
        self.js_runtimes = dict(js_runtimes or {})

    def build_options(self, out_dir: Path) -> dict:
        options = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "playlist_items": "1",
            "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
            "windowsfilenames": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "no_color": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "max_filesize": MAX_SOURCE_BYTES,
            "prefer_ffmpeg": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": PREFERRED_AUDIO_QUALITY,
                }
            ],
        }

        if self.cookies_file:
            options["cookiefile"] = str(self.cookies_file)
        if self.js_runtimes:
            options["js_runtimes"] = {
                name: dict(config) for name, config in self.js_runtimes.items()
            }
        return options

    def download(self, url: str, out_dir: Path) -> AudioResult:
        try:
            with yt_dlp.YoutubeDL(self.build_options(out_dir)) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            raise DownloadFailed(str(exc)) from exc

        info = self._select_video(info)
        audio_path = self._find_audio(out_dir)
        return AudioResult(
            path=audio_path,
            title=info.get("title") or "ไม่ทราบชื่อเพลง",
            duration=info.get("duration"),
            uploader=info.get("uploader") or info.get("channel"),
            webpage_url=info.get("webpage_url") or url,
            video_id=str(info.get("id") or "audio"),
        )

    @staticmethod
    def _select_video(info: dict | None) -> dict:
        if not info:
            raise RuntimeError("ไม่พบข้อมูลวิดีโอ")

        entries = info.get("entries")
        if entries:
            entries = [entry for entry in entries if entry]
            if not entries:
                raise RuntimeError("ไม่พบวิดีโอในลิงก์นี้")
            info = entries[0]

        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise RuntimeError("ไม่รองรับไลฟ์สดหรือวิดีโอที่ยังไม่ออกอากาศ")

        return info

    @staticmethod
    def _find_audio(out_dir: Path) -> Path:
        files = [
            path
            for path in out_dir.iterdir()
            if path.is_file() and path.suffix.lower() not in TEMP_SUFFIXES
        ]
        mp3_files = [path for path in files if path.suffix.lower() == ".mp3"]
        candidates = mp3_files or files
        if not candidates:
            raise RuntimeError("ดาวน์โหลดแล้วแต่ไม่พบไฟล์เสียง")
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def shrink(self, path: Path, limit: int) -> Path | None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        for kbps in SHRINK_BITRATES:
            out_path = path.with_name(f"{path.stem}_{kbps}k.mp3")
            try:
                proc = subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(path),
                        "-vn",
                        "-b:a",
                        f"{kbps}k",
                        str(out_path),
                    ],
                    capture_output=True,
                    check=False,
                    text=True,
                    creationflags=flags,
                )
            except OSError:
                return None

            if proc.returncode == 0 and out_path.exists():
                if out_path.stat().st_size <= limit:
                    return out_path
                out_path.unlink(missing_ok=True)

        return None
