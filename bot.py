import os
import asyncio
import tempfile
import logging

import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bait-bot")

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ.get("GUILD_ID", "1562831867844108329"))  # server that gets commands instantly

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
PRE_PATH = os.path.join(ASSETS_DIR, "pre.ogg")      # plays before the intro, optional
INTRO_PATH = os.path.join(ASSETS_DIR, "intro.mp3")  # intro clip
OUTRO_PATH = os.path.join(ASSETS_DIR, "outro.mp3")  # outro clip

MAX_DISCORD_UPLOAD = 25 * 1024 * 1024  # 25 MB, adjust for your server's boost level
OUT_RATE = 44100
DEFAULT_IN_RATE = 44100

VOL_DB_MIN = -100.0
VOL_DB_MAX = 100.0
BASS_MIN = -100.0
BASS_MAX = 100.0
SPEED_MIN = 1.0
SPEED_MAX = 2.0
BAIT_SPEED_DEFAULT = 1.4  # the bait speed; /vol adds the bait wrap and this speed by default

# Intermediate format. 32-bit float keeps samples above full scale until the final encode.
RAW_FMT = ["-f", "f32le", "-ar", str(OUT_RATE), "-ac", "2"]

# Final step before Opus: resample to 48 kHz, stereo, then convert to 16-bit.
# The 16-bit conversion keeps the Opus encoder from producing a near-empty file on loud audio.
FINAL_AF = "aresample=48000,aformat=channel_layouts=stereo,aformat=sample_fmts=s16"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix=".", intents=intents)
tree = bot.tree


def anywhere(fn):
    """Let a command be installed to a user, so it works in servers, DMs, and group DMs."""
    fn = app_commands.allowed_installs(guilds=True, users=True)(fn)
    return app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(fn)


async def run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = [l.strip() for l in stderr.decode(errors="ignore").splitlines() if l.strip()][-3:]
        raise RuntimeError(f"ffmpeg failed: {' | '.join(tail)[-400:]}")


async def probe_sample_rate(path: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate", "-of", "default=nw=1:nk=1", path,
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
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return 0.0


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


def track_filters(db: float, bass: float) -> list[str]:
    """Volume and bass change on the track, before the speed change and the bait wrap."""
    filters = []
    if bass != 0.0:
        filters.append(f"bass=g={bass}")
    if db != 0.0:
        filters.append(f"volume={db}dB")
    return filters


def speed_filters(in_rate: int, speed: float) -> list[str]:
    """Speed change (tempo and pitch together). Goes through 192 kHz so loud peaks don't turn into overs."""
    if speed == 1.0:
        return []
    return [f"asetrate={round(in_rate * speed)}", "aresample=192000"]


async def build_vol_bait(inp: str, out: str, db: float, bass: float, speed: float):
    """Volume and bass on the track, then speed, then the pre/intro/track/outro join, then Opus."""
    in_rate = await probe_sample_rate(inp)

    with tempfile.TemporaryDirectory() as tmp:
        track_raw = os.path.join(tmp, "track.raw")
        joined_raw = os.path.join(tmp, "joined.raw")

        af = ",".join(track_filters(db, bass) + speed_filters(in_rate, speed))
        await decode_to_raw(inp, track_raw, af or None)

        for required in (INTRO_PATH, OUTRO_PATH):
            if not os.path.exists(required):
                raise RuntimeError(f"missing {os.path.relpath(required, ASSETS_DIR)} in assets/")

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

        join_raw(parts + [intro_raw, track_raw, outro_raw], joined_raw)

        await run_ffmpeg([
            "-f", "f32le", "-ar", str(OUT_RATE), "-ac", "2", "-i", joined_raw,
            "-af", FINAL_AF,
            "-c:a", "libopus", "-b:a", "192k", out,
        ])


@tree.command(name="vol", description="Volume, bass, speed (1x-2x), and bait wrap in one command")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    db="Volume change in dB (-100 to 100). 10 = +10 dB. 0 = no change.",
    bass="Bass change in dB (-100 to 100). 0 = no change.",
    speed="Bait speed from 1x to 2x. Default 1.4. Set 1 for normal speed (bait wrap still added).",
)
async def vol_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    db: app_commands.Range[float, VOL_DB_MIN, VOL_DB_MAX] = 0.0,
    bass: app_commands.Range[float, BASS_MIN, BASS_MAX] = 0.0,
    speed: app_commands.Range[float, SPEED_MIN, SPEED_MAX] = BAIT_SPEED_DEFAULT,
):
    await interaction.response.defer(thinking=True)

    with tempfile.TemporaryDirectory() as tmp:
        ext = os.path.splitext(file.filename)[1] or ".mp3"
        inp = os.path.join(tmp, f"vol_in{ext}")
        out = os.path.join(tmp, "vol_out.ogg")
        await file.save(inp)

        try:
            await build_vol_bait(inp, out, db, bass, speed)
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

        out_len = await probe_duration(out)
        label = f"Bait added (pre/intro/outro) | Volume {db:+g} dB | Bass {bass:+g} dB | Speed {speed:g}x | length {out_len:.1f}s"
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
    log.info(f"Logged in as {bot.user}, synced /vol to guild {GUILD_ID} and globally")


if __name__ == "__main__":
    bot.run(TOKEN)
