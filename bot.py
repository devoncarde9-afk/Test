import os
import wave
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
INTRO_PATH = os.path.join(ASSETS_DIR, "intro.mp3")
OUTRO_PATH = os.path.join(ASSETS_DIR, "outro.mp3")

MAX_DISCORD_UPLOAD = 25 * 1024 * 1024  # 25 MB, adjust for your server's boost level
OUT_RATE = 44100
DEFAULT_IN_RATE = 44100

# aecho: in_gain:out_gain:delays_ms:decays
REVERB_PRESETS = {
    "room": "0.8:0.88:50|80:0.4|0.3",
    "hall": "0.8:0.88:100|150|200:0.4|0.3|0.2",
    "cave": "0.6:0.9:200|300|400:0.5|0.4|0.3",
}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


@dataclass(frozen=True)
class BaitSettings:
    speed: float = 1.0    # tempo + pitch together (turntable style)
    pitch: float = 0.0    # semitones, tempo preserved
    tempo: float = 1.0    # tempo multiplier, pitch preserved
    reverb: str = "off"   # key of REVERB_PRESETS, or "off"
    volume: float = 1.0   # multiplier on the main track


async def run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='ignore')[-500:]}")


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

    if s.reverb in REVERB_PRESETS:
        filters.append(f"aecho={REVERB_PRESETS[s.reverb]}")

    if abs(s.volume - 1.0) > 1e-6:
        filters.append(f"volume={s.volume:.4f}:precision=float")
        if s.volume > 1.0:
            filters.append("alimiter=limit=0.95")

    return ",".join(filters)


async def decode_to_wav(src: str, dst: str, af: str | None = None):
    args = ["-i", src]
    if af:
        args += ["-af", af]
    args += ["-ar", str(OUT_RATE), "-ac", "2", "-c:a", "pcm_s16le", dst]
    await run_ffmpeg(args)


def join_wavs(paths: list[str], out_path: str):
    with wave.open(out_path, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(OUT_RATE)
        for p in paths:
            with wave.open(p, "rb") as w:
                out.writeframes(w.readframes(w.getnframes()))


async def build_bait(input_path: str, output_path: str, settings: BaitSettings):
    """
    Decode intro, processed main, and outro to WAV, join them in Python,
    then encode once to OGG (Vorbis). No concat filter or demuxer involved.
    """
    in_rate = await probe_sample_rate(input_path)

    with tempfile.TemporaryDirectory() as tmp:
        intro_wav = os.path.join(tmp, "intro.wav")
        main_wav = os.path.join(tmp, "main.wav")
        outro_wav = os.path.join(tmp, "outro.wav")
        joined_wav = os.path.join(tmp, "joined.wav")

        await decode_to_wav(INTRO_PATH, intro_wav)
        await decode_to_wav(input_path, main_wav, build_main_filter(in_rate, settings))
        await decode_to_wav(OUTRO_PATH, outro_wav)

        join_wavs([intro_wav, main_wav, outro_wav], joined_wav)

        await run_ffmpeg([
            "-i", joined_wav,
            "-c:a", "libvorbis", "-q:a", "6",
            output_path,
        ])


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
            await build_bait(input_path, output_path, settings)
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

        await interaction.followup.send(
            content=f"Done — {label}.",
            file=discord.File(output_path, filename="bait.ogg"),
        )


@tree.command(name="bait", description="Wrap an audio file in intro/outro clips at a new speed")
@app_commands.describe(
    file="The audio file to process",
    speed="Playback speed (changes tempo and pitch together)",
)
@app_commands.choices(speed=[
    app_commands.Choice(name="1.4x", value="1.4"),
    app_commands.Choice(name="1.7x", value="1.7"),
])
async def bait(
    interaction: discord.Interaction,
    file: discord.Attachment,
    speed: app_commands.Choice[str],
):
    settings = BaitSettings(speed=float(speed.value))
    await process_and_reply(interaction, file, settings, f"{speed.name} speed")


@tree.command(name="pitch", description="Shift pitch in semitones, keeping tempo the same")
@app_commands.describe(
    file="The audio file to process",
    semitones="Pitch shift in semitones (-12 to 12)",
)
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
async def reverb(
    interaction: discord.Interaction,
    file: discord.Attachment,
    preset: app_commands.Choice[str],
):
    settings = BaitSettings(reverb=preset.value)
    await process_and_reply(interaction, file, settings, f"{preset.name} reverb")


@tree.command(name="volume", description="Change volume of the main track")
@app_commands.describe(
    file="The audio file to process",
    level="Volume multiplier (0.1 to 3.0)",
)
async def volume(
    interaction: discord.Interaction,
    file: discord.Attachment,
    level: app_commands.Range[float, 0.1, 3.0],
):
    settings = BaitSettings(volume=level)
    await process_and_reply(interaction, file, settings, f"volume {level:g}x")


@bot.event
async def on_ready():
    await tree.sync()
    log.info(f"Logged in as {bot.user}")


if __name__ == "__main__":
    bot.run(TOKEN)
