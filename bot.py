import os
import re
import json
import random
import socket
import platform
import asyncio
import tempfile
import logging
from dataclasses import dataclass, replace

import discord
from discord.ext import commands

try:
    import psutil
except ImportError:
    psutil = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bait-bot")

TOKEN = os.environ["DISCORD_TOKEN"]
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
PRE_PATH = os.path.join(ASSETS_DIR, "pre.ogg")  # plays before the intro in .bait, optional
INTRO_PATH = os.path.join(ASSETS_DIR, "intro.mp3")  # default intro when no intro file is attached
OUTRO_PATH = os.path.join(ASSETS_DIR, "outro.mp3")

MAX_DISCORD_UPLOAD = 25 * 1024 * 1024  # 25 MB, adjust for your server's boost level
OUT_RATE = 44100
DEFAULT_IN_RATE = 44100
BASS_FREQ = 100  # Hz, center of the bass cut/boost
AUDIO_EXTS = (".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac", ".opus", ".webm", ".mp4")

# .bait speed range. Any value in this range works.
BAIT_SPEED_MIN = 0.5
BAIT_SPEED_MAX = 2.0
BAIT_SPEED_DEFAULT = 1.4

# .volume range in dB. Clean style stops getting louder once the limiter takes over.
VOLUME_DB_MIN = -30.0
VOLUME_DB_MAX = 60.0

# .master: normalize to -14 LUFS with two-pass loudnorm (measure, then apply fixed values),
# then add gain with no limiter. +24 dB lands at about 10 LUFS, matching the uploaded clips.
MASTER_TARGET = "I=-14:TP=-1:LRA=11"
MASTER_GAIN_DB = 24.0
MASTER_GAIN_MAX = 60.0

# .loud defaults. Gain is in dB, bass is in dB at BASS_FREQ (-40 cuts hard, +40 boosts hard).
LOUD_GAIN_DB = 70.0
LOUD_BASS_DB = 0.0
LOUD_BASS_MIN = -40.0
LOUD_BASS_MAX = 40.0
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
# loud / hard: the gain goes straight to the hard clip at encode, so every dB stays audible.
# A low cut and top-end roll-off take some of the harshness out of the clipping.
HARD_PRE = "highpass=f=35"
HARD_POST = "lowpass=f=11000"

# .loudset defaults.
LOUDSET_GAIN_DB = 80.0
LOUDSET_SMOOTH = (
    "asoftclip=type=tanh,"
    "lowpass=f=8000,"
    f"equalizer=f={BASS_FREQ}:width_type=o:width=1:g=3"
)
LOUDSET_HARD = (
    "lowpass=f=8000,"
    f"equalizer=f={BASS_FREQ}:width_type=o:width=1:g=3"
)

# .angelic: two tracks from the Audacity project, processed separately and mixed.
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

# .cr defaults, from the discord.js cr command.
CR_SPEED_DEFAULT = 5.0
CR_SPEED_MIN = -20.0
CR_SPEED_MAX = 20.0
CR_LOW_DEFAULT = 1450.0
CR_HIGH_DEFAULT = 3500.0
# Discord user ID that .cr refuses, carried over from the original command. Delete to remove the block.
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

# Number of entries at the start of FX_COMMANDS that are sets (orzxjo), listed as Sets in .help.
SET_COUNT = 7

intents = discord.Intents.default()
intents.message_content = True  # needed to read ".command" text
bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)


@dataclass(frozen=True)
class BaitSettings:
    speed: float = 1.0          # tempo + pitch together (turntable style)
    pitch: float = 0.0          # semitones, tempo preserved
    tempo: float = 1.0          # tempo multiplier, pitch preserved
    reverb: str = "off"         # key of REVERB_PRESETS, or "off"
    volume: float = 1.0         # multiplier on the main track, limited
    gain_db: float = 0.0        # raw dB boost on the main track, allowed to clip
    bass_db: float = 0.0        # dB at BASS_FREQ: positive boosts, negative cuts, 0 = unchanged
    fx: str = ""                # raw ffmpeg filter chain before gain, or "" for none
    after_gain: str = ""        # raw ffmpeg filter chain after gain, or "" for none
    two_pass_loudnorm: str = ""  # target for two-pass loudnorm (e.g. "I=-14:TP=-1:LRA=11"), or ""
    mix_tracks: tuple[str, str] | None = None  # two chains run in parallel and mixed (.angelic)
    add_intro: bool = False     # put your intro before the song; effects apply to both
    wrap: bool = False          # add pre, intro, and outro clips around the track (only .bait)
    match_output: bool = False  # make the whole output match the input's loudness (LUFS)


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
    filters = [f"asetrate={apparent_rate}", f"aresample={OUT_RATE}"]
    filters += atempo_chain(s.tempo / pitch_ratio)

    # Bass EQ runs before gain so the gain doesn't change the bass balance.
    if abs(s.bass_db) > 1e-6:
        filters.append(f"equalizer=f={BASS_FREQ}:width_type=o:width=1:g={s.bass_db:.2f}")

    if s.reverb in REVERB_PRESETS:
        filters.append(f"aecho={REVERB_PRESETS[s.reverb]}")

    if s.fx:
        filters.append(s.fx)

    if abs(s.volume - 1.0) > 1e-6:
        filters.append(f"volume={s.volume:.4f}:precision=double")
        if s.volume > 1.0:
            filters.append("alimiter=limit=0.95")

    # Raw gain. Overs stay in float until the final encode, unless after_gain shapes them.
    if abs(s.gain_db) > 1e-6:
        linear = 10 ** (s.gain_db / 20)
        filters.append(f"volume={linear:.6f}:precision=double")

    if s.after_gain:
        filters.append(s.after_gain)

    return ",".join(filters)


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

    - two_pass_loudnorm: measure the track first, then use fixed loudnorm values (.master).
    - .bait (wrap): pre clip, intro, processed track, outro, joined as float.
    - mix_tracks: two chains run in parallel on the track and summed (.angelic).
    - add_intro: the intro (intro_path if given, otherwise the bot's default intro)
      is joined before the song, then the whole thing goes through the same
      effect chain, so the sets apply to both.
    - otherwise: just the processed track.

    If match_output is set, the final file's loudness is corrected to the input's.
    Returns a loudness note when matching, otherwise None.
    """
    in_rate = await probe_sample_rate(input_path)
    use_intro = intro_path or INTRO_PATH

    if settings.two_pass_loudnorm:
        fx = await measure_loudnorm(input_path, settings.two_pass_loudnorm)
        settings = replace(settings, fx=fx)

    with tempfile.TemporaryDirectory() as tmp:
        main_raw = os.path.join(tmp, "main.raw")
        joined_raw = os.path.join(tmp, "joined.raw")

        if settings.mix_tracks is not None:
            chain_a, chain_b = settings.mix_tracks
            await decode_mix_to_raw(input_path, main_raw, chain_a, chain_b)
        elif settings.add_intro and not settings.wrap:
            song_raw = os.path.join(tmp, "song.raw")
            intro_raw = os.path.join(tmp, "intro.raw")
            combined_raw = os.path.join(tmp, "combined.raw")
            await decode_to_raw(input_path, song_raw)
            await decode_to_raw(use_intro, intro_raw)
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
        if final_af:
            args += ["-af", final_af]
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


def is_audio(att: discord.Attachment) -> bool:
    return (att.content_type or "").startswith("audio") or att.filename.lower().endswith(AUDIO_EXTS)


async def get_replied_audio(ctx: commands.Context) -> discord.Attachment | None:
    """The first audio file on the message being replied to, if any."""
    ref = ctx.message.reference
    if ref is None or ref.message_id is None:
        return None
    replied = ref.resolved if isinstance(ref.resolved, discord.Message) else None
    if replied is None:
        try:
            replied = await ctx.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return None
    return next((a for a in replied.attachments if is_audio(a)), None)


async def resolve_inputs(ctx: commands.Context) -> tuple[discord.Attachment | None, discord.Attachment | None]:
    """
    Returns (song, intro_file).

    - Replying to an audio file: that file is the song. An audio file on the
      command message is the intro.
    - No reply: the first audio file on the command message is the song, and a
      second one is the intro.
    """
    own = [a for a in ctx.message.attachments if is_audio(a)]
    replied = await get_replied_audio(ctx)
    if replied is not None:
        return replied, (own[0] if own else None)
    if own:
        return own[0], (own[1] if len(own) > 1 else None)
    return None, None


async def run_effect(ctx: commands.Context, settings: BaitSettings, label: str):
    song, intro_att = await resolve_inputs(ctx)
    if song is None:
        await ctx.reply(
            "Attach an audio file to your command, or reply to a message that has one. "
            "For a custom intro, attach a second audio file (the song first, the intro second)."
        )
        return

    async with ctx.typing():
        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, song.filename)
            output_path = os.path.join(tmp, "output.ogg")
            intro_path = None

            await song.save(input_path)
            if settings.add_intro and intro_att is not None:
                intro_path = os.path.join(tmp, "intro_" + intro_att.filename)
                await intro_att.save(intro_path)

            try:
                note = await build_audio(input_path, output_path, settings, intro_path)
            except Exception as e:
                log.exception("effect build failed")
                msg = f"Something went wrong: `{e}`"
                await ctx.reply(msg[:1900])
                return

            size = os.path.getsize(output_path)
            if size > MAX_DISCORD_UPLOAD:
                await ctx.reply(
                    f"Result is {size / (1024*1024):.1f} MB, which is over this server's "
                    f"upload limit ({MAX_DISCORD_UPLOAD / (1024*1024):.0f} MB). "
                    f"Try a shorter input file."
                )
                return

            out_len = await probe_duration(output_path)
            text = f"Done — {label}. Output length: {out_len:.1f}s."
            if note:
                text += f" Loudness: {note}."
            if settings.add_intro:
                text += " Intro: " + ("your attached file." if intro_path else "the bot's default intro.")
            await ctx.reply(
                content=text,
                file=discord.File(output_path, filename="output.ogg"),
            )


def parse_args(args: tuple[str, ...]) -> tuple[bool, list[str]]:
    """Pull out the word 'intro' and return the rest in order."""
    intro = any(a.lower() == "intro" for a in args)
    rest = [a for a in args if a.lower() != "intro"]
    return intro, rest


def parse_number(rest: list[str], idx: int, name: str, lo: float, hi: float, default: float | None) -> float:
    if idx >= len(rest):
        if default is None:
            raise ValueError(f"Missing {name}.")
        return default
    try:
        value = float(rest[idx])
    except ValueError:
        raise ValueError(f"{name} must be a number, got `{rest[idx]}`.")
    if not (lo <= value <= hi):
        raise ValueError(f"{name} must be between {lo:g} and {hi:g}.")
    return value


async def handle(ctx: commands.Context, fn):
    """Run a command body and send friendly errors for bad input."""
    try:
        await fn()
    except ValueError as e:
        await ctx.reply(str(e))


@bot.command(name="mix")
async def mix_cmd(ctx: commands.Context, *args: str):
    """Join two audio files, first one plays first. Usage: attach two files, type .mix"""

    async def go():
        own = [a for a in ctx.message.attachments if is_audio(a)]
        replied = await get_replied_audio(ctx)

        # Order: a replied-to file comes first, then the attached files in the order sent.
        ordered = []
        if replied is not None:
            ordered.append(replied)
        ordered += own

        if len(ordered) < 2:
            await ctx.reply(
                "Attach two audio files to one message, then type `.mix`. "
                "The first file plays first. You can also reply to a bot file and attach one more."
            )
            return

        ordered = ordered[:2] if len(ordered) > 2 else ordered

        async with ctx.typing():
            with tempfile.TemporaryDirectory() as tmp:
                paths = []
                for i, att in enumerate(ordered):
                    path = os.path.join(tmp, f"{i}_{att.filename}")
                    await att.save(path)
                    paths.append(path)

                output_path = os.path.join(tmp, "mix.ogg")
                try:
                    await build_mix(paths, output_path)
                except Exception as e:
                    log.exception("mix failed")
                    await ctx.reply(f"Something went wrong: `{str(e)[:1800]}`")
                    return

                size = os.path.getsize(output_path)
                if size > MAX_DISCORD_UPLOAD:
                    await ctx.reply(
                        f"Result is {size / (1024*1024):.1f} MB, which is over this server's "
                        f"upload limit ({MAX_DISCORD_UPLOAD / (1024*1024):.0f} MB). "
                        f"Try shorter files."
                    )
                    return

                out_len = await probe_duration(output_path)
                names = " then ".join(a.filename for a in ordered)
                await ctx.reply(
                    content=f"Mixed: {names}. Output length: {out_len:.1f}s.",
                    file=discord.File(output_path, filename="mix.ogg"),
                )

    await handle(ctx, go)


@bot.command(name="master")
async def master_cmd(ctx: commands.Context, *args: str):
    """Normalize to -14 LUFS (two-pass), then add gain with no limiter. Usage: .master [dB 0-60] [intro]"""
    intro, rest = parse_args(args)

    async def go():
        gain = parse_number(rest, 0, "gain dB", 0.0, MASTER_GAIN_MAX, MASTER_GAIN_DB)
        settings = BaitSettings(
            two_pass_loudnorm=MASTER_TARGET,
            gain_db=gain,
            add_intro=intro,
        )
        await run_effect(ctx, settings, f"master: -14 LUFS normalize, +{gain:g} dB, no limiter")

    await handle(ctx, go)


@bot.command(name="angelic")
async def angelic_cmd(ctx: commands.Context, *args: str):
    """Angelic fortress set: two tracks processed in parallel and mixed. Usage: .angelic"""

    async def go():
        settings = BaitSettings(mix_tracks=(ANGELIC_TRACK_A, ANGELIC_TRACK_B))
        await run_effect(ctx, settings, "angelic fortress set (two tracks mixed)")

    await handle(ctx, go)


@bot.command(name="bait")
async def bait_cmd(ctx: commands.Context, *args: str):
    """Wrap the track in pre/intro/outro clips. Usage: .bait [speed]"""
    _, rest = parse_args(args)

    async def go():
        speed = parse_number(rest, 0, "speed", BAIT_SPEED_MIN, BAIT_SPEED_MAX, BAIT_SPEED_DEFAULT)
        settings = BaitSettings(speed=speed, wrap=True, match_output=True)
        await run_effect(ctx, settings, f"{speed:g}x speed, volume matched to input")

    await handle(ctx, go)


@bot.command(name="pitch")
async def pitch_cmd(ctx: commands.Context, *args: str):
    """Shift pitch in semitones. Usage: .pitch <-12 to 12> [intro]"""
    intro, rest = parse_args(args)

    async def go():
        semis = parse_number(rest, 0, "semitones", -12.0, 12.0, None)
        await run_effect(ctx, BaitSettings(pitch=semis, add_intro=intro), f"pitch {semis:+g} st")

    await handle(ctx, go)


@bot.command(name="tempo")
async def tempo_cmd(ctx: commands.Context, *args: str):
    """Change tempo, pitch kept. Usage: .tempo <0.5 to 2.0> [intro]"""
    intro, rest = parse_args(args)

    async def go():
        factor = parse_number(rest, 0, "tempo", 0.5, 2.0, None)
        await run_effect(ctx, BaitSettings(tempo=factor, add_intro=intro), f"tempo {factor:g}x")

    await handle(ctx, go)


@bot.command(name="reverb")
async def reverb_cmd(ctx: commands.Context, *args: str):
    """Add reverb. Usage: .reverb <room|hall|cave> [intro]"""
    intro, rest = parse_args(args)

    async def go():
        preset = rest[0].lower() if rest else ""
        if preset not in REVERB_PRESETS:
            raise ValueError("Pick a reverb: room, hall, or cave.")
        await run_effect(ctx, BaitSettings(reverb=preset, add_intro=intro), f"{preset} reverb")

    await handle(ctx, go)


@bot.command(name="volume")
async def volume_cmd(ctx: commands.Context, *args: str):
    """Change level in dB. Usage: .volume <-30 to 60> [clean|hard] [intro]"""
    intro, rest = parse_args(args)

    async def go():
        db = parse_number(rest, 0, "dB", VOLUME_DB_MIN, VOLUME_DB_MAX, None)
        style = "hard"
        if len(rest) > 1:
            style = rest[1].lower()
            if style not in ("clean", "hard"):
                raise ValueError("Style must be clean or hard.")
        if style == "clean":
            settings = BaitSettings(
                fx=CLEAN_PRE,
                gain_db=db,
                after_gain=CLEAN_POST,
                add_intro=intro,
            )
        else:
            settings = BaitSettings(
                fx=HARD_PRE,
                gain_db=db,
                after_gain=HARD_POST,
                add_intro=intro,
            )
        await run_effect(ctx, settings, f"{db:+g} dB, {style}")

    await handle(ctx, go)


@bot.command(name="loud")
async def loud_cmd(ctx: commands.Context, *args: str):
    """Loud. Usage: .loud [dB] [bass -40 to 40] [clean|loud] [intro]"""
    intro, rest = parse_args(args)

    async def go():
        gain = parse_number(rest, 0, "gain dB", 0.0, 100.0, LOUD_GAIN_DB)
        bass = parse_number(rest, 1, "bass dB", LOUD_BASS_MIN, LOUD_BASS_MAX, LOUD_BASS_DB)
        style = "clean"
        if len(rest) > 2:
            style = rest[2].lower()
            if style not in ("clean", "loud"):
                raise ValueError("Style must be clean or loud.")
        if style == "loud":
            pre, post = HARD_PRE, HARD_POST
        else:
            pre, post = CLEAN_PRE, CLEAN_POST
        settings = BaitSettings(
            fx=pre,
            gain_db=gain,
            bass_db=bass,
            after_gain=post,
            add_intro=intro,
        )
        await run_effect(ctx, settings, f"+{gain:g} dB, bass {bass:+g} dB, {style}")

    await handle(ctx, go)


@bot.command(name="loudset")
async def loudset_cmd(ctx: commands.Context, *args: str):
    """Heavy gain. Usage: .loudset [dB] [smooth|hard] [intro]"""
    intro, rest = parse_args(args)

    async def go():
        gain = parse_number(rest, 0, "gain dB", 0.0, 100.0, LOUDSET_GAIN_DB)
        style = "smooth"
        if len(rest) > 1:
            style = rest[1].lower()
            if style not in ("smooth", "hard"):
                raise ValueError("Style must be smooth or hard.")
        after = LOUDSET_HARD if style == "hard" else LOUDSET_SMOOTH
        settings = BaitSettings(gain_db=gain, after_gain=after, add_intro=intro)
        await run_effect(ctx, settings, f"loudset +{gain:g} dB, {style}")

    await handle(ctx, go)


@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    """Check the bot is online and show host info."""
    cpu = platform.processor() or platform.machine()
    ram_text = "unknown"
    if psutil is not None:
        mem = psutil.virtual_memory()
        used = getattr(mem, "active", mem.used)
        ram_text = f"{used / 1024**3:.2f}GB / {mem.total / 1024**3:.2f}GB"
    await ctx.reply(
        f"im awake and running on {socket.gethostname()}\n\n"
        f"os: {platform.system()} {platform.release()}\n"
        f"cpu: {cpu} ({os.cpu_count()} cores)\n"
        f"ram: {ram_text}\n"
        f"gpu: Integrated"
    )


@bot.command(name="analyze")
async def analyze_cmd(ctx: commands.Context):
    """Audio file info: length, bitrate, sample rate, loudness, peak, and waveform. Attach a file or reply to one."""
    source = await resolve_single_audio(ctx)
    if source is None:
        await ctx.reply("Attach an audio file, or reply to one, then type `.analyze`.")
        return

    async with ctx.typing():
        with tempfile.TemporaryDirectory() as tmp:
            ext = os.path.splitext(source.filename)[1] or ".mp3"
            inp = os.path.join(tmp, f"input{ext}")
            png = os.path.join(tmp, "waveform.png")
            await source.save(inp)

            try:
                out, _ = await run_checked(
                    "ffprobe", "-v", "quiet", "-print_format", "json",
                    "-show_format", "-show_streams", inp,
                )
                data = json.loads(out)
                fmt = data["format"]
                stream = next(s for s in data["streams"] if s.get("codec_type") == "audio")
                lufs, peak = await analyze_loudness(inp)

                # One waveform image, drawn from the audio. -frames:v 1 stops after the image is written.
                await run_checked(
                    "ffmpeg", "-y", "-hide_banner", "-i", inp,
                    "-lavfi",
                    "showwavespic=s=1920x660:colors=6464DC:filter=average:split_channels=0",
                    "-frames:v", "1", png,
                )
            except Exception as e:
                log.exception("analyze failed")
                await ctx.reply(f"failed to analyze: `{str(e)[:1500]}`")
                return

            duration = float(fmt.get("duration", 0))
            minutes = int(duration // 60)
            seconds = int(duration % 60)
            bitrate = round(int(fmt.get("bit_rate", 0)) / 1000)
            await ctx.reply(
                content=(
                    f"processed file `{source.filename}`\n"
                    f"duration: {minutes}:{seconds:02d}\n"
                    f"bitrate: {bitrate} kbps\n"
                    f"sample rate: {stream.get('sample_rate', 'n/a')}hz\n"
                    f"integrated: {fmt_metric(lufs, 'LUFS')}\n"
                    f"peak: {fmt_metric(peak, 'dBFS')}"
                ),
                file=discord.File(png, filename="waveform.png"),
            )


@bot.command(name="cr")
async def cr_cmd(ctx: commands.Context, *args: str):
    """Speed change plus a spectrogram notch. Usage: .cr [speed -20 to 20] [low Hz] [high Hz], attach a file."""
    if str(ctx.author.id) == CR_BLOCKED_USER_ID:
        await ctx.reply("you do not have access to this command.")
        return
    if random.randrange(1500) == 1:
        await ctx.reply("man fuck you")
        return

    try:
        speed = float(args[0]) if len(args) > 0 else CR_SPEED_DEFAULT
        low = float(args[1]) if len(args) > 1 else CR_LOW_DEFAULT
        high = float(args[2]) if len(args) > 2 else CR_HIGH_DEFAULT
    except ValueError:
        await ctx.reply("speed, low, and high must be numbers.")
        return
    if not (CR_SPEED_MIN <= speed <= CR_SPEED_MAX):
        await ctx.reply(f"speed must be between {CR_SPEED_MIN:g} and {CR_SPEED_MAX:g}.")
        return

    source = await resolve_single_audio(ctx)
    if source is None:
        await ctx.reply("Attach an audio file, or reply to one, then type `.cr`.")
        return

    async with ctx.typing():
        with tempfile.TemporaryDirectory() as tmp:
            ext = os.path.splitext(source.filename)[1] or ".mp3"
            inp = os.path.join(tmp, f"input{ext}")
            out = os.path.join(tmp, "output.mp3")
            spec = os.path.join(tmp, "spectrogram.png")
            await source.save(inp)

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
                await ctx.reply(f"failed. `{str(e)[:1500]}`")
                return

            await ctx.reply(
                content="applied spectrogram hole",
                files=[
                    discord.File(out, filename="output.mp3"),
                    discord.File(spec, filename="spectrogram.png"),
                ],
            )


async def resolve_single_audio(ctx: commands.Context) -> discord.Attachment | None:
    """The audio file on the command message, or on the message it replies to."""
    for att in ctx.message.attachments:
        if is_audio(att):
            return att
    return await get_replied_audio(ctx)


async def analyze_loudness(path: str) -> tuple[float | None, float | None]:
    """Integrated loudness and true peak from ebur128 (used by .analyze)."""
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


def make_fx_command(name: str, description: str, chain: str):
    """One dot command per effect. Each closure keeps its own chain."""

    @bot.command(name=name, help=description)
    async def fx_cmd(ctx: commands.Context, *args: str):
        intro, _ = parse_args(args)
        await run_effect(ctx, BaitSettings(fx=chain, add_intro=intro), name)

    return fx_cmd


for _name, _desc, _chain in FX_COMMANDS:
    make_fx_command(_name, _desc, _chain)


CORE_HELP = [
    (".mix", "Joins two audio files end to end. The first file plays first."),
    (".master [dB] [intro]", "Normalizes to -14 LUFS (two-pass), then adds gain with no limiter. Default +24 dB gives about 10 LUFS. Up to +60 dB."),
    (".angelic", "Angelic fortress set. Two tracks processed in parallel and mixed together. No intro option."),
    (".bait [speed]", "Wraps your track in pre, intro, and outro clips. Speed 0.5 to 2.0, default 1.4. Volume matched to input."),
    (".pitch <semitones> [intro]", "Shifts pitch, tempo unchanged. -12 to 12."),
    (".tempo <0.5 to 2.0> [intro]", "Changes tempo, pitch unchanged."),
    (".reverb <room|hall|cave> [intro]", "Adds a reverb preset."),
    (".volume <-30 to 60> [clean|hard] [intro]", "Changes level by dB. Hard (default) is loudest but harsher. Clean is smoother but stops getting louder past about +30 dB."),
    (".loud [dB] [bass] [clean|loud] [intro]", "Default +70 dB, bass 0. Clean is smooth but stops getting louder past about +30 dB. Loud keeps getting louder with each dB but is harsher. Bass -40 to +40 at 100 Hz."),
    (".loudset [dB] [smooth|hard] [intro]", "Heavy gain. Smooth avoids the tin-foil sound. Hard is louder but edgier."),
    (".ping", "Checks the bot is online and shows host info."),
    (".analyze", "Attach a file or reply to one. Shows length, bitrate, sample rate, loudness, peak, and a waveform."),
    (".cr [speed] [low] [high]", "Speed change (-20 to 20, default 5) with a spectrogram notch (default 1450 to 3500 Hz). Attach a file."),
]


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    """Show all commands."""
    lines = [
        "**Commands**",
        "Attach an audio file, or reply to one. Replying to a file the bot made chains the effect onto it.",
        "**mix**: attach two files in one message. The first file plays first.",
        "**intro**: add the word `intro` to put an intro before the song. Attach a second audio file "
        "(song first, intro second) to use your own intro, or leave it out to use the bot's default.",
        "",
        "**Core**",
    ]
    for usage, desc in CORE_HELP:
        lines.append(f"`{usage}`: {desc}")
    lines.append("")
    lines.append("**Sets (orzxjo)**")
    for name, desc, _ in FX_COMMANDS[:SET_COUNT]:
        lines.append(f"`.{name}`: {desc}")
    lines.append("")
    lines.append("**Effects**")
    for name, desc, _ in FX_COMMANDS[SET_COUNT:]:
        lines.append(f"`.{name}`: {desc}")

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 1900:
            await ctx.send(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await ctx.send(chunk)


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user}")


if __name__ == "__main__":
    bot.run(TOKEN)
