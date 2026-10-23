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
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
INTRO_PATH = os.path.join(ASSETS_DIR, "intro.mp3")
OUTRO_PATH = os.path.join(ASSETS_DIR, "outro.mp3")

MAX_DISCORD_UPLOAD = 25 * 1024 * 1024  # 25 MB, adjust for your server's boost level

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


async def run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='ignore')[-500:]}")


async def build_bait(input_path: str, output_path: str, speed: float):
    """
    Concatenate intro + speed-changed main track + outro, export as OGG (Vorbis).
    Uses ffmpeg's concat demuxer so every clip is first normalized to the same
    format (sample rate / channel layout) before gluing them together.
    """
    with tempfile.TemporaryDirectory() as tmp:
        norm_intro = os.path.join(tmp, "intro.ogg")
        norm_main = os.path.join(tmp, "main.ogg")
        norm_outro = os.path.join(tmp, "outro.ogg")
        concat_list = os.path.join(tmp, "list.txt")

        # Normalize intro to 44.1kHz stereo
        await run_ffmpeg([
            "-i", INTRO_PATH,
            "-ar", "44100", "-ac", "2",
            "-c:a", "libvorbis", "-q:a", "6",
            norm_intro,
        ])

        # Apply speed change to the main track (pitch rises with speed, same as
        # a turntable/tape speed-up) and normalize format
        new_rate = int(44100 * speed)
        await run_ffmpeg([
            "-i", input_path,
            "-af", f"asetrate={new_rate},aresample=44100",
            "-ac", "2",
            "-c:a", "libvorbis", "-q:a", "6",
            norm_main,
        ])

        # Normalize outro
        await run_ffmpeg([
            "-i", OUTRO_PATH,
            "-ar", "44100", "-ac", "2",
            "-c:a", "libvorbis", "-q:a", "6",
            norm_outro,
        ])

        with open(concat_list, "w") as f:
            for p in (norm_intro, norm_main, norm_outro):
                f.write(f"file '{p}'\n")

        await run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c:a", "libvorbis", "-q:a", "6",
            output_path,
        ])


@tree.command(name="bait", description="Add intro/outro bait clips and a speed change to an audio file")
@app_commands.describe(
    file="The audio file to process",
    speed="Playback speed multiplier",
)
@app_commands.choices(speed=[
    app_commands.Choice(name="1.4x", value="1.4"),
    app_commands.Choice(name="1.7x", value="1.7"),
])
async def bait(interaction: discord.Interaction, file: discord.Attachment, speed: app_commands.Choice[str]):
    await interaction.response.defer(thinking=True)

    speed_val = float(speed.value)

    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, file.filename)
        output_path = os.path.join(tmp, "output.ogg")

        await file.save(input_path)

        try:
            await build_bait(input_path, output_path, speed_val)
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
            content=f"Done — {speed.name} speed.",
            file=discord.File(output_path, filename="bait.ogg"),
        )


@bot.event
async def on_ready():
    await tree.sync()
    log.info(f"Logged in as {bot.user}")


if __name__ == "__main__":
    bot.run(TOKEN)
