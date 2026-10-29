from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from audiobot.audio import AudioProcessingError

BASE_DIR = Path(__file__).resolve().parent.parent
INTRO_PATH = BASE_DIR / "bait" / "russia.ogg"
OUTRO_PATH = BASE_DIR / "bait" / "bait หลัง.ogg"
MAX_OUTPUT_SECONDS = 6 * 60 + 59
TRIM_MARGIN_SECONDS = 0.05
FALLBACK_BIT_RATE = 128000
OGG_CAPTURE_PATTERN = b"OggS"
OGG_CRC_POLYNOMIAL = 0x04C11DB7
OGG_EOS_FLAG = 0x04


@dataclass(frozen=True)
class AudioProfile:
    duration: float
    codec: str
    sample_rate: int
    channels: int
    bit_rate: int | None
    encoder: str
    extension: str


def _read_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _read_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _profile_codec(codec: str, format_name: str) -> tuple[str, str]:
    formats = set(format_name.split(","))
    if codec == "vorbis" and "ogg" in formats:
        return "libvorbis", ".ogg"
    if codec == "opus" and "ogg" in formats:
        return "libopus", ".ogg"
    if codec == "mp3" and "mp3" in formats:
        return "libmp3lame", ".mp3"
    if codec == "aac" and "aac" in formats:
        return "aac", ".aac"
    raise AudioProcessingError("รองรับเฉพาะไฟล์ OGG, MP3 และ AAC")


def probe_audio(path: Path) -> AudioProfile:
    if not path.is_file():
        raise AudioProcessingError(f"ไม่พบไฟล์เสียง: {path.name}")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise AudioProcessingError("ไม่พบ ffprobe")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        raise AudioProcessingError(detail[-500:] or "อ่านข้อมูลเสียงไม่สำเร็จ")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AudioProcessingError("อ่านข้อมูลเสียงไม่สำเร็จ") from exc

    streams = data.get("streams") or []
    stream = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )
    if stream is None:
        raise AudioProcessingError("ไม่พบแทร็กเสียง")

    format_data = data.get("format") or {}
    duration = _read_float(format_data.get("duration"))
    if duration is None:
        duration = _read_float(stream.get("duration"))
    if duration is None:
        raise AudioProcessingError("ไม่สามารถอ่านความยาวเสียงได้")

    codec = str(stream.get("codec_name") or "")
    sample_rate = _read_int(stream.get("sample_rate"))
    channels = _read_int(stream.get("channels"))
    if sample_rate is None or channels is None:
        raise AudioProcessingError("ไม่สามารถอ่านรูปแบบเสียงได้")

    bit_rate = _read_int(stream.get("bit_rate"))
    if bit_rate is None:
        bit_rate = _read_int(format_data.get("bit_rate"))

    encoder, extension = _profile_codec(
        codec,
        str(format_data.get("format_name") or ""),
    )
    return AudioProfile(
        duration=duration,
        codec=codec,
        sample_rate=sample_rate,
        channels=channels,
        bit_rate=bit_rate,
        encoder=encoder,
        extension=extension,
    )


def _run_ffmpeg(args: list[str], output_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioProcessingError("ไม่พบ ffmpeg")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            *args,
        ],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    if proc.returncode != 0 or not output_path.is_file():
        detail = (proc.stderr or "").strip()
        raise AudioProcessingError(detail[-500:] or "ประมวลผลเสียงไม่สำเร็จ")
    if output_path.stat().st_size == 0:
        raise AudioProcessingError("ไฟล์เสียงที่ได้ว่างเปล่า")


def _matches_profile(profile: AudioProfile, target: AudioProfile) -> bool:
    return (
        profile.codec == target.codec
        and profile.sample_rate == target.sample_rate
        and profile.channels == target.channels
    )


def _encode_bait(
    source_path: Path,
    output_path: Path,
    profile: AudioProfile,
) -> AudioProfile:
    args = [
        "-i",
        str(source_path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-c:a",
        profile.encoder,
        "-ar",
        str(profile.sample_rate),
        "-ac",
        str(profile.channels),
    ]
    bit_rate = profile.bit_rate or FALLBACK_BIT_RATE
    if profile.encoder in {"libvorbis", "libopus", "libmp3lame", "aac"}:
        args.extend(["-b:a", str(bit_rate)])
    if profile.encoder == "libmp3lame":
        args.extend(["-id3v2_version", "0", "-write_id3v1", "0"])
    if profile.encoder == "aac":
        args.extend(["-f", "adts"])
    args.append(str(output_path))
    _run_ffmpeg(args, output_path)

    info = probe_audio(output_path)
    if not _matches_profile(profile, info):
        raise AudioProcessingError("แปลง bait ให้ตรงกับเพลงไม่สำเร็จ")
    return info


def _trim_song(
    source_path: Path,
    output_path: Path,
    seconds: float,
    profile: AudioProfile,
) -> AudioProfile:
    args = [
        "-i",
        str(source_path),
        "-t",
        f"{seconds:.6f}",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-c",
        "copy",
    ]
    if profile.encoder == "aac":
        args.extend(["-f", "adts"])
    args.append(str(output_path))
    _run_ffmpeg(args, output_path)

    info = probe_audio(output_path)
    if not _matches_profile(profile, info):
        raise AudioProcessingError("ตัดเพลงไม่สำเร็จ")
    return info


def _trim_to_fit(
    source_path: Path,
    output_path: Path,
    max_song_duration: float,
    profile: AudioProfile,
) -> AudioProfile:
    target = max_song_duration - TRIM_MARGIN_SECONDS
    for _ in range(3):
        if target <= 0:
            break
        info = _trim_song(source_path, output_path, target, profile)
        if info.duration <= max_song_duration:
            return info
        target -= (info.duration - max_song_duration) + TRIM_MARGIN_SECONDS
    raise AudioProcessingError("ตัดเพลงให้ไม่เกิน 6:59 ไม่สำเร็จ")


def _ogg_crc_table() -> tuple[int, ...]:
    table: list[int] = []
    for index in range(256):
        value = index << 24
        for _ in range(8):
            if value & 0x80000000:
                value = ((value << 1) ^ OGG_CRC_POLYNOMIAL) & 0xFFFFFFFF
            else:
                value = (value << 1) & 0xFFFFFFFF
        table.append(value)
    return tuple(table)


_OGG_CRC_TABLE = _ogg_crc_table()


def _ogg_crc(page: bytearray) -> int:
    crc = 0
    for byte in page:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


def _read_ogg_pages(path: Path) -> list[bytearray]:
    data = path.read_bytes()
    pages: list[bytearray] = []
    offset = 0
    while offset < len(data):
        start = data.find(OGG_CAPTURE_PATTERN, offset)
        if start < 0 or start + 27 > len(data):
            break
        segment_count = data[start + 26]
        header_end = start + 27 + segment_count
        if header_end > len(data):
            break
        page_end = header_end + sum(data[start + 27 : header_end])
        if page_end > len(data):
            break
        pages.append(bytearray(data[start:page_end]))
        offset = page_end
    if not pages:
        raise AudioProcessingError("อ่านไฟล์ OGG ไม่สำเร็จ")
    return pages


def _ogg_granule(page: bytearray) -> int:
    return struct.unpack_from("<q", page, 6)[0]


def _ogg_serial(page: bytearray) -> int:
    return struct.unpack_from("<I", page, 14)[0]


def _rewrite_ogg_page(
    page: bytearray,
    serial: int,
    sequence: int,
    granule: int,
    flags: int,
) -> bytearray:
    page[5] = flags
    struct.pack_into("<q", page, 6, granule)
    struct.pack_into("<I", page, 14, serial)
    struct.pack_into("<I", page, 18, sequence)
    struct.pack_into("<I", page, 22, 0)
    struct.pack_into("<I", page, 22, _ogg_crc(page))
    return page


def _merge_ogg(parts: list[Path], output_path: Path) -> None:
    streams = [_read_ogg_pages(part) for part in parts]
    serial = _ogg_serial(streams[0][0])
    pages: list[bytearray] = []
    sequence = 0
    granule_offset = 0

    for stream_index, stream in enumerate(streams):
        last_granule = _ogg_granule(stream[-1])
        if last_granule <= 0:
            raise AudioProcessingError("อ่านตำแหน่งเวลา OGG ไม่สำเร็จ")

        started = stream_index == 0
        for page in stream:
            granule = _ogg_granule(page)
            if not started:
                if granule <= 0:
                    continue
                started = True
            if granule >= 0:
                granule += granule_offset
            flags = page[5] & ~OGG_EOS_FLAG
            pages.append(_rewrite_ogg_page(page, serial, sequence, granule, flags))
            sequence += 1
        granule_offset += last_granule

    last_page = pages[-1]
    last_page[5] |= OGG_EOS_FLAG
    struct.pack_into("<I", last_page, 22, 0)
    struct.pack_into("<I", last_page, 22, _ogg_crc(last_page))

    with output_path.open("wb") as target:
        for page in pages:
            target.write(page)
    if output_path.stat().st_size == 0:
        raise AudioProcessingError("รวมไฟล์เสียงไม่สำเร็จ")


def _binary_concat(parts: list[Path], output_path: Path) -> None:
    with output_path.open("wb") as target:
        for part in parts:
            with part.open("rb") as source:
                shutil.copyfileobj(source, target)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise AudioProcessingError("รวมไฟล์เสียงไม่สำเร็จ")


def process_bypass(
    input_path: Path,
    output_dir: Path,
    intro_path: Path = INTRO_PATH,
    outro_path: Path = OUTRO_PATH,
    max_output_seconds: float = MAX_OUTPUT_SECONDS,
) -> Path:
    source = probe_audio(input_path)
    output_path = output_dir / f"{uuid4().hex}{source.extension}"
    intro_output = output_dir / f"intro_{uuid4().hex}{source.extension}"
    outro_output = output_dir / f"outro_{uuid4().hex}{source.extension}"

    intro = _encode_bait(intro_path, intro_output, source)
    outro = _encode_bait(outro_path, outro_output, source)
    max_song_duration = max_output_seconds - intro.duration - outro.duration
    if max_song_duration <= 0:
        raise AudioProcessingError("bait ยาวเกิน 6:59")

    song_path = input_path
    if source.duration > max_song_duration:
        song_path = output_dir / f"song_{uuid4().hex}{source.extension}"
        _trim_to_fit(
            input_path,
            song_path,
            max_song_duration,
            source,
        )

    parts = [intro_output, song_path, outro_output]
    if source.extension == ".ogg":
        _merge_ogg(parts, output_path)
    else:
        _binary_concat(parts, output_path)
    return output_path
