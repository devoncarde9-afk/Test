from __future__ import annotations

import re
from urllib.parse import urlsplit
from uuid import uuid4

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def format_duration(seconds) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "ไม่ทราบ"
    if total <= 0:
        return "ไม่ทราบ"

    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def normalize_url(raw: str) -> str | None:
    link = raw.strip().strip("<>").strip()
    if not link:
        return None

    candidate = link if _SCHEME_RE.match(link) else f"https://{link}"
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not hostname:
        return None
    return candidate


def safe_filename(title: str, extension: str = ".mp3") -> str:
    name = _INVALID_FILENAME_RE.sub(" ", title or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:80] or "audio"
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"{name}{ext}"


def unique_ogg_filename() -> str:
    return f"{uuid4().hex}.ogg"


def clean_error_message(message: str, limit: int = 300) -> str:
    text = _ANSI_RE.sub("", message).strip()
    text = text.replace("ERROR: ", "")
    return text[:limit]
