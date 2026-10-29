from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWED_CHANNEL_ID = 1564276697551872030
DEFAULT_REQUIRED_ROLE_ID = 1526986518995144705

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_channel_id: int
    required_role_id: int
    cookies_file: Path | None
    js_runtimes: dict[str, dict]


def find_cookies_file(base_dir: Path = BASE_DIR) -> Path | None:
    configured = os.getenv("YTDL_COOKIES", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = base_dir / path
        if path.is_file():
            return path
        log.warning("ไม่พบไฟล์ cookie ที่ตั้งไว้: %s", path)
        return None

    default = base_dir / "cookies.txt"
    return default if default.is_file() else None


def detect_js_runtimes() -> dict[str, dict]:
    runtimes: dict[str, dict] = {}
    if shutil.which("node"):
        runtimes["node"] = {}
    if shutil.which("deno"):
        runtimes["deno"] = {}
    return runtimes


def _env_id(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} ต้องเป็นตัวเลข") from exc


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    return Settings(
        token=os.getenv("DISCORD_TOKEN", "").strip(),
        allowed_channel_id=_env_id("ALLOWED_CHANNEL_ID", DEFAULT_ALLOWED_CHANNEL_ID),
        required_role_id=_env_id("REQUIRED_ROLE_ID", DEFAULT_REQUIRED_ROLE_ID),
        cookies_file=find_cookies_file(),
        js_runtimes=detect_js_runtimes(),
    )
