import os
import re
import math
import asyncio
import tempfile
import logging
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bait-bot")

TOKEN = os.environ["DISCORD_TOKEN"]
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
PRE_PATH = os.path.join(ASSETS_DIR, "pre.ogg")  # plays before the intro in /bait, optional
INTRO_PATH = os.path.join(ASSETS_DIR, "intro.mp3")
OUTRO_PATH = os.path.join(ASSETS_DIR, "outro.mp3")

MAX_DISCORD_UPLOAD = 25 * 1024 * 1024  # 25 MB, adjust for your server's boost level
OUT_RATE = 44100
DEFAULT_IN_RATE = 44100
BASS_FREQ = 100  # Hz, center of the bass cut/boost

# Defaults for /loud, matching the uploaded clip.
LOUD_VOLUME = 3162.0  # about +70 dB
LOUD_BASS_DB = 12.0

# Intermediate format. 32-bit float keeps samples above full scale intact,
# so nothing is clipped until the final encode.
RAW_FMT = ["-f", "f32le", "-ar", str(OUT_RATE), "-ac", "2"]

# aecho: in_gain:out_gain:delays_ms:decays
REVERB_PRESETS = {
    "room": "0.8:0.88:50|80:0.4|0.3",
    "hall": "0.8:0.88:100|150|200:0.4|0.3|0.2",
    "cave": "0.6:0.9:200|300|400:0.5|0.4|0.3",
}

# Each per-effect command: (command name, description, ffmpeg filter chain)
FX_COMMANDS = [
    ("echo", "Add an echo to the main track", "aecho=0.8:0.9:500:0.3"),
    ("flanger", "Flanger sweep on the main track", "flanger"),
    ("phaser", "Phaser sweep on the main track", "aphaser"),
    ("chorus", "Chorus on the main track", "chorus=0.7:0.9:55:0.4:0.25:2"),
    ("tremolo", "Tremolo (volume wobble) on the main track", "tremolo=f=5:d=0.8"),
    ("vibrato", "Vibrato (pitch wobble) on the main track", "vibrato=f=6:d=0.5"),
    ("telephone", "Phone-line bandpass on the main track", "highpass=f=300,lowpass=f=3400"),
    ("radio", "AM radio sound on the main track", "highpass=f=400,lowpass=f=2800,volume=2"),
    ("underwater", "Muffled underwater sound on the main track", "lowpass=f=500,aecho=0.6:0.5:300:0.4"),
    ("buzz", "Buzzing tremolo on the main track", "tremolo=f=30:d=1"),
    ("bassboost", "Boost the bass on the main track", "bass=g=10"),
    ("trebleboost", "Boost the treble on the main track", "treble=g=8"),
    ("bitcrush", "Lo-fi bitcrush on the main track", "acrusher=bits=6:mode=log:aa=1"),
    ("distortion", "Hard distortion on the main track", "acrusher=bits=4:mode=lin:aa=1,volume=2"),
    ("widestereo", "Widen the stereo image on the main track", "extrastereo=m=2.5"),
    ("mono", "Collapse the main track to mono", "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"),
    ("swapchannels", "Swap left and right on the main track", "pan=stereo|c0=c1|c1=c0"),
    ("reverse", "Play the main track backwards (uses more memory on long files)", "areverse"),
    ("vinyl", "Warm vinyl-style sound on the main track", "lowpass=f=8000,crystalizer=i=3"),
    ("crystalize", "Add sparkle to the main track", "crystalizer=i=4"),
    ("softclip", "Soft-clip the main track", "asoftclip=type=tanh"),
    ("compressor", "Squash dynamics on the main track", "acompressor=threshold=-20dB:ratio=6:attack=5:release=50:makeup=6"),
    ("loudnorm", "Normalize the main track to -14 LUFS", "loudnorm=I=-14:TP=-1:LRA=11"),
    ("gate", "Noise gate on the main track", "agate=threshold=0.05"),
    ("tape", "Old cassette tape sound on the main track", "highpass=f=60,lowpass=f=12000,tremolo=f=0.5:d=0.2,crystalizer=i=2"),
]

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


def anywhere(fn):
    """Allow a command to run in servers, DMs, and group DMs when user-installed."""
    fn = app_commands.allowed_installs(guilds=True, users=True)(fn)
    return app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(fn)


@dataclass(frozen=True)
class BaitSettings:
    speed: float = 1.0          # tempo + pitch together (turntable style)
    pitch: float = 0.0          # semitones, tempo preserved
    tempo: float = 1.0          # tempo multiplier, pitch preserved
    reverb: str = "off"         # key of REVERB_PRESETS, or "off"
    volume: float = 1.0         # multiplier on the main track, limited
    gain_db: float = 0.0        # raw dB boost on the main track, allowed to clip
    bass_db: float = 0.0        # dB at BASS_FREQ: positive boosts, negative cuts, 0 = unchanged
    fx: str = ""                # raw ffmpeg filter chain, or "" for none
    wrap: bool = False          # add pre, intro, and outro clips around the track (only /bait)
    match_output: bool = False  # make the whole output match the input's loudness (LUFS)


async def run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='ignore')[-500:]}")


async def measure_lufs(path: str, raw: bool = False) -> float:
    """Integrated loudness (LUFS). Pass raw=True for a headerless float32 file."""
    args = ["ffmpeg", "-hide_banner", "-nostats"]
    if raw:
        args += RAW_FMT
    args += ["-i", path, "-af", "ebur128", "-f", "null", "-"]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    matches = re.findall(r"I:\s+(-?[\d.]+) LUFS", stderr.decode(errors="ignore"))
    if not matches:
        raise RuntimeError("could not measure loudness")
    return float(matches[-1])


async def probe_sample_rate(path: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate",
        "-of", "default=nw=1:nk=1", path,
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

    # Raw gain with no limiter. Overs stay in float until the final encode.
    if abs(s.gain_db) > 1e-6:
        linear = 10 ** (s.gain_db / 20)
        filters.append(f"volume={linear:.6f}:precision=double")

    return ",".join(filters)


async def decode_to_raw(src: str, dst: str, af: str | None = None):
    """Decode to headerless float32 stereo at OUT_RATE. Nothing is clipped here."""
    args = ["-i", src]
    if af:
        args += ["-af", af]
    args += RAW_FMT + [dst]
    await run_ffmpeg(args)


def join_raw(paths: list[str], out_path: str):
    """Raw float32 files with the same format concatenate byte for byte."""
    with open(out_path, "wb") as out:
        for p in paths:
            with open(p, "rb") as f:
                while chunk := f.read(1 << 20):
                    out.write(chunk)


async def build_bait(input_path: str, output_path: str, settings: BaitSettings) -> str | None:
    """
    Process the main track as float32. If settings.wrap is set, join the optional
    pre clip, the intro, the main track, and the outro. If match_output is set,
    correct the final file's loudness to the input's. Then encode once to OGG.
    Returns a loudness note when matching, otherwise None.
    """
    in_rate = await probe_sample_rate(input_path)

    with tempfile.TemporaryDirectory() as tmp:
        main_raw = os.path.join(tmp, "main.raw")
        joined_raw = os.path.join(tmp, "joined.raw")

        await decode_to_raw(input_path, main_raw, build_main_filter(in_rate, settings))

        if settings.wrap:
            parts = []
            if os.path.exists(PRE_PATH):
                pre_raw = os.path.join(tmp, "pre.raw")
                await decode_to_raw(PRE_PATH, pre_raw)
                parts.append(pre_raw)
            else:
                log.warning("assets/pre.ogg not found, skipping pre clip")

            intro_raw = os.path.join(tmp, "intro.raw")
            outro_raw = os.path.join(tmp, "outro.raw")
            await decode_to_raw(INTRO_PATH, intro_raw)
            await decode_to_raw(OUTRO_PATH, outro_raw)

            join_raw(parts + [intro_raw, main_raw, outro_raw], joined_raw)
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


async def process_and_reply(
    interaction: discord.Interaction,
    file: discord.Attachment,
    settings: BaitSettings,
    label: str,
):
    await interaction.response.defer(thinking=True)

    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, file.filename)
        output_path = os.path.join(tmp, "output.ogg")

        await file.save(input_path)

        try:
            note = await build_bait(input_path, output_path, settings)
        except Exception as e:
            log.exception("bait build failed")
            msg = f"Something went wrong: `{e}`"
            if len(msg) > 1900:
                msg = msg[:1900] + "... (truncated, check logs)"
            await interaction.followup.send(msg)
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
        await interaction.followup.send(
            content=text,
            file=discord.File(output_path, filename="output.ogg"),
        )


@tree.command(name="bait", description="Wrap an audio file in intro/outro clips at a new speed, keeping its volume")
@app_commands.describe(
    file="The audio file to process",
    speed="Playback speed (changes tempo and pitch together)",
)
@app_commands.choices(speed=[
    app_commands.Choice(name="1.4x", value="1.4"),
    app_commands.Choice(name="1.7x", value="1.7"),
])
@anywhere
async def bait(
    interaction: discord.Interaction,
    file: discord.Attachment,
    speed: app_commands.Choice[str],
):
    settings = BaitSettings(speed=float(speed.value), wrap=True, match_output=True)
    await process_and_reply(interaction, file, settings, f"{speed.name} speed, volume matched to input")


@tree.command(name="pitch", description="Shift pitch in semitones, keeping tempo the same")
@app_commands.describe(
    file="The audio file to process",
    semitones="Pitch shift in semitones (-12 to 12)",
)
@anywhere
async def pitch(
    interaction: discord.Interaction,
    file: discord.Attachment,
    semitones: app_commands.Range[float, -12.0, 12.0],
):
    settings = BaitSettings(pitch=semitones)
    await process_and_reply(interaction, file, settings, f"pitch {semitones:+g} st")


@tree.command(name="tempo", description="Change tempo, keeping pitch the same")
@app_commands.describe(
    file="The audio file to process",
    factor="Tempo multiplier (0.5 to 2.0)",
)
@anywhere
async def tempo(
    interaction: discord.Interaction,
    file: discord.Attachment,
    factor: app_commands.Range[float, 0.5, 2.0],
):
    settings = BaitSettings(tempo=factor)
    await process_and_reply(interaction, file, settings, f"tempo {factor:g}x")


@tree.command(name="reverb", description="Add a reverb preset to the main track")
@app_commands.describe(
    file="The audio file to process",
    preset="Reverb preset",
)
@app_commands.choices(preset=[
    app_commands.Choice(name="room", value="room"),
    app_commands.Choice(name="hall", value="hall"),
    app_commands.Choice(name="cave", value="cave"),
])
@anywhere
async def reverb(
    interaction: discord.Interaction,
    file: discord.Attachment,
    preset: app_commands.Choice[str],
):
    settings = BaitSettings(reverb=preset.value)
    await process_and_reply(interaction, file, settings, f"{preset.name} reverb")


@tree.command(name="volume", description="Change volume of the track (limited, no clipping)")
@app_commands.describe(
    file="The audio file to process",
    level="Volume multiplier (0.1 to 3.0)",
)
@anywhere
async def volume(
    interaction: discord.Interaction,
    file: discord.Attachment,
    level: app_commands.Range[float, 0.1, 3.0],
):
    settings = BaitSettings(volume=level)
    await process_and_reply(interaction, file, settings, f"volume {level:g}x")


@tree.command(name="loud", description="Max loudness with clipping, plus bass boost or cut")
@app_commands.describe(
    file="The audio file to process",
    volume="Volume multiplier. 1 = no change, 3162 = about +70 dB. Overs clip.",
    bass="Bass in dB at 100 Hz: positive boosts, negative cuts (-20 to 20). Default 12.",
)
@anywhere
async def loud(
    interaction: discord.Interaction,
    file: discord.Attachment,
    volume: app_commands.Range[float, 1.0, 5000.0] = LOUD_VOLUME,
    bass: app_commands.Range[float, -20.0, 20.0] = LOUD_BASS_DB,
):
    gain_db = 20 * math.log10(volume)
    settings = BaitSettings(gain_db=gain_db, bass_db=bass)
    await process_and_reply(
        interaction, file, settings,
        f"volume x{volume:g} (+{gain_db:.1f} dB), bass {bass:+g} dB",
    )


def register_fx_command(name: str, description: str, chain: str):
    """Create one slash command per effect. Each closure captures its own chain."""

    async def fx_cmd(interaction: discord.Interaction, file: discord.Attachment):
        settings = BaitSettings(fx=chain)
        await process_and_reply(interaction, file, settings, name)

    fx_cmd = anywhere(fx_cmd)
    fx_cmd = app_commands.describe(file="The audio file to process")(fx_cmd)
    tree.command(name=name, description=description)(fx_cmd)


for _name, _desc, _chain in FX_COMMANDS:
    register_fx_command(_name, _desc, _chain)


@bot.event
async def on_ready():
    await tree.sync()
    log.info(f"Logged in as {bot.user}")


if __name__ == "__main__":
    bot.run(TOKEN)
