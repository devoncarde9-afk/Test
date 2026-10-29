from __future__ import annotations

import math
import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MIN_HIGHPASS_HZ = 10
MAX_HIGHPASS_HZ = 20000
MIN_SHELF_GAIN_DB = -30.0
MAX_SHELF_GAIN_DB = 30.0
MIN_SPEED = 0.25
MAX_SPEED = 4.0
MIN_PITCH = 0.5
MAX_PITCH = 2.0
MIN_TEMPO = 0.25
MAX_TEMPO = 4.0
MIN_VOLUME_DB = -30.0
MAX_VOLUME_DB = 100.0
MIN_STACK_TRACKS = 1
MAX_STACK_TRACKS = 32
MAX_TRIM_SECONDS = 24 * 60 * 60
VORBIS_QUALITY = "5"
OUTPUT_SAMPLE_RATE = "48000"
LOW_HZ_SAMPLE_RATE = 22050
SOXR_RESAMPLE = (
    f"aresample={OUTPUT_SAMPLE_RATE}:resampler=soxr:precision=28:cheby=1"
)
RUBBERBAND_QUALITY = "pitchq=quality:window=long:channels=together"
AUDACITY_BASS_HZ = 250.0
AUDACITY_TREBLE_HZ = 4000.0
AUDACITY_SHELF_SLOPE = 0.4
WAVEFORM_SIZE = "640x240"
WAVEFORM_BACKGROUND = "#c0c0c0"
WAVEFORM_PEAK = "#3232c8"
WAVEFORM_RMS = "#6464dc"


@dataclass(frozen=True)
class AudioProbeResult:
    integrated_lufs: float
    true_peak_dbfs: float
    duration_seconds: float
    sample_rate: int


def parse_ratio(raw: str) -> float:
    text = raw.strip().casefold()
    if not text:
        raise ValueError
    if text.endswith("%"):
        return float(text[:-1]) / 100.0
    if text.endswith("x"):
        text = text[:-1]
    return float(text)


def _parse_factor(raw: str, minimum: float, maximum: float) -> float:
    value = parse_ratio(raw)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError
    return value


def parse_speed(raw: str) -> float:
    return _parse_factor(raw, MIN_SPEED, MAX_SPEED)


def parse_tempo(raw: str) -> float:
    return _parse_factor(raw, MIN_TEMPO, MAX_TEMPO)


def parse_volume_db(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or not MIN_VOLUME_DB <= value <= MAX_VOLUME_DB:
        raise ValueError
    return value


def parse_stack_count(raw: str) -> int:
    value = int(raw)
    if not MIN_STACK_TRACKS <= value <= MAX_STACK_TRACKS:
        raise ValueError
    return value


def parse_time_value(raw: str) -> float:
    text = raw.strip()
    if not text:
        raise ValueError

    if ":" in text:
        parts = text.split(":")
        try:
            if len(parts) == 2:
                minutes = int(parts[0])
                seconds = float(parts[1])
                if minutes < 0 or not 0 <= seconds < 60:
                    raise ValueError
                value = minutes * 60 + seconds
            elif len(parts) == 3:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = float(parts[2])
                if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
                    raise ValueError
                value = hours * 3600 + minutes * 60 + seconds
            else:
                raise ValueError
        except ValueError as exc:
            raise ValueError from exc
    elif text.isdecimal() and len(text) >= 3:
        minutes = int(text[:-2])
        seconds = int(text[-2:])
        if seconds >= 60:
            raise ValueError
        value = minutes * 60 + seconds
    else:
        value = float(text)

    if not math.isfinite(value) or not 0 < value <= MAX_TRIM_SECONDS:
        raise ValueError
    return value


def parse_pitch(raw: str) -> float:
    text = raw.strip().casefold()
    if not text:
        raise ValueError
    if text.endswith("st"):
        semitones = float(text[:-2])
    elif text.startswith(("+", "-")):
        semitones = float(text)
    else:
        semitones = None

    if semitones is not None:
        if not math.isfinite(semitones) or not -12.0 <= semitones <= 12.0:
            raise ValueError
        value = 2.0 ** (semitones / 12.0)
    else:
        value = parse_ratio(text)
    if not math.isfinite(value) or not MIN_PITCH <= value <= MAX_PITCH:
        raise ValueError
    return value


def build_eight_mono_filter(
    highpass_hz: int | None = None,
    bass_db: float = 0.0,
    treble_db: float = 0.0,
) -> str:
    filters: list[str] = []
    if highpass_hz is not None:
        filters.append(f"highpass=f={highpass_hz}")
    filters.extend(_audacity_shelf_filters(bass_db, treble_db))
    prefix = "".join(f"{audio_filter}," for audio_filter in filters)
    return (
        f"[0:a]aformat=channel_layouts=mono,aresample={OUTPUT_SAMPLE_RATE},{prefix}"
        "pan=8c|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0|c6=c0|c7=c0[eight];"
        "[eight]pan=mono|c0=c0+c1+c2+c3+c4+c5+c6+c7,volume=8[mono];"
        "[mono]pan=stereo|c0=c0|c1=c0[stereo]"
    )


def build_thirty_two_tracks_filter(
    highpass_hz: int | None = None,
    bass_db: float = 0.0,
    treble_db: float = 0.0,
) -> str:
    filters: list[str] = []
    if highpass_hz is not None:
        filters.append(f"highpass=f={highpass_hz}")
    filters.extend(_audacity_shelf_filters(bass_db, treble_db))
    prefix = "".join(f"{audio_filter}," for audio_filter in filters)
    split_labels = "".join(f"[t{index}]" for index in range(32))
    return (
        f"[0:a]aformat=channel_layouts=mono,"
        f"aresample={OUTPUT_SAMPLE_RATE},{prefix}asplit=32{split_labels};"
        f"{split_labels}amix=inputs=32:normalize=0,volume=32[mono];"
        "[mono]pan=stereo|c0=c0|c1=c0[stereo]"
    )


def _audacity_shelf_coefficients(
    frequency_hz: float,
    gain_db: float,
    is_bass: bool,
    sample_rate: float = 48000.0,
    slope: float = AUDACITY_SHELF_SLOPE,
) -> tuple[float, float, float, float, float, float]:
    w = 2.0 * math.pi * frequency_hz / sample_rate
    a = 10.0 ** (gain_db / 40.0)
    b = math.sqrt((a * a + 1.0) / slope - (a - 1.0) ** 2)
    cosine = math.cos(w)
    sine = math.sin(w)

    if is_bass:
        b0 = a * ((a + 1.0) - (a - 1.0) * cosine + b * sine)
        b1 = 2.0 * a * ((a - 1.0) - (a + 1.0) * cosine)
        b2 = a * ((a + 1.0) - (a - 1.0) * cosine - b * sine)
        a0 = (a + 1.0) + (a - 1.0) * cosine + b * sine
        a1 = -2.0 * ((a - 1.0) + (a + 1.0) * cosine)
        a2 = (a + 1.0) + (a - 1.0) * cosine - b * sine
    else:
        b0 = a * ((a + 1.0) + (a - 1.0) * cosine + b * sine)
        b1 = -2.0 * a * ((a - 1.0) + (a + 1.0) * cosine)
        b2 = a * ((a + 1.0) + (a - 1.0) * cosine - b * sine)
        a0 = (a + 1.0) - (a - 1.0) * cosine + b * sine
        a1 = 2.0 * ((a - 1.0) - (a + 1.0) * cosine)
        a2 = (a + 1.0) - (a - 1.0) * cosine - b * sine

    return b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0


def _biquad_filter(
    coefficients: tuple[float, float, float, float, float, float],
) -> str:
    b0, b1, b2, a0, a1, a2 = coefficients
    return (
        f"biquad=b0={b0:.17g}:b1={b1:.17g}:b2={b2:.17g}:"
        f"a0={a0:.17g}:a1={a1:.17g}:a2={a2:.17g}"
    )


def _audacity_shelf_filters(
    bass_db: float,
    treble_db: float,
) -> list[str]:
    filters: list[str] = []
    if bass_db:
        filters.append(
            _biquad_filter(
                _audacity_shelf_coefficients(
                    AUDACITY_BASS_HZ,
                    bass_db,
                    is_bass=True,
                )
            )
        )
    if treble_db:
        filters.append(
            _biquad_filter(
                _audacity_shelf_coefficients(
                    AUDACITY_TREBLE_HZ,
                    treble_db,
                    is_bass=False,
                )
            )
        )
    return filters


def build_volume_filter(
    volume_db: float,
    highpass_hz: int | None = None,
    bass_db: float = 0.0,
    treble_db: float = 0.0,
) -> str:
    filters: list[str] = []
    if highpass_hz is not None:
        filters.append(f"highpass=f={highpass_hz}")
    filters.extend(_audacity_shelf_filters(bass_db, treble_db))
    filters.append(f"volume={volume_db:.17g}dB")
    prefix = "".join(f"{audio_filter}," for audio_filter in filters)
    return (
        f"[0:a]{SOXR_RESAMPLE},"
        f"{prefix}aformat=sample_fmts=fltp:channel_layouts=stereo[stereo]"
    )


def build_stack_filter(
    tracks: int,
    highpass_hz: int | None = None,
    bass_db: float = 0.0,
    treble_db: float = 0.0,
) -> str:
    filters: list[str] = []
    if highpass_hz is not None:
        filters.append(f"highpass=f={highpass_hz}")
    filters.extend(_audacity_shelf_filters(bass_db, treble_db))
    prefix = "".join(f"{audio_filter}," for audio_filter in filters)

    if tracks == 1:
        return (
            f"[0:a]aformat=channel_layouts=mono,aresample={OUTPUT_SAMPLE_RATE},"
            f"{prefix}pan=stereo|c0=c0|c1=c0[stereo]"
        )

    split_labels = "".join(f"[track{index}]" for index in range(tracks))
    return (
        f"[0:a]aformat=channel_layouts=mono,aresample={OUTPUT_SAMPLE_RATE},"
        f"{prefix}asplit={tracks}{split_labels};"
        f"{split_labels}amix=inputs={tracks}:normalize=0,"
        "pan=stereo|c0=c0|c1=c0[stereo]"
    )


def build_aeksets_filter() -> str:
    bass_a = _biquad_filter(
        _audacity_shelf_coefficients(AUDACITY_BASS_HZ, -30.0, is_bass=True)
    )
    treble_a = _biquad_filter(
        _audacity_shelf_coefficients(AUDACITY_TREBLE_HZ, -30.0, is_bass=False)
    )
    bass_b = _biquad_filter(
        _audacity_shelf_coefficients(AUDACITY_BASS_HZ, -15.0, is_bass=True)
    )
    treble_b = _biquad_filter(
        _audacity_shelf_coefficients(AUDACITY_TREBLE_HZ, 15.0, is_bass=False)
    )
    split_labels = "".join(f"[s{index}]" for index in range(32))
    return (
        "[0:a]aformat=channel_layouts=mono,aresample=48000,asplit=2[a][b];"
        f"[a]{bass_a},{treble_a},volume=15dB[a_mix];"
        f"[b]{bass_b},{treble_b}[b_mix];"
        "[a_mix][b_mix]amix=inputs=2:normalize=0[mix];"
        f"[mix]asplit=32{split_labels};"
        f"{split_labels}amix=inputs=32:normalize=0[mono];"
        "[mono]pan=stereo|c0=c0|c1=c0[stereo]"
    )


def build_mix_filter() -> str:
    normalize = (
        f"aresample={OUTPUT_SAMPLE_RATE},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "asetpts=PTS-STARTPTS"
    )
    return (
        f"[0:a]{normalize}[first];"
        f"[1:a]{normalize}[second];"
        "[first][second]concat=n=2:v=0:a=1[stereo]"
    )


def build_speed_filter(speed: float) -> str:
    rate = int(round(48000 * speed))
    return (
        f"[0:a]{SOXR_RESAMPLE},asetrate={rate},{SOXR_RESAMPLE},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo[stereo]"
    )


def build_pitch_filter(pitch: float) -> str:
    return (
        f"[0:a]{SOXR_RESAMPLE},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"rubberband=pitch={pitch:.17g}:{RUBBERBAND_QUALITY}[stereo]"
    )


def build_tempo_filter(tempo: float) -> str:
    factors: list[float] = []
    remaining = tempo
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    tempo_filters = ",".join(f"atempo={factor:.17g}" for factor in factors)
    return (
        f"[0:a]{SOXR_RESAMPLE},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"{tempo_filters}[stereo]"
    )


def build_cut_filter(duration: float) -> str:
    return (
        f"[0:a]atrim=start=0:end={duration:.17g},asetpts=PTS-STARTPTS,"
        f"{SOXR_RESAMPLE},aformat=sample_fmts=fltp:channel_layouts=stereo[stereo]"
    )


def build_fcut_filter(duration: float) -> str:
    return (
        f"[0:a]atrim=start={duration:.17g},asetpts=PTS-STARTPTS,"
        f"{SOXR_RESAMPLE},aformat=sample_fmts=fltp:channel_layouts=stereo[stereo]"
    )


class AudioProcessingError(Exception):
    pass


def _probe_value(pattern: str, output: str, label: str) -> float:
    match = re.search(pattern, output, re.IGNORECASE | re.DOTALL)
    if match is None:
        raise AudioProcessingError(f"อ่านค่า {label} จาก ffmpeg ไม่สำเร็จ")
    try:
        return float(match.group(1))
    except ValueError as exc:
        raise AudioProcessingError(f"อ่านค่า {label} จาก ffmpeg ไม่สำเร็จ") from exc


def _probe_audio_metadata(input_path: Path) -> tuple[float, int]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise AudioProcessingError("ไม่พบ ffprobe")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "format=duration:stream=duration,sample_rate",
            "-of",
            "json",
            str(input_path),
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
        raise AudioProcessingError(detail[-500:] or "อ่านความยาวเสียงไม่สำเร็จ")

    try:
        data = json.loads(proc.stdout)
        stream = (data.get("streams") or [{}])[0]
        format_duration = float((data.get("format") or {}).get("duration", "nan"))
        stream_duration = float(stream.get("duration", "nan"))
        sample_rate = int(stream.get("sample_rate", "0"))
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AudioProcessingError("อ่านข้อมูลเสียงไม่สำเร็จ") from exc

    duration = format_duration if math.isfinite(format_duration) else stream_duration
    if not math.isfinite(duration) or duration < 0:
        raise AudioProcessingError("อ่านความยาวเสียงไม่สำเร็จ")
    if sample_rate <= 0:
        raise AudioProcessingError("อ่าน sample rate ไม่สำเร็จ")
    return duration, sample_rate


def process_probe(input_path: Path, waveform_path: Path) -> AudioProbeResult:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioProcessingError("ไม่พบ ffmpeg")
    if not input_path.is_file():
        raise AudioProcessingError("ไม่พบไฟล์เสียง")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    analysis = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-nostdin",
            "-i",
            str(input_path),
            "-map",
            "0:a:0",
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    if analysis.returncode != 0:
        detail = (analysis.stderr or "").strip()
        raise AudioProcessingError(detail[-500:] or "วิเคราะห์เสียงไม่สำเร็จ")

    output = analysis.stderr
    duration_seconds, sample_rate = _probe_audio_metadata(input_path)
    result = AudioProbeResult(
        integrated_lufs=_probe_value(
            r"Integrated loudness:\s*I:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*LUFS",
            output,
            "LUFS",
        ),
        true_peak_dbfs=_probe_value(
            r"True peak:\s*Peak:\s*(-?(?:\d+(?:\.\d*)?|\.\d+|inf))\s*dBFS",
            output,
            "true peak",
        ),
        duration_seconds=duration_seconds,
        sample_rate=sample_rate,
    )

    waveform_filter = (
        f"[0:a]showwavespic=s={WAVEFORM_SIZE}:split_channels=1:"
        f"colors={WAVEFORM_PEAK}:filter=peak:draw=full[pk];"
        f"[0:a]showwavespic=s={WAVEFORM_SIZE}:split_channels=1:"
        f"colors={WAVEFORM_RMS}:filter=average:draw=full[rms];"
        f"color=c={WAVEFORM_BACKGROUND}:s={WAVEFORM_SIZE}[bg];"
        "[bg][pk]overlay=format=auto[bg1];"
        "[bg1][rms]overlay=format=auto"
    )
    waveform = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(input_path),
            "-filter_complex",
            waveform_filter,
            "-frames:v",
            "1",
            str(waveform_path),
        ],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    if waveform.returncode != 0 or not waveform_path.is_file():
        detail = (waveform.stderr or "").strip()
        raise AudioProcessingError(detail[-500:] or "สร้าง waveform ไม่สำเร็จ")
    if waveform_path.stat().st_size == 0:
        raise AudioProcessingError("สร้าง waveform ไม่สำเร็จ")

    return result


def _run_ffmpeg(
    input_paths: Path | Sequence[Path],
    output_path: Path,
    filter_complex: str,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioProcessingError("ไม่พบ ffmpeg")

    if isinstance(input_paths, Path):
        inputs = [input_paths]
    else:
        inputs = list(input_paths)
    if not inputs:
        raise AudioProcessingError("ไม่พบไฟล์เสียง")

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    ]
    for path in inputs:
        command.extend(["-i", str(path)])
    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[stereo]",
            "-vn",
            "-c:a",
            "libvorbis",
            "-q:a",
            VORBIS_QUALITY,
            "-ar",
            OUTPUT_SAMPLE_RATE,
            "-ac",
            "2",
            str(output_path),
        ]
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    if proc.returncode != 0 or not output_path.is_file():
        detail = (proc.stderr or "").strip()
        raise AudioProcessingError(detail[-500:] or "ffmpeg ทำงานไม่สำเร็จ")
    if output_path.stat().st_size == 0:
        raise AudioProcessingError("ไฟล์ผลลัพธ์ว่างเปล่า")


def _validate_processing(
    highpass_hz: int | None,
    bass_db: float,
    treble_db: float,
) -> None:
    if (
        highpass_hz is not None
        and not MIN_HIGHPASS_HZ <= highpass_hz <= MAX_HIGHPASS_HZ
    ):
        raise AudioProcessingError(
            f"highpass ต้องอยู่ระหว่าง {MIN_HIGHPASS_HZ} ถึง {MAX_HIGHPASS_HZ} Hz"
        )
    if not MIN_SHELF_GAIN_DB <= bass_db <= MAX_SHELF_GAIN_DB:
        raise AudioProcessingError(
            f"bass ต้องอยู่ระหว่าง {MIN_SHELF_GAIN_DB} ถึง {MAX_SHELF_GAIN_DB} dB"
        )
    if not MIN_SHELF_GAIN_DB <= treble_db <= MAX_SHELF_GAIN_DB:
        raise AudioProcessingError(
            f"treble ต้องอยู่ระหว่าง {MIN_SHELF_GAIN_DB} ถึง {MAX_SHELF_GAIN_DB} dB"
        )


def _resample_to_wav(
    input_path: Path,
    output_path: Path,
    sample_rate: int,
) -> None:
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
            "-i",
            str(input_path),
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "pcm_s16le",
            "-ar",
            str(sample_rate),
            str(output_path),
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
        raise AudioProcessingError(detail[-500:] or "แปลงเสียงเป็น WAV ไม่สำเร็จ")
    if output_path.stat().st_size == 0:
        raise AudioProcessingError("แปลงเสียงเป็น WAV ไม่สำเร็จ")


def _validate_factor(
    label: str,
    value: float,
    minimum: float,
    maximum: float,
) -> None:
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise AudioProcessingError(
            f"{label} ต้องอยู่ระหว่าง {minimum} ถึง {maximum}"
        )


def process_eight_mono(
    input_path: Path,
    output_path: Path,
    highpass_hz: int | None = None,
    bass_db: float = 0.0,
    treble_db: float = 0.0,
) -> None:
    _validate_processing(highpass_hz, bass_db, treble_db)
    _run_ffmpeg(
        input_path,
        output_path,
        build_eight_mono_filter(highpass_hz, bass_db, treble_db),
    )


def process_thirty_two_tracks(
    input_path: Path,
    output_path: Path,
    highpass_hz: int | None = None,
    bass_db: float = 0.0,
    treble_db: float = 0.0,
) -> None:
    _validate_processing(highpass_hz, bass_db, treble_db)
    _run_ffmpeg(
        input_path,
        output_path,
        build_thirty_two_tracks_filter(highpass_hz, bass_db, treble_db),
    )


def process_volume(
    input_path: Path,
    output_path: Path,
    volume_db: float,
    highpass_hz: int | None = None,
    bass_db: float = 0.0,
    treble_db: float = 0.0,
) -> None:
    _validate_factor("volume", volume_db, MIN_VOLUME_DB, MAX_VOLUME_DB)
    _validate_processing(highpass_hz, bass_db, treble_db)
    _run_ffmpeg(
        input_path,
        output_path,
        build_volume_filter(volume_db, highpass_hz, bass_db, treble_db),
    )


def process_stack(
    input_path: Path,
    output_path: Path,
    tracks: int,
    highpass_hz: int | None = None,
    bass_db: float = 0.0,
    treble_db: float = 0.0,
) -> None:
    if type(tracks) is not int or not MIN_STACK_TRACKS <= tracks <= MAX_STACK_TRACKS:
        raise AudioProcessingError(
            f"stack ต้องอยู่ระหว่าง {MIN_STACK_TRACKS} ถึง {MAX_STACK_TRACKS} เลเยอร์"
        )
    _validate_processing(highpass_hz, bass_db, treble_db)
    _run_ffmpeg(
        input_path,
        output_path,
        build_stack_filter(tracks, highpass_hz, bass_db, treble_db),
    )


def process_aeksets(input_path: Path, output_path: Path) -> None:
    _run_ffmpeg(input_path, output_path, build_aeksets_filter())


def process_aekloudlowhz(input_path: Path, output_path: Path) -> None:
    intermediate_path = output_path.with_name(f"{output_path.stem}_22050.wav")
    try:
        _resample_to_wav(input_path, intermediate_path, LOW_HZ_SAMPLE_RATE)
        process_aeksets(intermediate_path, output_path)
    finally:
        intermediate_path.unlink(missing_ok=True)


def process_mix(
    first_path: Path,
    second_path: Path,
    output_path: Path,
) -> None:
    _run_ffmpeg((first_path, second_path), output_path, build_mix_filter())


def process_speed(input_path: Path, output_path: Path, speed: float) -> None:
    _validate_factor("speed", speed, MIN_SPEED, MAX_SPEED)
    _run_ffmpeg(input_path, output_path, build_speed_filter(speed))


def process_pitch(input_path: Path, output_path: Path, pitch: float) -> None:
    _validate_factor("pitch", pitch, MIN_PITCH, MAX_PITCH)
    _run_ffmpeg(input_path, output_path, build_pitch_filter(pitch))


def process_tempo(input_path: Path, output_path: Path, tempo: float) -> None:
    _validate_factor("tempo", tempo, MIN_TEMPO, MAX_TEMPO)
    _run_ffmpeg(input_path, output_path, build_tempo_filter(tempo))


def _validate_trim_duration(duration: float) -> None:
    if not math.isfinite(duration) or not 0 < duration <= MAX_TRIM_SECONDS:
        raise AudioProcessingError(
            f"เวลาต้องมากกว่า 0 และไม่เกิน {MAX_TRIM_SECONDS} วินาที"
        )


def process_cut(input_path: Path, output_path: Path, duration: float) -> None:
    _validate_trim_duration(duration)
    _run_ffmpeg(input_path, output_path, build_cut_filter(duration))


def process_fcut(input_path: Path, output_path: Path, duration: float) -> None:
    _validate_trim_duration(duration)
    _run_ffmpeg(input_path, output_path, build_fcut_filter(duration))
