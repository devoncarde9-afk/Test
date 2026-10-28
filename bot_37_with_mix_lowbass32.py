import os
import asyncio
import tempfile
import logging
import json

import aiohttp
from urllib.parse import urlparse

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
OP_START_PATH = os.path.join(ASSETS_DIR, "que_bait.ogg")  # /op: plays first
OP_END_PATH = os.path.join(ASSETS_DIR, "bait_eendcreo_creo_creo_creo_creo_creo_creo_creo_creo_creo.mp3")  # /op: plays last

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


async def build_bait_track(inp: str, out: str, db: float, bass: float, speed: float,
                           start_clip: str, end_clip: str, pre_clip: str | None,
                           user_intros: list[str] | None = None):
    """Volume and bass on the track, then speed, then start/track/end join, then Opus."""
    in_rate = await probe_sample_rate(inp)

    for required in (start_clip, end_clip):
        if not os.path.exists(required):
            raise RuntimeError(f"missing {os.path.relpath(required, ASSETS_DIR)} in assets/")

    with tempfile.TemporaryDirectory() as tmp:
        track_raw = os.path.join(tmp, "track.raw")
        joined_raw = os.path.join(tmp, "joined.raw")

        af = ",".join(track_filters(db, bass) + speed_filters(in_rate, speed))
        await decode_to_raw(inp, track_raw, af or None)

        parts = []
        if pre_clip and os.path.exists(pre_clip):
            pre_raw = os.path.join(tmp, "pre.raw")
            await decode_to_raw(pre_clip, pre_raw)
            parts.append(pre_raw)

        start_raw = os.path.join(tmp, "start.raw")
        end_raw = os.path.join(tmp, "end.raw")
        await decode_to_raw(start_clip, start_raw)
        await decode_to_raw(end_clip, end_raw)

        # Order: pre, bait intro, your intros (in the order given), track, end bait.
        # Your intros get the speed change only. Volume and bass are never applied to them.
        middle = [start_raw]
        for idx, intro_path in enumerate(user_intros or []):
            intro_in_rate = await probe_sample_rate(intro_path)
            intro_af = ",".join(speed_filters(intro_in_rate, speed))
            user_raw = os.path.join(tmp, f"user_intro_{idx}.raw")
            await decode_to_raw(intro_path, user_raw, intro_af or None)
            middle.append(user_raw)

        join_raw(parts + middle + [track_raw, end_raw], joined_raw)

        await run_ffmpeg([
            "-f", "f32le", "-ar", str(OUT_RATE), "-ac", "2", "-i", joined_raw,
            "-af", FINAL_AF,
            "-c:a", "libopus", "-b:a", "192k", out,
        ])


async def run_bait_command(interaction, file, db, bass, speed, start_clip, end_clip, pre_clip, title, intros=(), start_att=None, end_att=None):
    """Shared body for /vol and /opbait: download, process, reply."""
    await interaction.response.defer(thinking=True)

    with tempfile.TemporaryDirectory() as tmp:
        ext = os.path.splitext(file.filename)[1] or ".mp3"
        inp = os.path.join(tmp, f"in{ext}")
        out = os.path.join(tmp, "out.ogg")
        await file.save(inp)

        if start_att is not None:
            start_clip = os.path.join(tmp, f"start_bait{os.path.splitext(start_att.filename)[1] or '.mp3'}")
            await start_att.save(start_clip)
        if end_att is not None:
            end_clip = os.path.join(tmp, f"end_bait{os.path.splitext(end_att.filename)[1] or '.mp3'}")
            await end_att.save(end_clip)

        user_intros = []
        for idx, att in enumerate(a for a in intros if a is not None):
            path = os.path.join(tmp, f"intro_{idx}{os.path.splitext(att.filename)[1] or '.mp3'}")
            await att.save(path)
            user_intros.append(path)

        try:
            await build_bait_track(inp, out, db, bass, speed, start_clip, end_clip, pre_clip, user_intros)
        except Exception as e:
            log.exception(f"{title} failed")
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
        label = (f"{title} added | Volume {db:+g} dB | Bass {bass:+g} dB | "
                 f"Speed {speed:g}x | length {out_len:.1f}s")
        await interaction.followup.send(content=label, file=discord.File(out, filename="out.ogg"))


@tree.command(name="vol", description="Volume, bass, speed (1x-2x), and bait wrap in one command")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    db="Volume change in dB (-100 to 100). 10 = +10 dB. 0 = no change.",
    bass="Bass change in dB (-100 to 100). 0 = no change.",
    speed="Bait speed from 1x to 2x. Default 1.4. Set 1 for normal speed (bait wrap still added).",
    intro="Optional: your intro. Goes after the bait intro. Gets the speed change, volume unchanged.",
    intro2="Optional: second intro (friend). Goes after intro 1. Gets the speed change, volume unchanged.",
)
async def vol_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    db: app_commands.Range[float, VOL_DB_MIN, VOL_DB_MAX] = 0.0,
    bass: app_commands.Range[float, BASS_MIN, BASS_MAX] = 0.0,
    speed: app_commands.Range[float, SPEED_MIN, SPEED_MAX] = BAIT_SPEED_DEFAULT,
    intro: discord.Attachment | None = None,
    intro2: discord.Attachment | None = None,
):
    await run_bait_command(
        interaction, file, db, bass, speed,
        start_clip=INTRO_PATH, end_clip=OUTRO_PATH, pre_clip=PRE_PATH,
        title="Bait (pre/intro/outro)", intros=(intro, intro2),
    )


@tree.command(name="op", description="Same as /vol, but with the OP bait clips")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    db="Volume change in dB (-100 to 100). 10 = +10 dB. 0 = no change.",
    bass="Bass change in dB (-100 to 100). 0 = no change.",
    speed="Bait speed from 1x to 2x. Default 1.4. Set 1 for normal speed (bait wrap still added).",
    intro="Optional: your intro. Goes after the bait intro. Gets the speed change, volume unchanged.",
    intro2="Optional: second intro (friend). Goes after intro 1. Gets the speed change, volume unchanged.",
)
async def op_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    db: app_commands.Range[float, VOL_DB_MIN, VOL_DB_MAX] = 0.0,
    bass: app_commands.Range[float, BASS_MIN, BASS_MAX] = 0.0,
    speed: app_commands.Range[float, SPEED_MIN, SPEED_MAX] = BAIT_SPEED_DEFAULT,
    intro: discord.Attachment | None = None,
    intro2: discord.Attachment | None = None,
):
    await run_bait_command(
        interaction, file, db, bass, speed,
        start_clip=OP_START_PATH, end_clip=OP_END_PATH, pre_clip=None,
        title="OP bait", intros=(intro, intro2),
    )


@tree.command(name="custom", description="Your own intro bait, your intro, your song, and your end bait")
@anywhere
@app_commands.describe(
    file="The song to process",
    start_bait="Your bait clip that plays first",
    end_bait="Your bait clip that plays last",
    intro="Optional: your own intro, after the start bait. Gets speed only.",
    db="Volume change in dB (-100 to 100). 0 = no change.",
    bass="Bass change in dB (-100 to 100). 0 = no change.",
    speed="Speed from 1x to 2x. Default 1.4. Set 1 for normal speed.",
)
async def custom_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    start_bait: discord.Attachment,
    end_bait: discord.Attachment,
    intro: discord.Attachment | None = None,
    db: app_commands.Range[float, VOL_DB_MIN, VOL_DB_MAX] = 0.0,
    bass: app_commands.Range[float, BASS_MIN, BASS_MAX] = 0.0,
    speed: app_commands.Range[float, SPEED_MIN, SPEED_MAX] = BAIT_SPEED_DEFAULT,
):
    await run_bait_command(
        interaction, file, db, bass, speed,
        start_clip=None, end_clip=None, pre_clip=None,
        title="Custom bait",
        intros=(intro,),
        start_att=start_bait, end_att=end_bait,
    )


# /download: accepts direct audio-file URLs from any website.
AUDIO_EXTS = {
    ".mp3", ".ogg", ".opus", ".wav", ".flac", ".m4a", ".aac", ".wma", ".aiff", ".aif", ".webm",
}
DOWNLOAD_TIMEOUT_SECONDS = 120


async def run_capture(*args: str) -> tuple[int, str, str]:
    """Run a subprocess and return its exit code, stdout, and stderr."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode,
        stdout.decode(errors="ignore"),
        stderr.decode(errors="ignore"),
    )


@tree.command(name="download", description="Download a direct audio file link and send it back to you")
@anywhere
@app_commands.describe(url="Direct link to an audio file you have the rights to use")
async def download_cmd(interaction: discord.Interaction, url: str):
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        await interaction.response.send_message(
            "That is not a valid http or https link.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)

    name = os.path.basename(parsed.path) or "download"
    # Avoid unsafe path characters while preserving a useful filename.
    name = os.path.basename(name.replace("\\", "_"))
    ext = os.path.splitext(name)[1].lower()
    if ext not in AUDIO_EXTS:
        name = f"{name}.mp3" if not ext else name
        ext = os.path.splitext(name)[1].lower()

    try:
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"Download failed: HTTP {resp.status}.")
                    return
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if not (ctype.startswith("audio/") or ext in AUDIO_EXTS):
                    await interaction.followup.send("That link does not appear to point to an audio file.")
                    return
                if resp.content_length is not None and resp.content_length > MAX_DISCORD_UPLOAD:
                    await interaction.followup.send(
                        f"File is {resp.content_length / (1024 * 1024):.1f} MB, over the upload limit."
                    )
                    return

                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_DISCORD_UPLOAD:
                        await interaction.followup.send("File is over the Discord upload limit.")
                        return
    except Exception as e:
        log.exception("download failed")
        await interaction.followup.send(f"Download failed: `{str(e)[:300]}`")
        return

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, name)
        with open(path, "wb") as f:
            f.write(data)

        # Validate the source, then transcode it to a genuine MP3. Some URLs
        # end in .mp3 while serving a different container/codec; merely naming
        # those bytes "something.mp3" can produce an unplayable Discord preview.
        code, out, _ = await run_capture(
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1", path,
        )
        if code != 0 or "audio" not in out:
            await interaction.followup.send("That file has no readable audio stream.")
            return

        mp3_name = os.path.splitext(name)[0] + ".mp3"
        mp3_path = os.path.join(tmp, "converted.mp3")
        try:
            await run_ffmpeg([
                "-i", path,
                "-vn",
                "-map", "0:a:0",
                "-c:a", "libmp3lame",
                "-q:a", "2",
                "-ar", "44100",
                "-ac", "2",
                mp3_path,
            ])
        except Exception as e:
            log.exception("audio conversion failed")
            await interaction.followup.send(
                f"Could not convert that audio to a playable MP3: `{str(e)[:500]}`"
            )
            return

        converted_size = os.path.getsize(mp3_path)
        if converted_size > MAX_DISCORD_UPLOAD:
            await interaction.followup.send(
                f"Converted MP3 is {converted_size / (1024 * 1024):.1f} MB, over the Discord upload limit."
            )
            return

        # Verify the actual output is an MP3 audio stream before sending it.
        code, out, _ = await run_capture(
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,codec_type", "-of", "default=nw=1:nk=1", mp3_path,
        )
        if code != 0 or "audio" not in out or "mp3" not in out:
            await interaction.followup.send("The converted MP3 failed validation. Try another direct audio link.")
            return

        await interaction.followup.send(
            content=f"Downloaded and converted `{mp3_name}` ({converted_size / (1024 * 1024):.2f} MB).",
            file=discord.File(mp3_path, filename=mp3_name),
        )


@tree.command(name="copyright", description="Check an audio file for copyright-related metadata tags")
@anywhere
@app_commands.describe(file="Audio file to inspect for copyright or rights metadata")
async def copyright_cmd(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(thinking=True, ephemeral=True)
    with tempfile.TemporaryDirectory() as tmp:
        ext = os.path.splitext(file.filename)[1] or ".audio"
        path = os.path.join(tmp, f"input{ext}")
        await file.save(path)
        code, stdout, stderr = await run_capture(
            "ffprobe", "-v", "error", "-show_format", "-show_streams",
            "-of", "json", path,
        )
        if code != 0:
            await interaction.followup.send(
                f"Couldn't inspect this file with ffprobe: `{stderr[:500]}`", ephemeral=True
            )
            return
        try:
            info = json.loads(stdout)
        except json.JSONDecodeError:
            await interaction.followup.send("Couldn't read the file's metadata.", ephemeral=True)
            return

        tags = {}
        for section in [info.get("format", {})] + info.get("streams", []):
            for key, value in section.get("tags", {}).items():
                tags[str(key)] = str(value)
        needles = ("copyright", "copy_right", "rights", "license", "licence", "publisher")
        matches = [(k, v) for k, v in tags.items() if any(n in k.lower() for n in needles)]
        if matches:
            lines = [f"`{k}`: {v[:250]}" for k, v in matches[:12]]
            message = "**Copyright/rights-related metadata found:**\n" + "\n".join(lines)
        else:
            message = "**No explicit copyright/rights metadata tags found.** This does not mean the audio is copyright-free; many copyrighted files have no such tags."
        await interaction.followup.send(message[:1900], ephemeral=True)


@tree.command(name="boost100", description="Create a heavily boosted, peak-limited MP3 from an audio file")
@anywhere
@app_commands.describe(file="Audio file to boost; extreme gain can cause severe distortion")
async def boost100_cmd(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(thinking=True)
    with tempfile.TemporaryDirectory() as tmp:
        ext = os.path.splitext(file.filename)[1] or ".audio"
        src = os.path.join(tmp, f"input{ext}")
        out = os.path.join(tmp, "boost100.mp3")
        await file.save(src)

        code, stdout, stderr = await run_capture(
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1", src,
        )
        if code != 0 or "audio" not in stdout:
            await interaction.followup.send("That attachment doesn't contain a readable audio stream.")
            return

        try:
            # Apply +100 dB digital gain, then limit peaks below full scale so the
            # encoded MP3 doesn't wrap or become invalid. This cannot create a
            # physically calibrated 100 dB SPL file; the limiter will heavily
            # distort most normal audio at this gain.
            await run_ffmpeg([
                "-i", src, "-vn", "-map", "0:a:0",
                "-af", "volume=100dB,alimiter=limit=0.95:level=false:attack=5:release=50",
                "-c:a", "libmp3lame", "-q:a", "2", "-ar", "44100", "-ac", "2", out,
            ])
        except Exception as e:
            log.exception("100 dB boost failed")
            await interaction.followup.send(f"Boost failed: `{str(e)[:500]}`")
            return

        size = os.path.getsize(out)
        if size > MAX_DISCORD_UPLOAD:
            await interaction.followup.send(
                f"Boosted MP3 is {size / (1024 * 1024):.1f} MB, over the upload limit. Try a shorter file."
            )
            return
        await interaction.followup.send(
            content=(f"Created `{os.path.basename(os.path.splitext(file.filename)[0])}_boost100.mp3` "
                     f"({size / (1024 * 1024):.2f} MB). **Warning:** +100 dB digital gain is not 100 dB of real-world sound pressure and will usually cause extreme distortion; keep playback volume low."),
            file=discord.File(out, filename=f"{os.path.splitext(os.path.basename(file.filename))[0]}_boost100.mp3"),
        )


@tree.command(name="mix", description="Mix up to three audio files together into one MP3")
@anywhere
@app_commands.describe(
    file1="First audio file",
    file2="Second audio file (optional)",
    file3="Third audio file (optional)",
)
async def mix_cmd(
    interaction: discord.Interaction,
    file1: discord.Attachment,
    file2: discord.Attachment | None = None,
    file3: discord.Attachment | None = None,
):
    files = [f for f in (file1, file2, file3) if f is not None]
    await interaction.response.defer(thinking=True)

    with tempfile.TemporaryDirectory() as tmp:
        inputs = []
        for idx, attachment in enumerate(files):
            ext = os.path.splitext(attachment.filename)[1] or ".audio"
            path = os.path.join(tmp, f"mix_input_{idx}{ext}")
            await attachment.save(path)
            code, stdout, _ = await run_capture(
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1", path,
            )
            if code != 0 or "audio" not in stdout:
                await interaction.followup.send(f"`{attachment.filename}` doesn't contain a readable audio stream.")
                return
            inputs.append(path)

        out = os.path.join(tmp, "mixed.mp3")
        args = []
        for path in inputs:
            args += ["-i", path]
        labels = "".join(f"[{i}:a:0]" for i in range(len(inputs)))
        filter_complex = (
            f"{labels}amix=inputs={len(inputs)}:duration=longest:dropout_transition=2:normalize=1,"
            "aresample=44100,aformat=channel_layouts=stereo,alimiter=limit=0.95:level=false[out]"
        )
        try:
            await run_ffmpeg(args + [
                "-filter_complex", filter_complex, "-map", "[out]", "-vn",
                "-c:a", "libmp3lame", "-q:a", "2", out,
            ])
        except Exception as e:
            log.exception("audio mix failed")
            await interaction.followup.send(f"Mix failed: `{str(e)[:500]}`")
            return

        size = os.path.getsize(out)
        if size > MAX_DISCORD_UPLOAD:
            await interaction.followup.send(
                f"Mixed MP3 is {size / (1024 * 1024):.1f} MB, over the Discord upload limit. Try shorter files."
            )
            return
        await interaction.followup.send(
            content=f"Mixed {len(files)} audio file(s) into one MP3 ({size / (1024 * 1024):.2f} MB).",
            file=discord.File(out, filename="mixed.mp3"),
        )


@tree.command(name="lowbass32", description="Create a mono MP3 from 32 layered copies with strong low-bass boost")
@anywhere
@app_commands.describe(file="Audio file to process into a 32-layer mono mix")
async def lowbass32_cmd(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(thinking=True)

    with tempfile.TemporaryDirectory() as tmp:
        ext = os.path.splitext(file.filename)[1] or ".audio"
        src = os.path.join(tmp, f"input{ext}")
        out = os.path.join(tmp, "lowbass32_mono.mp3")
        await file.save(src)

        code, stdout, _ = await run_capture(
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1", src,
        )
        if code != 0 or "audio" not in stdout:
            await interaction.followup.send("That attachment doesn't contain a readable audio stream.")
            return

        # Split the source into 32 mono layers and blend them to one mono output.
        # Apply a strong low-frequency boost and +61 dB digital gain, then limit
        # peaks to prevent invalid over-range samples. This will be heavily distorted.
        split_labels = "".join(f"[layer{i}]" for i in range(32))
        input_labels = "".join(f"[layer{i}]" for i in range(32))
        filter_complex = (
            f"[0:a:0]aformat=channel_layouts=mono,asplit=32{split_labels};"
            f"{input_labels}amix=inputs=32:duration=longest:normalize=1,"
            "bass=g=20:f=90:w=0.5,volume=61dB,alimiter=limit=0.90:level=false[out]"
        )
        try:
            await run_ffmpeg([
                "-i", src, "-filter_complex", filter_complex, "-map", "[out]", "-vn",
                "-c:a", "libmp3lame", "-q:a", "2", "-ar", "44100", "-ac", "1", out,
            ])
        except Exception as e:
            log.exception("32-layer mono low-bass processing failed")
            await interaction.followup.send(f"32-track mono processing failed: `{str(e)[:500]}`")
            return

        size = os.path.getsize(out)
        if size > MAX_DISCORD_UPLOAD:
            await interaction.followup.send(
                f"Processed MP3 is {size / (1024 * 1024):.1f} MB, over the Discord upload limit. Try a shorter file."
            )
            return
        output_name = f"{os.path.splitext(os.path.basename(file.filename))[0]}_32track_mono_lowbass61.mp3"
        await interaction.followup.send(
            content=(f"Created `{output_name}` ({size / (1024 * 1024):.2f} MB): 32 layered mono copies, strong low-bass boost, and +61 dB digital gain. "
                     "The limiter prevents peaks above the digital ceiling, so this is not a guaranteed real-world 61 dB sound level and distortion may be severe. Keep playback volume low."),
            file=discord.File(out, filename=output_name),
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
