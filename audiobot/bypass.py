from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from audiobot.audio import AudioProcessingError

BASE_DIR = Path(__file__).resolve().parent.parent
INTRO_PATH = BASE_DIR / "bait" / "russia.ogg"
OUTRO_PATH = BASE_DIR / "bait" / "bait หลัง.ogg"
MAX_OUTPUT_SECONDS = 6 * 60 + 59
TRIM_MARGIN_SECONDS = 0.10
FALLBACK_BIT_RATE = 128000


@dataclass(frozen=True)
class AudioProfile:
    duration: float
    codec: str
    sample_rate: int
    channels: int
    bit_rate: int | None
    encoder: str
    extension: str


def _positive_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _run(command: list[str], timeout: int = 240) -> None:
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "คำสั่งประมวลผลเสียงล้มเหลว").strip()
        raise AudioProcessingError(detail[-1200:])


def _ffmpeg() -> str:
    value = shutil.which("ffmpeg")
    if not value:
        raise AudioProcessingError("ไม่พบ ffmpeg ในระบบ")
    return value


def _ffprobe() -> str:
    value = shutil.which("ffprobe")
    if not value:
        raise AudioProcessingError("ไม่พบ ffprobe ในระบบ")
    return value


def probe_audio(path: Path) -> AudioProfile:
    if not path.is_file() or path.stat().st_size == 0:
        raise AudioProcessingError(f"ไม่พบไฟล์เสียงหรือไฟล์ว่าง: {path.name}")
    proc = subprocess.run(
        [_ffprobe(), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if proc.returncode:
        raise AudioProcessingError((proc.stderr or "อ่านข้อมูลเสียงไม่สำเร็จ")[-1000:])
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AudioProcessingError("อ่านข้อมูลเสียงไม่สำเร็จ") from exc
    streams = data.get("streams") or []
    stream = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if stream is None:
        raise AudioProcessingError(f"ไม่พบแทร็กเสียงใน {path.name}")
    fmt = data.get("format") or {}
    duration = _positive_float(fmt.get("duration")) or _positive_float(stream.get("duration"))
    sample_rate = _positive_int(stream.get("sample_rate"))
    channels = _positive_int(stream.get("channels"))
    if duration is None or sample_rate is None or channels is None:
        raise AudioProcessingError(f"อ่านความยาวหรือรูปแบบเสียงไม่ได้: {path.name}")
    codec = str(stream.get("codec_name") or "")
    formats = set(str(fmt.get("format_name") or "").split(","))
    if codec == "vorbis" and "ogg" in formats:
        encoder, extension = "libvorbis", ".ogg"
    elif codec == "opus" and "ogg" in formats:
        encoder, extension = "libopus", ".ogg"
    elif codec == "mp3" and "mp3" in formats:
        encoder, extension = "libmp3lame", ".mp3"
    elif codec == "aac" and ("aac" in formats or "mov" in formats or "mp4" in formats):
        encoder, extension = "aac", ".aac"
    else:
        raise AudioProcessingError(f"ไม่รองรับเสียงชนิด {codec or 'unknown'} ใน {path.name}; รองรับ OGG/Vorbis, OGG/Opus, MP3 และ AAC")
    bitrate = _positive_int(stream.get("bit_rate")) or _positive_int(fmt.get("bit_rate"))
    return AudioProfile(duration, codec, sample_rate, channels, bitrate, encoder, extension)


def _encode_to_profile(source: Path, destination: Path, profile: AudioProfile) -> None:
    args = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-map_metadata", "-1", "-c:a", profile.encoder,
        "-ar", str(profile.sample_rate), "-ac", str(profile.channels),
        "-b:a", str(profile.bit_rate or FALLBACK_BIT_RATE),
    ]
    if profile.extension == ".aac":
        args += ["-f", "adts"]
    args.append(str(destination))
    _run(args)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise AudioProcessingError(f"แปลงไฟล์เสียงไม่สำเร็จ: {source.name}")
    probe_audio(destination)


def _trim_song(source: Path, destination: Path, seconds: float, profile: AudioProfile) -> None:
    args = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-t", f"{seconds:.3f}", "-map", "0:a:0",
        "-vn", "-sn", "-dn", "-map_metadata", "-1", "-c:a", "copy",
    ]
    if profile.extension == ".aac":
        args += ["-f", "adts"]
    args.append(str(destination))
    _run(args)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise AudioProcessingError("ตัดเพลงไม่สำเร็จ")


def _concat_audio(parts: list[Path], output_path: Path, profile: AudioProfile) -> None:
    # FFmpeg's concat demuxer handles container headers/timestamps correctly;
    # raw byte concatenation and hand-edited OGG pages can create invalid files.
    list_path = output_path.with_name(f"concat_{uuid4().hex}.txt")
    try:
        lines = []
        for part in parts:
            if not part.is_file() or part.stat().st_size == 0:
                raise AudioProcessingError(f"ไฟล์สำหรับรวมไม่พบหรือว่าง: {part.name}")
            escaped = str(part.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        args = [
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-map", "0:a:0", "-vn", "-c:a", "copy",
        ]
        if profile.extension == ".aac":
            args += ["-f", "adts"]
        args.append(str(output_path))
        _run(args, timeout=300)
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise AudioProcessingError("รวมไฟล์เสียงไม่สำเร็จ")
        probe_audio(output_path)
    finally:
        list_path.unlink(missing_ok=True)


def process_bypass(
    input_path: Path,
    output_dir: Path,
    intro_path: Path = INTRO_PATH,
    outro_path: Path = OUTRO_PATH,
    max_output_seconds: float = MAX_OUTPUT_SECONDS,
) -> Path:
    """Place the intro before the supplied audio and the outro after it."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for asset in (intro_path, outro_path):
        if not asset.is_file():
            raise AudioProcessingError(f"ไม่พบไฟล์เสียงประกอบ: {asset}")
    source = probe_audio(input_path)
    output_path = output_dir / f"{uuid4().hex}{source.extension}"
    intro_output = output_dir / f"intro_{uuid4().hex}{source.extension}"
    outro_output = output_dir / f"outro_{uuid4().hex}{source.extension}"
    song_path = input_path
    temporary: list[Path] = [intro_output, outro_output]
    try:
        _encode_to_profile(intro_path, intro_output, source)
        _encode_to_profile(outro_path, outro_output, source)
        max_song_duration = max_output_seconds - probe_audio(intro_output).duration - probe_audio(outro_output).duration
        if max_song_duration <= 0:
            raise AudioProcessingError("ไฟล์ intro/outro ยาวเกินขีดจำกัด 6:59")
        if source.duration > max_song_duration:
            song_path = output_dir / f"song_{uuid4().hex}{source.extension}"
            temporary.append(song_path)
            _trim_song(input_path, song_path, max(0.1, max_song_duration - TRIM_MARGIN_SECONDS), source)
        _concat_audio([intro_output, song_path, outro_output], output_path, source)
        if probe_audio(output_path).duration > max_output_seconds + 0.25:
            raise AudioProcessingError("ไฟล์ผลลัพธ์ยาวเกินขีดจำกัด 6:59")
        return output_path
    except subprocess.TimeoutExpired as exc:
        raise AudioProcessingError("ประมวลผลเสียงใช้เวลานานเกินกำหนด") from exc
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
