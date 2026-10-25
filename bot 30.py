import os
import re
import json
import random
import socket
import platform
import array
import subprocess
import asyncio
import tempfile
import logging
from dataclasses import dataclass, replace

import discord
from discord import app_commands
from discord.ext import commands

try:
    import psutil
except ImportError:
    psutil = None

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bait-bot")

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ.get("GUILD_ID", "1562831867844108329"))  # server that gets commands instantly
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
PRE_PATH = os.path.join(ASSETS_DIR, "pre.ogg")  # plays before the intro in /bait, optional
INTRO_PATH = os.path.join(ASSETS_DIR, "intro.mp3")  # default intro used by /bait
OUTRO_PATH = os.path.join(ASSETS_DIR, "outro.mp3")  # outro used by /bait and /randomintro
INTROS_DIR = os.path.join(ASSETS_DIR, "intros")  # pool of intros for /randomintro

MAX_DISCORD_UPLOAD = 25 * 1024 * 1024  # 25 MB, adjust for your server's boost level
OUT_RATE = 44100
DEFAULT_IN_RATE = 44100
BASS_FREQ = 100  # Hz, center of the bass cut
AUDIO_EXTS = (".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac", ".opus", ".webm", ".mp4")

# /bait speed range. Any value in this range works.
BAIT_SPEED_MIN = 0.5
BAIT_SPEED_MAX = 2.0
BAIT_SPEED_DEFAULT = 1.4

# /volume range in dB. Clean style stops getting louder once the limiter takes over.
VOLUME_DB_MIN = -30.0
VOLUME_DB_MAX = 60.0
# Above this many dB, hard-style clipping drops to a lower internal rate to cut high-frequency static.
VOLUME_HZ_THRESHOLD_DB = 20.0
VOLUME_LOW_HZ = 22050

# /amplify (Audacity-style): target peak in dB. 0 dBFS is the top without clipping.
AMPLIFY_TARGET_DEFAULT = 0.0
AMPLIFY_TARGET_MIN = -30.0
AMPLIFY_TARGET_MAX = 60.0
AMPLIFY_BASS_CUT_DEFAULT = 0.0
AMPLIFY_BASS_CUT_MAX = 40.0
# /amplify clipping rate. "auto" drops to MASTER_AUTO_HZ only when clipping is allowed and gain is high.
AMPLIFY_AUTO_THRESHOLD_DB = 20.0

# /master: normalize to -14 LUFS with two-pass loudnorm (measure, then apply fixed values),
# then add gain with no limiter. +24 dB lands at about 10 LUFS, matching the uploaded clips.
MASTER_TARGET = "I=-14:TP=-1:LRA=11"
MASTER_GAIN_DB = 24.0
MASTER_GAIN_MAX = 60.0
# /master clipping rate. "auto" uses MASTER_AUTO_HZ above MASTER_AUTO_THRESHOLD_DB and full rate below.
MASTER_AUTO_THRESHOLD_DB = 20.0
MASTER_AUTO_HZ = 22050

# /bass: how many dB to remove at BASS_FREQ.
BASS_CUT_DEFAULT = 30.0
BASS_CUT_MAX = 40.0

# /volume clean and hard styles.
# clean: the gain goes into a limiter. Smooth sound, but the limiter sets the
# final level, so the output stops getting louder past roughly +30 dB.
CLEAN_PRE = (
    "highpass=f=35,"
    "acompressor=threshold=-18dB:ratio=3:attack=10:release=150:makeup=3"
)
CLEAN_POST = (
    "alimiter=limit=0.891:attack=2:release=60:level=disabled,"
    "lowpass=f=16000"
)
# hard: the gain goes straight to the hard clip at encode, so every dB stays audible.
# A low cut and top-end roll-off take some of the harshness out of the clipping.
HARD_PRE = "highpass=f=35"
HARD_POST = "lowpass=f=11000"
# hard above VOLUME_HZ_THRESHOLD_DB: clip at a lower internal rate, then return to OUT_RATE.
HARD_LOW_RATE_POST = (
    f"aresample={VOLUME_LOW_HZ},"
    "aformat=sample_fmts=s16,"
    f"aresample={OUT_RATE},"
    "lowpass=f=11000"
)

# /angelic: two tracks from the Audacity project, processed separately and mixed.
# Track A: heavy bass cut, a high-pass, a 2x time stretch (tempo halved, pitch kept),
#          then a 4 Hz tremolo at 26% depth.
ANGELIC_TRACK_A = (
    "bass=g=-27,volume=7.6dB,"
    "highpass=f=300,"
    "atempo=0.5,"
    "tremolo=f=4:d=0.26"
)
# Track B: bass boost, a low-pass around 300 Hz with the treble cut, a compressor with
#          makeup gain, a phaser, soft clipping, then a final bass and treble lift.
ANGELIC_TRACK_B = (
    "bass=g=5,volume=5.8dB,treble=g=-30,"
    "lowpass=f=300,"
    "acompressor=threshold=-27dB:ratio=16.2:attack=1.5:release=401:makeup=6.92,"
    "aphaser=in_gain=0.9:out_gain=0.9:delay=2:decay=0.3:speed=2,"
    "asoftclip=type=tanh,"
    "bass=g=1,volume=11.6dB,treble=g=14"
)

# /cr defaults, from the discord.js cr command.
CR_SPEED_DEFAULT = 5.0
CR_SPEED_MIN = -20.0
CR_SPEED_MAX = 20.0
CR_LOW_DEFAULT = 1450.0
CR_HIGH_DEFAULT = 3500.0
# Discord user ID that /cr refuses, carried over from the original command. Delete to remove the block.
CR_BLOCKED_USER_ID = "1456822669969461483"

# Intermediate format. 32-bit float keeps samples above full scale intact,
# so nothing is clipped until the final encode.
RAW_FMT = ["-f", "f32le", "-ar", str(OUT_RATE), "-ac", "2"]

# aecho: in_gain:out_gain:delays_ms:decays
REVERB_PRESETS = {
    "room": "0.8:0.88:50|80:0.4|0.3",
    "hall": "0.8:0.88:100|150|200:0.4|0.3|0.2",
    "cave": "0.6:0.9:200|300|400:0.5|0.4|0.3",
}

# Effect chains: (command name, description, ffmpeg filter chain).
# The orz* entries approximate uploaded Audacity chains, in the order listed in each file.
FX_COMMANDS = [
    ("orzmid", "orzxjo mid set: +9 dB, bass/treble, compressor, bass/treble",
     "volume=9dB,bass=g=-10,treble=g=-3,"
     "acompressor=threshold=-20dB:ratio=3:attack=101:release=401:makeup=1.585,"
     "bass=g=7,volume=-6dB,treble=g=12"),
    ("orzloud", "orzxjo loud set: bass cut, +7.5 dB, hard compression, big makeup",
     "bass=g=-15,volume=7.5dB,"
     "acompressor=threshold=-24dB:ratio=8.6:attack=1.5:release=2:makeup=15.85"),
    ("orzwah", "orzxjo wah set: bass cut, +10 dB, phaser sweep, 100 Hz high-pass",
     "bass=g=-14,volume=5dB,"
     "aphaser=in_gain=0.6:out_gain=0.8:delay=2:decay=0.5:speed=4,"
     "volume=5dB,highpass=f=100,highpass=f=100"),
    ("orzloudset", "orzxjo loudset: EQ, phaser, compression, click removal, +13.8 dB total",
     "bass=g=-1,treble=g=7,volume=-3dB,bass=g=-1,treble=g=7,volume=-3dB,"
     "aphaser=in_gain=0.5:out_gain=0.75:delay=2:decay=0.4:speed=0.6,"
     "acompressor=threshold=-29dB:ratio=6:attack=1.5:release=2:makeup=15.85,"
     "bass=g=-9,treble=g=1,volume=7.8dB,"
     "acompressor=threshold=-29dB:ratio=6:attack=1.5:release=2:makeup=15.85,"
     "adeclick,"
     "aphaser=in_gain=0.5:out_gain=0.75:delay=2:decay=0.4:speed=0.6,"
     "volume=6dB"),
    ("orzdrown", "orzxjo drown clean: -8 dB, sub cut, bass cut at 125 Hz, heavy compression",
     "volume=-8dB,highpass=f=22,bass=g=-10:f=125,"
     "acompressor=threshold=-30dB:ratio=20:attack=4:release=120:makeup=14.13"),
    # Audacity chain: _orzxjo_cr_sets.txt, applied in order.
    # ChangePitch keeps tempo: asetrate shifts pitch and tempo, atempo restores tempo.
    # ChangeTempo keeps pitch: atempo only. ChangeSpeedAndPitch: asetrate only.
    ("orzcr", "orzxjo cr set: nine steps of small pitch, tempo, and speed changes",
     "asetrate=42777,aresample=44100,atempo=1.030928,"   # pitch -3%, tempo kept
     "atempo=1.02,"                                       # tempo +2%
     "asetrate=44541,aresample=44100,"                    # speed +1%
     "asetrate=41895,aresample=44100,atempo=1.052632,"   # pitch -5%, tempo kept
     "asetrate=45423,aresample=44100,"                    # speed +3%
     "atempo=1.04,"                                       # tempo +4%
     "asetrate=43218,aresample=44100,"                    # speed -2%
     "asetrate=45864,aresample=44100,atempo=0.961538,"   # pitch +4%, tempo kept
     "atempo=1.03"),                                      # tempo +3%
    # Audacity chain: 999.txt. Tempo +13% (pitch kept), speed -25% (pitch and tempo together),
    # pitch -9% (tempo kept), then a phaser. The phaser is approximate.
    ("999", "Chain from 999.txt: tempo +13%, speed -25%, pitch -9%, phaser",
     "atempo=1.13,"
     "asetrate=33075,aresample=44100,"
     "asetrate=40131,aresample=44100,atempo=1.098901,"
     "aphaser=in_gain=0.9:out_gain=0.78:delay=3:decay=0.6:speed=2.1"),
    ("echo", "Add an echo", "aecho=0.8:0.9:500:0.3"),
    ("flanger", "Flanger sweep", "flanger"),
    ("phaser", "Phaser sweep", "aphaser"),
    ("chorus", "Chorus", "chorus=0.7:0.9:55:0.4:0.25:2"),
    ("tremolo", "Tremolo (volume wobble)", "tremolo=f=5:d=0.8"),
    ("vibrato", "Vibrato (pitch wobble)", "vibrato=f=6:d=0.5"),
    ("telephone", "Phone-line bandpass", "highpass=f=300,lowpass=f=3400"),
    ("radio", "AM radio sound", "highpass=f=400,lowpass=f=2800,volume=2"),
    ("underwater", "Muffled underwater sound", "lowpass=f=500,aecho=0.6:0.5:300:0.4"),
    ("buzz", "Buzzing tremolo", "tremolo=f=30:d=1"),
    ("bassboost", "Boost the bass", "bass=g=10"),
    ("trebleboost", "Boost the treble", "treble=g=8"),
    ("bitcrush", "Lo-fi bitcrush", "acrusher=bits=6:mode=log:aa=1"),
    ("distortion", "Hard distortion", "acrusher=bits=4:mode=lin:aa=1,volume=2"),
    ("widestereo", "Widen the stereo image", "extrastereo=m=2.5"),
    ("mono", "Collapse to mono", "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"),
    ("swapchannels", "Swap left and right", "pan=stereo|c0=c1|c1=c0"),
    ("reverse", "Play backwards (uses more memory on long files)", "areverse"),
    ("vinyl", "Warm vinyl-style sound", "lowpass=f=8000,crystalizer=i=3"),
    ("crystalize", "Add sparkle", "crystalizer=i=4"),
    ("softclip", "Soft-clip", "asoftclip=type=tanh"),
    ("compressor", "Squash dynamics", "acompressor=threshold=-20dB:ratio=6:attack=5:release=50:makeup=6"),
    ("loudnorm", "Normalize to -14 LUFS", "loudnorm=I=-14:TP=-1:LRA=11"),
    ("gate", "Noise gate", "agate=threshold=0.05"),
    ("tape", "Old cassette tape sound", "highpass=f=60,lowpass=f=12000,tremolo=f=0.5:d=0.2,crystalizer=i=2"),
]

# Number of entries at the start of FX_COMMANDS that are sets (orzxjo), listed as Sets in /help.
SET_COUNT = 7

intents = discord.Intents.default()
bot = commands.Bot(command_prefix=".", intents=intents)
tree = bot.tree


def anywhere(fn):
    """Let a command be installed to a user, so it works in servers, DMs, and group DMs."""
    fn = app_commands.allowed_installs(guilds=True, users=True)(fn)
    return app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(fn)


@dataclass(frozen=True)
class BaitSettings:
    speed: float = 1.0          # tempo + pitch together (turntable style)
    pitch: float = 0.0          # semitones, tempo preserved
    tempo: float = 1.0          # tempo multiplier, pitch preserved
    reverb: str = "off"         # key of REVERB_PRESETS, or "off"
    gain_db: float = 0.0        # raw dB boost on the main track, allowed to clip
    bass_db: float = 0.0        # dB at BASS_FREQ: negative removes bass, 0 = unchanged
    fx: str = ""                # raw ffmpeg filter chain before gain, or "" for none
    after_gain: str = ""        # raw ffmpeg filter chain after gain, or "" for none
    two_pass_loudnorm: str = ""  # target for two-pass loudnorm (e.g. "I=-14:TP=-1:LRA=11"), or ""
    mix_tracks: tuple[str, str] | None = None  # two chains run in parallel and mixed (/angelic)
    add_intro: bool = False     # an intro file goes before the song; effects apply to both
    wrap: bool = False          # add pre, intro, and outro clips around the track (only /bait)
    match_output: bool = False  # make the whole output match the input's loudness (LUFS)
    safe_peaks: bool = False    # oversample the speed step and limit peaks at the end (only /bait)


def ffmpeg_error_text(stderr: bytes) -> str:
    """Return the lines that actually describe the error, not the progress output."""
    lines = stderr.decode(errors="ignore").splitlines()
    errs = [l.strip() for l in lines if re.search(r"error|invalid|failed|unable|no such|cannot", l, re.I)]
    if errs:
        return " | ".join(errs[-3:])[-400:]
    return (lines[-1] if lines else "unknown error")[-400:]


async def run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {ffmpeg_error_text(stderr)}")


async def measure_peak_db(path: str, af: str | None = None) -> float:
    """Peak level in dB (volumedetect), optionally after a filter chain. Used by /amplify."""
    chain = f"{af},volumedetect" if af else "volumedetect"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-af", chain, "-f", "null", "-",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    match = re.search(r"max_volume:\s+(-?[\d.]+) dB", stderr.decode(errors="ignore"))
    if not match:
        raise RuntimeError("could not measure the peak level")
    return float(match.group(1))


async def measure_loudnorm(path: str, target: str) -> str:
    """Pass one of two-pass loudnorm: measure the file, return the filter with fixed values."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-af", f"loudnorm={target}:print_format=json", "-f", "null", "-",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"loudnorm measure failed: {ffmpeg_error_text(stderr)}")
    blocks = re.findall(r"\{[^{}]*\}", stderr.decode(errors="ignore"))
    if not blocks:
        raise RuntimeError("loudnorm measure gave no result")
    d = json.loads(blocks[-1])
    return (
        f"loudnorm={target}"
        f":measured_I={d['input_i']}"
        f":measured_TP={d['input_tp']}"
        f":measured_LRA={d['input_lra']}"
        f":measured_thresh={d['input_thresh']}"
        f":offset={d['target_offset']}"
        f":linear=true"
    )


async def measure_lufs(path: str, raw: bool = False) -> float:
    """Integrated loudness (LUFS). Pass raw=True for a headerless float32 file."""
    args = ["ffmpeg", "-hide_banner", "-nostats"]
    if raw:
        args += RAW_FMT
    args += ["-i", path, "-af", "ebur128", "-f", "null", "-"]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    matches = re.findall(r"I:\s+(-?[\d.]+) LUFS", stderr.decode(errors="ignore"))
    if not matches:
        raise RuntimeError("could not measure loudness")
    return float(matches[-1])


async def run_capture(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


async def run_checked(*args: str) -> tuple[str, str]:
    code, out, err = await run_capture(*args)
    if code != 0:
        tail = [l.strip() for l in err.splitlines() if l.strip()][-3:]
        raise RuntimeError(f"{args[0]} failed: {' | '.join(tail)[-400:]}")
    return out, err


async def probe_sample_rate(path: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate",
        "-of", "default=nw=1:nk=1", path,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        return int(stdout.decode().strip())
    except ValueError:
        return DEFAULT_IN_RATE


async def probe_duration(path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", path,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return 0.0


def atempo_chain(factor: float) -> list[str]:
    """atempo only accepts 0.5–2.0 per instance, so split larger/smaller factors."""
    filters = []
    while factor > 2.0:
        filters.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        filters.append("atempo=0.5")
        factor /= 0.5
    if abs(factor - 1.0) > 1e-6:
        filters.append(f"atempo={factor:.6f}")
    return filters


def build_main_filter(in_rate: int, s: BaitSettings) -> str:
    pitch_ratio = 2 ** (s.pitch / 12)

    # Raising the apparent rate shifts pitch and tempo together by speed * pitch_ratio.
    # atempo then removes the pitch-ratio tempo change, leaving the requested tempo.
    apparent_rate = round(in_rate * s.speed * pitch_ratio)
    resample = ["aresample=192000", f"aresample={OUT_RATE}"] if s.safe_peaks else [f"aresample={OUT_RATE}"]
    filters = [f"asetrate={apparent_rate}"] + resample
    filters += atempo_chain(s.tempo / pitch_ratio)

    # Bass EQ runs before gain so the gain doesn't change the bass balance.
    if abs(s.bass_db) > 1e-6:
        filters.append(f"equalizer=f={BASS_FREQ}:width_type=o:width=1:g={s.bass_db:.2f}")

    if s.reverb in REVERB_PRESETS:
        filters.append(f"aecho={REVERB_PRESETS[s.reverb]}")

    if s.fx:
        filters.append(s.fx)

    # Raw gain. Overs stay in float until the final encode, unless after_gain shapes them.
    if abs(s.gain_db) > 1e-6:
        linear = 10 ** (s.gain_db / 20)
        filters.append(f"volume={linear:.6f}:precision=double")

    if s.after_gain:
        filters.append(s.after_gain)

    return ",".join(filters)


def clip_rate_after_gain(hz: int) -> str:
    """After-gain step: clip at a lower rate, then return to OUT_RATE. Used by /master and /amplify."""
    return (
        f"aresample={hz},"
        "aformat=sample_fmts=s16,"
        f"aresample={OUT_RATE},"
        "lowpass=f=11000"
    )


async def decode_to_raw(src: str, dst: str, af: str | None = None):
    """Decode to headerless float32 stereo at OUT_RATE. Nothing is clipped here."""
    args = ["-i", src]
    if af:
        args += ["-af", af]
    args += RAW_FMT + [dst]
    await run_ffmpeg(args)


async def decode_mix_to_raw(src: str, dst: str, chain_a: str, chain_b: str):
    """Run two chains on the same source in parallel and sum them into one float32 file."""
    graph = (
        "[0:a]asplit=2[a][b];"
        f"[a]{chain_a}[ta];"
        f"[b]{chain_b}[tb];"
        "[ta][tb]amix=inputs=2:normalize=0[out]"
    )
    args = ["-i", src, "-filter_complex", graph, "-map", "[out]"] + RAW_FMT + [dst]
    await run_ffmpeg(args)


async def apply_chain_to_raw(src_raw: str, dst_raw: str, af: str):
    """Run a filter chain over an already-joined float32 file."""
    args = ["-f", "f32le", "-ar", str(OUT_RATE), "-ac", "2", "-i", src_raw, "-af", af]
    args += RAW_FMT + [dst_raw]
    await run_ffmpeg(args)


def join_raw(paths: list[str], out_path: str):
    """Raw float32 files with the same format concatenate byte for byte."""
    with open(out_path, "wb") as out:
        for p in paths:
            with open(p, "rb") as f:
                while chunk := f.read(1 << 20):
                    out.write(chunk)


async def build_audio(
    input_path: str,
    output_path: str,
    settings: BaitSettings,
    intro_path: str | None = None,
) -> str | None:
    """
    Process the track as float32.

    - two_pass_loudnorm: measure the track first, then use fixed loudnorm values (/master).
    - /bait (wrap): pre clip, intro, processed track, outro, joined as float.
    - mix_tracks: two chains run in parallel on the track and summed (/angelic).
    - add_intro: the intro file is joined before the song, then the whole thing goes
      through the same effect chain, so the effects apply to both.
    - otherwise: just the processed track.

    If match_output is set, the final file's loudness is corrected to the input's.
    Returns a loudness note when matching, otherwise None.
    """
    in_rate = await probe_sample_rate(input_path)

    if settings.two_pass_loudnorm:
        fx = await measure_loudnorm(input_path, settings.two_pass_loudnorm)
        settings = replace(settings, fx=fx)

    with tempfile.TemporaryDirectory() as tmp:
        main_raw = os.path.join(tmp, "main.raw")
        joined_raw = os.path.join(tmp, "joined.raw")

        if settings.mix_tracks is not None:
            chain_a, chain_b = settings.mix_tracks
            await decode_mix_to_raw(input_path, main_raw, chain_a, chain_b)
        elif settings.add_intro and intro_path and not settings.wrap:
            song_raw = os.path.join(tmp, "song.raw")
            intro_raw = os.path.join(tmp, "intro.raw")
            combined_raw = os.path.join(tmp, "combined.raw")
            await decode_to_raw(input_path, song_raw)
            await decode_to_raw(intro_path, intro_raw)
            join_raw([intro_raw, song_raw], combined_raw)
            await apply_chain_to_raw(combined_raw, main_raw, build_main_filter(OUT_RATE, settings))
        else:
            await decode_to_raw(input_path, main_raw, build_main_filter(in_rate, settings))

        if settings.wrap:
            parts = []
            if os.path.exists(PRE_PATH):
                pre_raw = os.path.join(tmp, "pre.raw")
                await decode_to_raw(PRE_PATH, pre_raw)
                parts.append(pre_raw)
            else:
                log.warning("assets/pre.ogg not found, skipping pre clip")

            intro_wrap_raw = os.path.join(tmp, "intro_wrap.raw")
            outro_raw = os.path.join(tmp, "outro.raw")
            await decode_to_raw(INTRO_PATH, intro_wrap_raw)
            await decode_to_raw(OUTRO_PATH, outro_raw)

            join_raw(parts + [intro_wrap_raw, main_raw, outro_raw], joined_raw)
            source = joined_raw
        else:
            source = main_raw

        final_af = None
        in_lufs = None
        if settings.match_output:
            in_lufs = await measure_lufs(input_path)
            out_lufs = await measure_lufs(source, raw=True)
            correction_db = in_lufs - out_lufs
            log.info(f"output match: input {in_lufs:.1f} LUFS, output {out_lufs:.1f} -> {correction_db:+.1f} dB")
            if abs(correction_db) > 0.1:
                linear = 10 ** (correction_db / 20)
                final_af = f"volume={linear:.6f}:precision=double"

        args = ["-f", "f32le", "-ar", str(OUT_RATE), "-ac", "2", "-i", source]
        final_chain = [f for f in [final_af, "alimiter=limit=0.99:attack=1:release=50" if settings.safe_peaks else None] if f]
        if final_chain:
            args += ["-af", ",".join(final_chain)]
        args += ["-c:a", "libvorbis", "-q:a", "6", output_path]
        await run_ffmpeg(args)

        if settings.match_output:
            final_lufs = await measure_lufs(output_path)
            log.info(f"final output {final_lufs:.1f} LUFS (input {in_lufs:.1f})")
            return f"input {in_lufs:.1f} LUFS, output {final_lufs:.1f} LUFS"
        return None


async def build_mix(input_paths: list[str], output_path: str):
    """Join files end to end in the given order, then encode once to OGG."""
    with tempfile.TemporaryDirectory() as tmp:
        raws = []
        for i, path in enumerate(input_paths):
            raw = os.path.join(tmp, f"part{i}.raw")
            await decode_to_raw(path, raw)
            raws.append(raw)
        joined = os.path.join(tmp, "mixed.raw")
        join_raw(raws, joined)
        await run_ffmpeg([
            "-f", "f32le", "-ar", str(OUT_RATE), "-ac", "2", "-i", joined,
            "-c:a", "libvorbis", "-q:a", "6", output_path,
        ])


def list_random_intros() -> list[str]:
    """Every audio file in assets/intros/, sorted. Empty if the folder is missing."""
    if not os.path.isdir(INTROS_DIR):
        return []
    return sorted(
        os.path.join(INTROS_DIR, name)
        for name in os.listdir(INTROS_DIR)
        if name.lower().endswith(AUDIO_EXTS)
    )


async def build_random_intro(input_path: str, output_path: str, intro_path: str):
    """Intro, then the track, then the standard outro. Joined as float, encoded once to OGG."""
    with tempfile.TemporaryDirectory() as tmp:
        intro_raw = os.path.join(tmp, "intro.raw")
        main_raw = os.path.join(tmp, "main.raw")
        outro_raw = os.path.join(tmp, "outro.raw")
        joined_raw = os.path.join(tmp, "joined.raw")

        await decode_to_raw(intro_path, intro_raw)
        await decode_to_raw(input_path, main_raw)
        await decode_to_raw(OUTRO_PATH, outro_raw)
        join_raw([intro_raw, main_raw, outro_raw], joined_raw)

        await run_ffmpeg([
            "-f", "f32le", "-ar", str(OUT_RATE), "-ac", "2", "-i", joined_raw,
            "-c:a", "libvorbis", "-q:a", "6", output_path,
        ])


def is_audio(att: discord.Attachment) -> bool:
    return (att.content_type or "").startswith("audio") or att.filename.lower().endswith(AUDIO_EXTS)


async def run_effect(
    interaction: discord.Interaction,
    settings: BaitSettings,
    label: str,
    file: discord.Attachment,
    intro: discord.Attachment | None = None,
    already_deferred: bool = False,
):
    """Download the file (and optional intro), process it, and send the result."""
    if not already_deferred:
        await interaction.response.defer(thinking=True)

    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, file.filename)
        output_path = os.path.join(tmp, "output.ogg")
        intro_path = None

        await file.save(input_path)
        if intro is not None:
            intro_path = os.path.join(tmp, "intro_" + intro.filename)
            await intro.save(intro_path)
            settings = replace(settings, add_intro=True)

        try:
            note = await build_audio(input_path, output_path, settings, intro_path)
        except Exception as e:
            log.exception("effect build failed")
            await interaction.followup.send(f"Something went wrong: `{str(e)[:1800]}`")
            return

        size = os.path.getsize(output_path)
        if size > MAX_DISCORD_UPLOAD:
            await interaction.followup.send(
                f"Result is {size / (1024*1024):.1f} MB, which is over this server's "
                f"upload limit ({MAX_DISCORD_UPLOAD / (1024*1024):.0f} MB). "
                f"Try a shorter input file."
            )
            return

        out_len = await probe_duration(output_path)
        text = f"Done — {label}. Output length: {out_len:.1f}s."
        if note:
            text += f" Loudness: {note}."
        if intro is not None:
            text += " Intro: your attached file."
        await interaction.followup.send(
            content=text,
            file=discord.File(output_path, filename="output.ogg"),
        )


@tree.command(name="randomintro", description="Random intro from the pack, then the standard outro")
@anywhere
@app_commands.describe(file="The audio file to process")
async def randomintro_cmd(interaction: discord.Interaction, file: discord.Attachment):
    intros = list_random_intros()
    if not intros:
        await interaction.response.send_message(
            "No intros found. Put the intro files in assets/intros/ and redeploy."
        )
        return

    await interaction.response.defer(thinking=True)
    intro_path = random.choice(intros)
    intro_name = os.path.splitext(os.path.basename(intro_path))[0]

    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, file.filename)
        output_path = os.path.join(tmp, "output.ogg")
        await file.save(input_path)

        try:
            await build_random_intro(input_path, output_path, intro_path)
        except Exception as e:
            log.exception("randomintro failed")
            await interaction.followup.send(f"Something went wrong: `{str(e)[:1800]}`")
            return

        size = os.path.getsize(output_path)
        if size > MAX_DISCORD_UPLOAD:
            await interaction.followup.send(
                f"Result is {size / (1024*1024):.1f} MB, which is over this server's "
                f"upload limit ({MAX_DISCORD_UPLOAD / (1024*1024):.0f} MB). "
                f"Try a shorter input file."
            )
            return

        out_len = await probe_duration(output_path)
        await interaction.followup.send(
            content=f"Done — random intro `{intro_name}`, standard outro. Output length: {out_len:.1f}s.",
            file=discord.File(output_path, filename="output.ogg"),
        )


@tree.command(name="bait", description="Wrap a track in pre/intro/outro clips at a speed you pick")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    speed=f"Speed {BAIT_SPEED_MIN:g}x to {BAIT_SPEED_MAX:g}x (tempo and pitch change together)",
)
async def bait_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    speed: app_commands.Range[float, BAIT_SPEED_MIN, BAIT_SPEED_MAX] = BAIT_SPEED_DEFAULT,
):
    settings = BaitSettings(speed=speed, wrap=True, match_output=True, safe_peaks=True)
    await run_effect(interaction, settings, f"{speed:g}x speed, volume matched to input", file)


@tree.command(name="master", description="Normalize to -14 LUFS, then add gain with no limiter")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    gain="Gain in dB after normalizing, 0 to 60. Default 24 gives about 10 LUFS.",
    hz="Clipping rate. auto drops to 22050 Hz above 20 dB. Pick a rate to force it.",
    intro="Optional intro file, placed before the song",
)
@app_commands.choices(hz=[
    app_commands.Choice(name="auto", value=0),
    app_commands.Choice(name="8000 Hz", value=8000),
    app_commands.Choice(name="16000 Hz", value=16000),
    app_commands.Choice(name="22050 Hz", value=22050),
    app_commands.Choice(name="32000 Hz", value=32000),
    app_commands.Choice(name="44100 Hz (no drop)", value=44100),
])
async def master_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    gain: app_commands.Range[float, 0.0, MASTER_GAIN_MAX] = MASTER_GAIN_DB,
    hz: app_commands.Choice[int] | None = None,
    intro: discord.Attachment | None = None,
):
    chosen = hz.value if hz else 0
    if chosen == 0:
        # auto: drop to MASTER_AUTO_HZ only above the threshold
        clip_hz = MASTER_AUTO_HZ if gain > MASTER_AUTO_THRESHOLD_DB else OUT_RATE
        hz_label = f"auto ({clip_hz} Hz)" if clip_hz != OUT_RATE else "auto (44100 Hz)"
    else:
        clip_hz = chosen
        hz_label = f"{chosen} Hz"

    after = clip_rate_after_gain(clip_hz) if clip_hz != OUT_RATE else ""
    settings = BaitSettings(two_pass_loudnorm=MASTER_TARGET, gain_db=gain, after_gain=after)
    await run_effect(
        interaction, settings,
        f"master: -14 LUFS normalize, +{gain:g} dB, clip {hz_label}",
        file, intro,
    )


@tree.command(name="amplify", description="Audacity-style Amplify: scale the peak to a target dB")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    target="Target peak in dB. 40 = add 40 dB over the file's peak.",
    allow_clipping="On (default) applies the full target. Off caps the target at 0 dB, so nothing clips.",
    bass="Bass to remove in dB at 100 Hz, 0 to 40. Default 0 (no cut).",
    hz="Clipping rate when clipping is on. auto drops to 22050 Hz above 20 dB of gain.",
    intro="Optional intro file, placed before the song",
)
@app_commands.choices(hz=[
    app_commands.Choice(name="auto", value=0),
    app_commands.Choice(name="8000 Hz", value=8000),
    app_commands.Choice(name="16000 Hz", value=16000),
    app_commands.Choice(name="22050 Hz", value=22050),
    app_commands.Choice(name="32000 Hz", value=32000),
    app_commands.Choice(name="44100 Hz (no drop)", value=44100),
])
async def amplify_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    target: app_commands.Range[float, AMPLIFY_TARGET_MIN, AMPLIFY_TARGET_MAX] = AMPLIFY_TARGET_DEFAULT,
    allow_clipping: bool = True,
    bass: app_commands.Range[float, 0.0, AMPLIFY_BASS_CUT_MAX] = AMPLIFY_BASS_CUT_DEFAULT,
    hz: app_commands.Choice[int] | None = None,
    intro: discord.Attachment | None = None,
):
    await interaction.response.defer(thinking=True)

    # The bass cut is applied first, so the peak we measure is the peak that will be amplified.
    bass_af = f"equalizer=f={BASS_FREQ}:width_type=o:width=1:g={-bass:.2f}" if bass > 0 else None

    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, file.filename)
        await file.save(input_path)
        try:
            current_peak = await measure_peak_db(input_path, bass_af)
        except Exception as e:
            log.exception("amplify peak measure failed")
            await interaction.followup.send(f"Something went wrong: `{str(e)[:1800]}`")
            return

    # Audacity's Amplify works on the peak: gain = target minus the current peak.
    # With clipping off, the target is capped at 0 dBFS so nothing goes past full scale.
    effective_target = target if allow_clipping else min(target, 0.0)
    gain = effective_target - current_peak

    # Clipping rate: explicit choice wins. In auto, only clipping that's allowed and high gain drops the rate.
    chosen = hz.value if hz else 0
    if chosen != 0:
        clip_hz = chosen
    elif allow_clipping and gain > AMPLIFY_AUTO_THRESHOLD_DB:
        clip_hz = MASTER_AUTO_HZ
    else:
        clip_hz = OUT_RATE
    after = clip_rate_after_gain(clip_hz) if (allow_clipping and clip_hz != OUT_RATE) else ""

    notes = []
    if not allow_clipping and target > 0.0:
        notes.append("capped at 0 dB, clipping off")
    if bass > 0:
        notes.append(f"bass -{bass:g} dB at {BASS_FREQ} Hz")
    if allow_clipping and clip_hz != OUT_RATE:
        notes.append(f"clip {clip_hz} Hz")
    note = f" ({', '.join(notes)})" if notes else ""

    settings = BaitSettings(
        bass_db=-bass,
        gain_db=gain,
        after_gain=after,
    )
    await run_effect(
        interaction,
        settings,
        f"amplify to {effective_target:+g} dB peak (gain {gain:+.2f} dB){note}",
        file,
        intro,
        already_deferred=True,
    )


@tree.command(name="volume", description="Change level in dB")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    db="Level change in dB (-30 to 60). 10 = +10 dB.",
    style="hard (default, loudest, harsher) or clean (smoother, stops getting louder past about +30 dB)",
    intro="Optional intro file, placed before the song",
)
@app_commands.choices(style=[
    app_commands.Choice(name="hard", value="hard"),
    app_commands.Choice(name="clean", value="clean"),
])
async def volume_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    db: app_commands.Range[float, VOLUME_DB_MIN, VOLUME_DB_MAX],
    style: app_commands.Choice[str] | None = None,
    intro: discord.Attachment | None = None,
):
    chosen = style.value if style else "hard"
    if chosen == "clean":
        settings = BaitSettings(fx=CLEAN_PRE, gain_db=db, after_gain=CLEAN_POST)
        label = f"{db:+g} dB, clean"
    elif db > VOLUME_HZ_THRESHOLD_DB:
        settings = BaitSettings(fx=HARD_PRE, gain_db=db, after_gain=HARD_LOW_RATE_POST)
        label = f"{db:+g} dB, hard (clipped at {VOLUME_LOW_HZ} Hz for less static)"
    else:
        settings = BaitSettings(fx=HARD_PRE, gain_db=db, after_gain=HARD_POST)
        label = f"{db:+g} dB, hard"
    await run_effect(interaction, settings, label, file, intro)


@tree.command(name="bass", description="Remove bass by cutting the low end around 100 Hz")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    cut="How many dB of bass to remove, 0 to 40. Default 30.",
    intro="Optional intro file, placed before the song",
)
async def bass_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    cut: app_commands.Range[float, 0.0, BASS_CUT_MAX] = BASS_CUT_DEFAULT,
    intro: discord.Attachment | None = None,
):
    settings = BaitSettings(bass_db=-cut)
    await run_effect(interaction, settings, f"bass removed: -{cut:g} dB at {BASS_FREQ} Hz", file, intro)


@tree.command(name="mix", description="Join two audio files, the first one plays first")
@anywhere
@app_commands.describe(
    first="Plays first",
    second="Plays second",
)
async def mix_cmd(
    interaction: discord.Interaction,
    first: discord.Attachment,
    second: discord.Attachment,
):
    await interaction.response.defer(thinking=True)
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, att in enumerate([first, second]):
            path = os.path.join(tmp, f"{i}_{att.filename}")
            await att.save(path)
            paths.append(path)

        output_path = os.path.join(tmp, "mix.ogg")
        try:
            await build_mix(paths, output_path)
        except Exception as e:
            log.exception("mix failed")
            await interaction.followup.send(f"Something went wrong: `{str(e)[:1800]}`")
            return

        size = os.path.getsize(output_path)
        if size > MAX_DISCORD_UPLOAD:
            await interaction.followup.send(
                f"Result is {size / (1024*1024):.1f} MB, which is over this server's "
                f"upload limit ({MAX_DISCORD_UPLOAD / (1024*1024):.0f} MB). "
                f"Try shorter files."
            )
            return

        out_len = await probe_duration(output_path)
        await interaction.followup.send(
            content=f"Mixed: {first.filename} then {second.filename}. Output length: {out_len:.1f}s.",
            file=discord.File(output_path, filename="mix.ogg"),
        )


@tree.command(name="pitch", description="Shift pitch in semitones, keeping tempo the same")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    semitones="Pitch shift in semitones (-12 to 12)",
    intro="Optional intro file, placed before the song",
)
async def pitch_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    semitones: app_commands.Range[float, -12.0, 12.0],
    intro: discord.Attachment | None = None,
):
    await run_effect(interaction, BaitSettings(pitch=semitones), f"pitch {semitones:+g} st", file, intro)


@tree.command(name="tempo", description="Change tempo, keeping pitch the same")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    factor="Tempo multiplier (0.5 to 2.0)",
    intro="Optional intro file, placed before the song",
)
async def tempo_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    factor: app_commands.Range[float, 0.5, 2.0],
    intro: discord.Attachment | None = None,
):
    await run_effect(interaction, BaitSettings(tempo=factor), f"tempo {factor:g}x", file, intro)


@tree.command(name="reverb", description="Add a reverb preset to the track")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    preset="Reverb preset",
    intro="Optional intro file, placed before the song",
)
@app_commands.choices(preset=[
    app_commands.Choice(name="room", value="room"),
    app_commands.Choice(name="hall", value="hall"),
    app_commands.Choice(name="cave", value="cave"),
])
async def reverb_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    preset: app_commands.Choice[str],
    intro: discord.Attachment | None = None,
):
    await run_effect(interaction, BaitSettings(reverb=preset.value), f"{preset.name} reverb", file, intro)


@tree.command(name="angelic", description="Angelic fortress set: two tracks processed in parallel and mixed")
@anywhere
@app_commands.describe(file="The audio file to process")
async def angelic_cmd(interaction: discord.Interaction, file: discord.Attachment):
    settings = BaitSettings(mix_tracks=(ANGELIC_TRACK_A, ANGELIC_TRACK_B))
    await run_effect(interaction, settings, "angelic fortress set (two tracks mixed)", file)


def draw_waveform_sync(path: str, png_path: str, width: int = 1920, height: int = 660) -> None:
    """Decode to mono 8 kHz samples with ffmpeg, then draw the waveform with Pillow."""
    if Image is None:
        raise RuntimeError("Pillow is not installed (add 'Pillow' to requirements.txt)")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
         "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        stdin=subprocess.DEVNULL, capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"decode failed: {proc.stderr.decode(errors='ignore')[-300:]}")
    samples = array.array("h")
    samples.frombytes(proc.stdout)
    n = len(samples)
    if n == 0:
        raise RuntimeError("no audio samples to draw")

    img = Image.new("RGB", (width, height), (12, 12, 18))
    draw = ImageDraw.Draw(img)
    mid = height // 2
    per = n / width
    for x in range(width):
        start = int(x * per)
        end = max(start + 1, int((x + 1) * per))
        seg = samples[start:end]
        peak = max(max(seg), -min(seg))
        h = int(peak / 32767 * (height / 2))
        draw.line([(x, mid - h), (x, mid + h)], fill=(100, 100, 220))
    img.save(png_path)


async def analyze_loudness(path: str) -> tuple[float | None, float | None]:
    """Integrated loudness and true peak from ebur128 (used by /analyze)."""
    _, _, err = await run_capture(
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-af", "ebur128=peak=true", "-f", "null", "-",
    )
    lufs = re.search(r"Integrated loudness:.*?I:\s+(-?[\d.]+) LUFS", err, re.S)
    peak = re.search(r"True peak:.*?Peak:\s+(-?[\d.]+) dBFS", err, re.S)
    return (
        float(lufs.group(1)) if lufs else None,
        float(peak.group(1)) if peak else None,
    )


def fmt_metric(value: float | None, unit: str) -> str:
    return "n/a" if value is None else f"{value} {unit}"


@tree.command(name="analyze", description="Audio file info: length, bitrate, loudness, peak, and waveform")
@anywhere
@app_commands.describe(file="The audio file to analyze")
async def analyze_cmd(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(thinking=True)
    with tempfile.TemporaryDirectory() as tmp:
        ext = os.path.splitext(file.filename)[1] or ".mp3"
        inp = os.path.join(tmp, f"input{ext}")
        png = os.path.join(tmp, "waveform.png")
        await file.save(inp)

        try:
            out, _ = await run_checked(
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", inp,
            )
            data = json.loads(out)
            fmt = data["format"]
            stream = next(s for s in data["streams"] if s.get("codec_type") == "audio")
            lufs, peak = await analyze_loudness(inp)
            await asyncio.to_thread(draw_waveform_sync, inp, png)
        except Exception as e:
            log.exception("analyze failed")
            await interaction.followup.send(f"failed to analyze: `{str(e)[:1500]}`")
            return

        duration = float(fmt.get("duration", 0))
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        bitrate = round(int(fmt.get("bit_rate", 0)) / 1000)
        await interaction.followup.send(
            content=(
                f"processed file `{file.filename}`\n"
                f"duration: {minutes}:{seconds:02d}\n"
                f"bitrate: {bitrate} kbps\n"
                f"sample rate: {stream.get('sample_rate', 'n/a')}hz\n"
                f"integrated: {fmt_metric(lufs, 'LUFS')}\n"
                f"peak: {fmt_metric(peak, 'dBFS')}"
            ),
            file=discord.File(png, filename="waveform.png"),
        )


@tree.command(name="cr", description="Speed change plus a spectrogram notch")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    speed="Speed change, -20 to 20 percent. Default 5.",
    low="Notch low edge in Hz. Default 1450.",
    high="Notch high edge in Hz. Default 3500.",
)
async def cr_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    speed: app_commands.Range[float, CR_SPEED_MIN, CR_SPEED_MAX] = CR_SPEED_DEFAULT,
    low: app_commands.Range[float, 0.0, 22050.0] = CR_LOW_DEFAULT,
    high: app_commands.Range[float, 0.0, 22050.0] = CR_HIGH_DEFAULT,
):
    if str(interaction.user.id) == CR_BLOCKED_USER_ID:
        await interaction.response.send_message("you do not have access to this command.")
        return
    if random.randrange(1500) == 1:
        await interaction.response.send_message("man fuck you")
        return

    await interaction.response.defer(thinking=True)
    with tempfile.TemporaryDirectory() as tmp:
        ext = os.path.splitext(file.filename)[1] or ".mp3"
        inp = os.path.join(tmp, f"input{ext}")
        out = os.path.join(tmp, "output.mp3")
        spec = os.path.join(tmp, "spectrogram.png")
        await file.save(inp)

        try:
            sr = await probe_sample_rate(inp)
            mult = 1 + speed / 100
            center = (low + high) / 2
            width = abs(high - low)
            eq = (
                f"anequalizer=c0 f={center} w={width} g=-90 t=2"
                f"|c1 f={center} w={width} g=-90 t=2"
            )
            af = f"asetrate={round(sr * mult)},aresample=48000,{eq},{eq},{eq}"
            await run_checked(
                "ffmpeg", "-y", "-i", inp, "-af", af,
                "-c:a", "libmp3lame", "-b:a", "192k", out,
            )
            await run_checked(
                "ffmpeg", "-y", "-i", out,
                "-lavfi", "showspectrumpic=s=1000x240:legend=0:gain=.5:color=cool",
                spec,
            )
        except Exception as e:
            log.exception("cr failed")
            await interaction.followup.send(f"failed. `{str(e)[:1500]}`")
            return

        await interaction.followup.send(
            content="applied spectrogram hole",
            files=[
                discord.File(out, filename="output.mp3"),
                discord.File(spec, filename="spectrogram.png"),
            ],
        )


def make_fx_command(name: str, description: str, chain: str):
    """One slash command per effect. Each closure keeps its own chain."""

    @tree.command(name=name, description=description[:100])
    @anywhere
    @app_commands.describe(
        file="The audio file to process",
        intro="Optional intro file, placed before the song",
    )
    async def fx_cmd(
        interaction: discord.Interaction,
        file: discord.Attachment,
        intro: discord.Attachment | None = None,
    ):
        await run_effect(interaction, BaitSettings(fx=chain), name, file, intro)

    return fx_cmd


for _name, _desc, _chain in FX_COMMANDS:
    make_fx_command(_name, _desc, _chain)


@tree.command(name="ping", description="Check the bot is online and show host info")
@anywhere
async def ping_cmd(interaction: discord.Interaction):
    cpu = platform.processor() or platform.machine()
    ram_text = "unknown"
    if psutil is not None:
        mem = psutil.virtual_memory()
        used = getattr(mem, "active", mem.used)
        ram_text = f"{used / 1024**3:.2f}GB / {mem.total / 1024**3:.2f}GB"
    await interaction.response.send_message(
        f"im awake and running on {socket.gethostname()}\n\n"
        f"os: {platform.system()} {platform.release()}\n"
        f"cpu: {cpu} ({os.cpu_count()} cores)\n"
        f"ram: {ram_text}\n"
        f"gpu: Integrated"
    )


@tree.command(name="help", description="List all commands")
@anywhere
async def help_cmd(interaction: discord.Interaction):
    lines = [
        "**Commands**",
        "Each command takes an audio file. Add an optional intro file to put it before the song.",
        "",
        "**Core**",
        "`/randomintro`: a random intro from the pack, then the standard outro.",
        "`/bait`: wrap in pre, intro, and outro clips at a speed you pick (0.5x to 2.0x). Volume matched to input.",
        "`/amplify`: Audacity-style Amplify. The target is the peak in dB, so 40 adds 40 dB. Clipping is on by default; turn it off to cap at 0 dB. Options: bass cut and clip rate (hz).",
        "`/master`: normalize to -14 LUFS, then add gain with no limiter. Default 24 dB. Pick a clip rate with `hz`, or leave it on auto (drops to 22050 Hz above 20 dB).",
        "`/volume`: change level in dB. Hard is loudest, clean is smoother. Above 20 dB, hard drops to 22.05 kHz for less static.",
        "`/bass`: remove bass around 100 Hz, default 30 dB.",
        "`/pitch`: shift pitch in semitones, tempo unchanged.",
        "`/tempo`: change tempo, pitch unchanged.",
        "`/reverb`: room, hall, or cave.",
        "`/mix`: join two files, the first plays first.",
        "`/angelic`: two-track Audacity set, mixed.",
        "`/analyze`: length, bitrate, loudness, peak, and waveform.",
        "`/cr`: speed change with a spectrogram notch.",
        "`/ping`: check the bot is online.",
        "",
        "**Sets (orzxjo)**",
    ]
    lines += [f"`/{name}`: {desc}" for name, desc, _ in FX_COMMANDS[:SET_COUNT]]
    lines.append("")
    lines.append("**Effects**")
    lines += [f"`/{name}`: {desc}" for name, desc, _ in FX_COMMANDS[SET_COUNT:]]

    chunks = []
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 1900:
            chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        chunks.append(chunk)

    await interaction.response.send_message(chunks[0])
    for extra in chunks[1:]:
        await interaction.followup.send(extra)



@tree.command(name="vol", description="Adjust volume and bass of an audio file by dB")
@anywhere
@app_commands.describe(
    file="The audio file to adjust",
    db="Volume change in dB, e.g. 5 or -10 (range: -100 to 100)",
    bass="Bass change in dB, e.g. 10 or -20 (range: -100 to 100). Optional.",
)
async def vol_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    db: app_commands.Range[float, -100.0, 100.0],
    bass: app_commands.Range[float, -100.0, 100.0] = 0.0,
):
    await interaction.response.defer(thinking=True)
    with tempfile.TemporaryDirectory() as tmp:
        ext = os.path.splitext(file.filename)[1] or ".mp3"
        inp = os.path.join(tmp, f"vol_in{ext}")
        out = os.path.join(tmp, "vol_out.ogg")
        await file.save(inp)
        try:
            filters = [f"volume={db}dB"]
            if bass != 0.0:
                filters.append(f"bass=g={bass}")
            filters += ["aresample=48000", "aformat=channel_layouts=stereo"]
            af = ",".join(filters)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", inp,
                "-map", "0:a:0",
                "-af", af,
                "-c:a", "libopus", "-b:a", "192k",
                out,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
                err = ffmpeg_error_text(stderr)
                await interaction.followup.send(f"Volume adjustment failed: `{err}`")
                return
        except Exception as e:
            log.exception("vol failed")
            await interaction.followup.send(f"Something went wrong: `{str(e)[:1800]}`")
            return

        size = os.path.getsize(out)
        if size > MAX_DISCORD_UPLOAD:
            await interaction.followup.send(
                f"Result is {size / (1024*1024):.1f} MB, over the upload limit "
                f"({MAX_DISCORD_UPLOAD / (1024*1024):.0f} MB). Try a shorter file."
            )
            return

        vol_sign = "+" if db >= 0 else ""
        bass_sign = "+" if bass >= 0 else ""
        label = f"Volume: {vol_sign}{db} dB"
        if bass != 0.0:
            label += f" | Bass: {bass_sign}{bass} dB"
        await interaction.followup.send(
            content=label,
            file=discord.File(out, filename="vol_out.ogg"),
        )


@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    await tree.sync()
    log.info(f"Logged in as {bot.user}, synced commands to guild {GUILD_ID} and globally")


if __name__ == "__main__":
    bot.run(TOKEN)
