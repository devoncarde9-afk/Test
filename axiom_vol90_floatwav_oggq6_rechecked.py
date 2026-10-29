import os
import asyncio
import tempfile
import logging
import json
import glob
import shutil
import sys

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


def _audacity_shelf_filters(bass_db: float, treble_db: float) -> list[str]:
    """Return optional low/high shelf EQ filters for the 32-layer audio chain."""
    filters = []
    if bass_db:
        filters.append(f"lowshelf=g={bass_db}:f=250")
    if treble_db:
        filters.append(f"highshelf=g={treble_db}:f=4000")
    return filters


def build_thirty_two_tracks_filter(
    highpass_hz: int | None = None,
    bass_db: float = 0.0,
    treble_db: float = 0.0,
) -> str:
    """Build the requested 32-copy mono mix, gain stage, and stereo output."""
    filters: list[str] = []
    if highpass_hz is not None:
        filters.append(f"highpass=f={highpass_hz}")
    filters.extend(_audacity_shelf_filters(bass_db, treble_db))
    prefix = "".join(f"{audio_filter}," for audio_filter in filters)
    split_labels = "".join(f"[t{index}]" for index in range(32))
    return (
        f"[0:a]aformat=channel_layouts=mono,"
        f"aresample={OUT_RATE},{prefix}asplit=32{split_labels};"
        f"{split_labels}amix=inputs=32:normalize=0,volume=32[mono];"
        "[mono]pan=stereo|c0=c0|c1=c0[stereo]"
    )


def anywhere(fn):
    """Let a command be installed to a user, so it works in servers, DMs, and group DMs."""
    fn = app_commands.allowed_installs(guilds=True, users=True)(fn)
    return app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(fn)


async def run_ffmpeg(args: list[str]):
    # Suppress progress spam so failures report the actual FFmpeg diagnostic.
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y", *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        lines = [l.strip() for l in stderr.decode(errors="ignore").splitlines() if l.strip()]
        useful = [l for l in lines if any(word in l.lower() for word in
                  ("error", "invalid", "failed", "no such", "cannot", "could not", "unable", "not found"))]
        detail = " | ".join((useful or lines)[-4:])[-700:]
        raise RuntimeError(f"ffmpeg failed: {detail or f'exit code {proc.returncode}'}")


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
    """Apply the requested gain directly; high-gain mode deliberately avoids compression."""
    filters = []
    if bass != 0.0:
        filters.append(f"bass=g={bass}")
    if db != 0.0:
        # Do not replace the requested gain with a fixed +18 dB compressor chain.
        # Float intermediates preserve over-range samples until the OGG encode stage.
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
        track_source_wav = os.path.join(tmp, "track_source_float32.wav")
        track_processed_wav = os.path.join(tmp, "track_processed_float32.wav")
        track_raw = os.path.join(tmp, "track.raw")
        joined_raw = os.path.join(tmp, "joined.raw")

        # First decode the original into an actual 32-bit-float WAV.
        # Only then apply gain/EQ/speed processing to that WAV, rather than
        # applying the effects during the initial decode.
        await run_ffmpeg([
            "-i", inp, "-map", "0:a:0", "-vn",
            "-ar", str(OUT_RATE), "-ac", "2",
            "-c:a", "pcm_f32le", track_source_wav,
        ])

        af = ",".join(track_filters(db, bass) + speed_filters(in_rate, speed))
        process_args = ["-i", track_source_wav]
        if af:
            process_args += ["-af", af]
        process_args += [
            "-ar", str(OUT_RATE), "-ac", "2",
            "-c:a", "pcm_f32le", track_processed_wav,
        ]
        await run_ffmpeg(process_args)

        # Read the processed float WAV back as headerless float32 for bytewise
        # concatenation with the unchanged intro/outro clips.
        await run_ffmpeg([
            "-i", track_processed_wav, "-f", "f32le",
            "-ar", str(OUT_RATE), "-ac", "2", track_raw,
        ])

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

        if db > 18.0:
            # High-gain path: raw float32 -> 32-bit-float WAV -> process/resample -> OGG Vorbis q6.
            # The requested gain is applied directly in track_filters() while decoding the track
            # to float32. The WAV intermediate keeps float samples instead of forcing 16-bit PCM.
            # Note: extreme positive gain can still exceed playback full scale and sound distorted.
            float_wav = os.path.join(tmp, "joined_float32.wav")
            await run_ffmpeg([
                "-f", "f32le", "-ar", str(OUT_RATE), "-ac", "2", "-i", joined_raw,
                "-c:a", "pcm_f32le", "-ar", str(OUT_RATE), "-ac", "2", float_wav,
            ])
            await run_ffmpeg([
                "-i", float_wav,
                "-af", "aresample=48000,aformat=channel_layouts=stereo",
                "-c:a", "libvorbis", "-q:a", "6", out,
            ])
        else:
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
        gain_note = " (direct gain, OGG Vorbis q6; no compression)" if db > 18.0 else ""
        label = (f"{title} added | Volume {db:+g} dB{gain_note} | Bass {bass:+g} dB | "
                 f"Speed {speed:g}x | length {out_len:.1f}s")
        await interaction.followup.send(content=label, file=discord.File(out, filename="out.ogg"))


@tree.command(name="vol", description="Volume, bass, speed (1x-2x), and bait wrap in one command")
@anywhere
@app_commands.describe(
    file="The audio file to process",
    db="Volume setting in dB (-100 to +100). Above +18 dB uses a 32-bit-float WAV intermediate and OGG Vorbis quality 6; extreme gain may distort on playback.",
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
    db="Volume setting in dB (-100 to +100). Above +18 dB uses a 32-bit-float WAV intermediate and OGG Vorbis quality 6; extreme gain may distort on playback.",
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
    db="Volume setting in dB (-100 to +100). Above +18 dB uses a 32-bit-float WAV intermediate and OGG Vorbis quality 6; extreme gain may distort on playback.",
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


@tree.command(name="download", description="Download audio from a direct link or supported media page")
@anywhere
@app_commands.describe(url="Direct audio link or a media page supported by yt-dlp")
async def download_cmd(interaction: discord.Interaction, url: str):
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        await interaction.response.send_message("That is not a valid http or https link.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = None
            # If yt-dlp is installed, try it first. This supports many media pages
            # that do not expose a direct audio file URL. It is optional; direct
            # file URLs still work without it.
            ytdlp = shutil.which("yt-dlp")
            # Prefer the installed command, but fall back to the same Python environment's module.
            ytdlp_cmd = [ytdlp] if ytdlp else [sys.executable, "-m", "yt_dlp"]
            template = os.path.join(tmp, "source.%(ext)s")
            # YouTube's player clients and throttling behavior change regularly. Try a normal
            # extraction first, then a second client configuration. Each attempt uses the same
            # unique temporary directory and removes partial outputs so repeat calls stay clean.
            attempts = [
                [],
                ["--extractor-args", "youtube:player_client=android,web", "--force-ipv4"],
            ]
            code, stdout, stderr = (1, "", "yt-dlp did not run")
            candidates = []
            for attempt_number, extra_args in enumerate(attempts, start=1):
                for old_file in glob.glob(os.path.join(tmp, "source.*")):
                    try:
                        os.remove(old_file)
                    except OSError:
                        pass
                code, stdout, stderr = await run_capture(
                    *ytdlp_cmd, "--no-playlist", "--retries", "5",
                    "--fragment-retries", "5", "--extractor-retries", "5",
                    "--sleep-requests", "1", "--socket-timeout", "30",
                    "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
                    "--audio-quality", "0", "--no-part", "--no-progress",
                    *extra_args, "-o", template, url,
                )
                candidates = [f for f in glob.glob(os.path.join(tmp, "source.*")) if os.path.isfile(f)]
                if code == 0 and candidates:
                    break
                log.warning("yt-dlp attempt %s failed (exit %s): %s", attempt_number, code, stderr[-900:])

            if code == 0 and candidates:
                # Prefer the extracted MP3 if present; validate it below before use.
                source_path = next((f for f in candidates if f.lower().endswith(".mp3")), candidates[0])
            else:
                log.info("yt-dlp extraction failed after retries: %s", stderr[-900:])

            if source_path is None:
                name = os.path.basename(parsed.path) or "download"
                name = os.path.basename(name.replace("\\", "_"))
                ext = os.path.splitext(name)[1].lower()
                if ext not in AUDIO_EXTS:
                    # Keep extensionless signed URLs eligible; Content-Type and ffprobe
                    # below determine whether the response is actually audio.
                    name = (name if not ext else os.path.splitext(name)[0]) + ".audio"
                source_path = os.path.join(tmp, name)
                timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
                try:
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(url, allow_redirects=True) as resp:
                            if resp.status != 200:
                                await interaction.followup.send(f"Download failed: HTTP {resp.status}.")
                                return
                            ctype = (resp.headers.get("Content-Type") or "").lower()
                            if not (ctype.startswith("audio/") or ctype in ("application/octet-stream", "binary/octet-stream")):
                                await interaction.followup.send(
                                    f"The server returned `{ctype or 'an unknown content type'}`, not an audio response. "
                                    "Use a direct audio-file URL or install/update `yt-dlp` for media pages."
                                )
                                return
                            if resp.content_length is not None and resp.content_length > MAX_DISCORD_UPLOAD * 4:
                                await interaction.followup.send("The source file is too large to process safely.")
                                return
                            total = 0
                            with open(source_path, "wb") as f:
                                async for chunk in resp.content.iter_chunked(64 * 1024):
                                    total += len(chunk)
                                    if total > MAX_DISCORD_UPLOAD * 4:
                                        await interaction.followup.send("The source file is too large to process safely.")
                                        return
                                    f.write(chunk)
                except Exception as e:
                    log.exception("direct download failed")
                    await interaction.followup.send(f"Download failed: `{str(e)[:350]}`")
                    return

            # ffprobe confirms the downloaded content actually contains audio.
            code, probe_out, probe_err = await run_capture(
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1", source_path,
            )
            if code != 0 or "audio" not in probe_out.lower():
                await interaction.followup.send(
                    "The URL did not produce a readable audio stream. The `yt-dlp` attempt may have failed, or this site may block extraction. "
                    "Make sure `yt-dlp` and FFmpeg are installed in the SAME environment/container that runs the bot, then update yt-dlp and restart the bot. "
                    f"yt-dlp details: `{stderr[-500:] or 'no diagnostic returned'}` | ffprobe: `{probe_err[-150:] or 'no audio stream found'}`. "
                    "If this is YouTube, redeploy with a current yt-dlp version (update it in your requirements/deployment image)."
                )
                return

            stem = os.path.splitext(os.path.basename(urlparse(url).path) or "download")[0]
            safe_stem = "".join(c for c in stem if c.isalnum() or c in "-_ ").strip()[:70] or "download"
            mp3_path = os.path.join(tmp, "converted.mp3")
            try:
                await run_ffmpeg([
                    "-hide_banner", "-i", source_path,
                    "-map", "0:a:0", "-vn", "-sn", "-dn",
                    "-af", "aresample=44100:async=1:first_pts=0",
                    "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                    "-f", "mp3", mp3_path,
                ])
            except Exception as e:
                log.exception("audio conversion failed")
                await interaction.followup.send(
                    "The URL returned audio, but FFmpeg couldn't decode enough samples to create an MP3. "
                    "Try a different/direct audio URL. Details: `" + str(e)[-350:] + "`"
                )
                return

            if not os.path.isfile(mp3_path) or os.path.getsize(mp3_path) < 1024:
                await interaction.followup.send("Conversion produced an empty or invalid MP3. Try another URL.")
                return
            converted_size = os.path.getsize(mp3_path)
            if converted_size > MAX_DISCORD_UPLOAD:
                await interaction.followup.send(
                    f"Converted MP3 is {converted_size / (1024 * 1024):.1f} MB, over the Discord upload limit."
                )
                return
            code, verify_out, verify_err = await run_capture(
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,codec_type", "-of", "default=nw=1:nk=1", mp3_path,
            )
            if code != 0 or "audio" not in verify_out.lower() or "mp3" not in verify_out.lower():
                await interaction.followup.send(f"Converted file failed MP3 validation: `{verify_err[-250:]}`")
                return
            out_name = f"{safe_stem}.mp3"
            await interaction.followup.send(
                content=f"Downloaded and converted `{out_name}` ({converted_size / (1024 * 1024):.2f} MB).",
                file=discord.File(mp3_path, filename=out_name),
            )
    except Exception as e:
        log.exception("download command failed")
        await interaction.followup.send(f"Download failed: `{str(e)[:400]}`")


@tree.command(name="copyright", description="Identify a song in audio and check likely copyright indicators")
@anywhere
@app_commands.describe(file="Audio file to identify by listening/fingerprinting")
async def copyright_cmd(interaction: discord.Interaction, file: discord.Attachment):
    """Recognize audio through AudD. Track identification is not a legal copyright determination."""
    api_token = os.environ.get("AUDD_API_TOKEN", "338233d27fab1efb6f48aa957aea98c1").strip()
    if not api_token:
        await interaction.response.send_message(
            "Audio recognition isn't configured yet. Set the `AUDD_API_TOKEN` environment variable "
            "with an AudD API token on the bot host, then restart the bot.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    with tempfile.TemporaryDirectory() as tmp:
        ext = os.path.splitext(file.filename)[1] or ".audio"
        path = os.path.join(tmp, f"input{ext}")
        try:
            await file.save(path)
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                await interaction.followup.send("The uploaded file was empty or couldn't be saved.", ephemeral=True)
                return

            timeout = aiohttp.ClientTimeout(total=60)
            form = aiohttp.FormData()
            form.add_field("api_token", api_token)
            form.add_field("return", "apple_music,spotify,lyrics")
            with open(path, "rb") as audio_file:
                form.add_field(
                    "file", audio_file,
                    filename=os.path.basename(file.filename) or "audio.bin",
                    content_type="application/octet-stream",
                )
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post("https://api.audd.io/", data=form) as resp:
                        body = await resp.text()
                        if resp.status != 200:
                            await interaction.followup.send(
                                f"Audio recognition service returned HTTP {resp.status}. Try again later.",
                                ephemeral=True,
                            )
                            return
            try:
                result = json.loads(body)
            except json.JSONDecodeError:
                await interaction.followup.send("The audio recognition service returned an unreadable response.", ephemeral=True)
                return

            if result.get("status") != "success":
                error_info = result.get("error", {})
                error_text = error_info.get("error_message", "Recognition service could not process this file.") if isinstance(error_info, dict) else "Recognition service could not process this file."
                await interaction.followup.send(f"Audio recognition failed: {str(error_text)[:500]}", ephemeral=True)
                return

            track = result.get("result")
            if not track:
                await interaction.followup.send(
                    "**No matching recording found.** The audio may be original, obscure, noisy, edited, or too short to identify. "
                    "This does **not** mean it is copyright-free.",
                    ephemeral=True,
                )
                return

            title = str(track.get("title") or "Unknown title")[:180]
            artist = str(track.get("artist") or "Unknown artist")[:180]
            album = str(track.get("album") or "")[:180]
            release_date = str(track.get("release_date") or "")[:40]
            links = []
            for key, label in (("song_link", "Recognition result"),):
                link = track.get(key)
                if isinstance(link, str) and link.startswith(("https://", "http://")):
                    links.append(f"[{label}]({link})")
            for service in (track.get("apple_music"), track.get("spotify")):
                if isinstance(service, dict):
                    link = service.get("url") or service.get("external_urls", {}).get("spotify")
                    if isinstance(link, str) and link.startswith(("https://", "http://")):
                        links.append(f"[Open track]({link})")
                        break

            details = [
                "**Audio match found**",
                f"**Title:** {title}",
                f"**Artist:** {artist}",
            ]
            if album:
                details.append(f"**Album:** {album}")
            if release_date:
                details.append(f"**Release date:** {release_date}")
            if links:
                details.append(" · ".join(links))
            details.extend([
                "",
                "**Copyright assessment: likely an existing commercial/released recording; treat it as potentially copyrighted unless you have permission or a valid licence.**",
                "Audio matching identifies a recording; it does not prove ownership, licence status, or whether a particular use is lawful. No match is not proof that audio is copyright-free.",
            ])
            await interaction.followup.send("\n".join(details)[:1900], ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Audio recognition timed out. Try a shorter file or try again later.", ephemeral=True)
        except Exception as e:
            log.exception("copyright audio recognition failed")
            await interaction.followup.send(f"Audio recognition failed: `{str(e)[:350]}`", ephemeral=True)


@tree.command(name="boost100", description="Make audio very loud with compression while avoiding hard clipping")
@anywhere
@app_commands.describe(file="Audio file to make very loud; literal +100 dB would clip and sound bad")
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
        if code != 0 or "audio" not in stdout.lower():
            await interaction.followup.send("That attachment doesn't contain a readable audio stream.")
            return
        try:
            # A literal +100 dB multiplies sample amplitude by 100,000, which
            # necessarily clips ordinary audio. Instead, make a loud, usable file:
            # compress peaks, normalize integrated loudness, then apply a true-peak cap.
            # This is not 100 dB SPL and cannot guarantee a fixed playback loudness.
            await run_ffmpeg([
                "-i", src, "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                "-af", "acompressor=threshold=0.125:ratio=6:attack=5:release=120:makeup=2,loudnorm=I=-9:TP=-1.5:LRA=7,alimiter=limit=0.95:attack=5:release=50:level=false",
                "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2", out,
            ])
        except Exception as first_error:
            log.exception("boost100 primary pass failed; retrying with a simpler loudness filter")
            try:
                if os.path.exists(out):
                    os.remove(out)
                await run_ffmpeg([
                    "-i", src, "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                    "-af", "acompressor=threshold=0.125:ratio=4:attack=10:release=150,loudnorm=I=-12:TP=-1.5:LRA=7,alimiter=limit=0.95:level=false",
                    "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2", out,
                ])
            except Exception as fallback_error:
                log.exception("boost100 fallback also failed")
                await interaction.followup.send(
                    f"Boost failed on both processing methods. Primary: `{str(first_error)[-250:]}`; "
                    f"fallback: `{str(fallback_error)[-250:]}`"
                )
                return
        if not os.path.isfile(out) or os.path.getsize(out) < 1024:
            await interaction.followup.send("The boost produced an empty output file.")
            return
        size = os.path.getsize(out)
        if size > MAX_DISCORD_UPLOAD:
            await interaction.followup.send(f"Boosted MP3 is {size / (1024 * 1024):.1f} MB, over the upload limit.")
            return
        base = os.path.splitext(os.path.basename(file.filename))[0]
        out_name = f"{base}_boost100.mp3"
        await interaction.followup.send(
            content=(f"Created `{out_name}` ({size / (1024 * 1024):.2f} MB) using heavy compression and loudness normalization for a loud, cleaner result. "
                     "A literal +100 dB boost would clip and sound badly distorted; this file is not 100 dB SPL. Keep playback volume low."),
            file=discord.File(out, filename=out_name),
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

        # Use the requested 32-track method: duplicate the mono signal 32 times,
        # mix without normalization, multiply the mix by 32, then duplicate to stereo.
        # This can clip heavily; test at low playback volume.
        filter_complex = build_thirty_two_tracks_filter(bass_db=0.0, treble_db=0.0)
        try:
            await run_ffmpeg([
                "-i", src, "-filter_complex", filter_complex, "-map", "[stereo]", "-vn",
                "-c:a", "libmp3lame", "-q:a", "2", "-ar", str(OUT_RATE), "-ac", "2", out,
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
        output_name = f"{os.path.splitext(os.path.basename(file.filename))[0]}_32track_method.mp3"
        await interaction.followup.send(
            content=(f"Created `{output_name}` ({size / (1024 * 1024):.2f} MB) using the requested 32-track mix and linear volume multiplier of 32. "
                     "This can clip or distort severely; test with playback volume low."),
            file=discord.File(out, filename=output_name),
        )


@tree.command(name="roblox_upload", description="Upload an audio attachment to your Roblox profile")
@anywhere
@app_commands.describe(
    file="Audio file (MP3, WAV, OGG, or FLAC; under 20 MB)",
    name="Name for the Roblox audio asset",
    description="Optional description for the asset",
)
async def roblox_upload_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    name: str,
    description: str = "",
):
    """Submit audio to Roblox Open Cloud under the configured user's ownership."""
    api_key = os.environ.get("ROBLOX_OPEN_CLOUD_API_KEY", "").strip()
    user_id = os.environ.get("ROBLOX_USER_ID", "").strip()
    allowed_raw = os.environ.get("ROBLOX_UPLOADER_USER_IDS", "").strip()
    allowed_ids = {part.strip() for part in allowed_raw.split(",") if part.strip()}

    # Fail closed: only explicitly allowlisted Discord users may use the uploader.
    if not allowed_ids or str(interaction.user.id) not in allowed_ids:
        await interaction.response.send_message(
            "❌ You are not authorized to use this uploader. Configure "
            "`ROBLOX_UPLOADER_USER_IDS` with your Discord user ID on Fly.io.",
            ephemeral=True,
        )
        return

    if not api_key or not user_id:
        await interaction.response.send_message(
            "❌ Roblox upload is not configured. Set `ROBLOX_OPEN_CLOUD_API_KEY` "
            "and `ROBLOX_USER_ID` in Fly.io secrets.",
            ephemeral=True,
        )
        return

    try:
        user_id_int = int(user_id)
        if user_id_int <= 0:
            raise ValueError
    except ValueError:
        await interaction.response.send_message(
            "❌ `ROBLOX_USER_ID` must be your numeric Roblox profile user ID.", ephemeral=True
        )
        return

    name = name.strip()
    description = description.strip()
    if not name or len(name) > 50 or len(description) > 1000:
        await interaction.response.send_message(
            "❌ Name must be 1–50 characters; description must be 0–1000 characters.",
            ephemeral=True,
        )
        return

    ext = os.path.splitext(file.filename or "")[1].lower()
    content_types = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }
    if ext not in content_types:
        await interaction.response.send_message(
            "❌ Supported formats are MP3, WAV, OGG, and FLAC.", ephemeral=True
        )
        return
    if file.size <= 0 or file.size >= 20 * 1024 * 1024:
        await interaction.response.send_message(
            "❌ The audio file must be non-empty and smaller than 20 MB.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        audio_bytes = await file.read()
        if not audio_bytes or len(audio_bytes) >= 20 * 1024 * 1024:
            await interaction.followup.send(
                "❌ The downloaded attachment is empty or at least 20 MB.", ephemeral=True
            )
            return

        metadata = {
            "assetType": "Audio",
            "displayName": name,
            "description": description,
            "creationContext": {"creator": {"userId": user_id_int}},
        }
        timeout = aiohttp.ClientTimeout(total=120)
        form = aiohttp.FormData()
        form.add_field("request", json.dumps(metadata), content_type="application/json")
        form.add_field(
            "fileContent",
            audio_bytes,
            filename=os.path.basename(file.filename or f"audio{ext}"),
            content_type=content_types[ext],
        )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://apis.roblox.com/assets/v1/assets",
                headers={"x-api-key": api_key},
                data=form,
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except Exception:
                    payload = {}
                if response.status not in (200, 201, 202):
                    message = payload.get("message") or payload.get("title") or "Upload rejected"
                    await interaction.followup.send(
                        f"❌ Roblox API returned HTTP {response.status}: {str(message)[:500]}",
                        ephemeral=True,
                    )
                    return

        operation_path = payload.get("path", "")
        if not isinstance(operation_path, str) or not re.fullmatch(r"operations/[A-Za-z0-9_-]+", operation_path):
            await interaction.followup.send(
                "⚠️ Roblox received the request but returned an unexpected operation response. "
                "Check the Roblox Creator Dashboard and Fly.io logs.", ephemeral=True,
            )
            return

        # Poll for a short period; Roblox may continue processing after the command replies.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            for _ in range(15):
                await asyncio.sleep(2)
                async with session.get(
                    f"https://apis.roblox.com/assets/v1/{operation_path}",
                    headers={"x-api-key": api_key},
                ) as response:
                    try:
                        status_data = await response.json(content_type=None)
                    except Exception:
                        status_data = {}
                    if response.status >= 400:
                        break
                    if status_data.get("done") is True:
                        if status_data.get("error"):
                            error_data = status_data.get("error")
                            error_message = error_data.get("message", "Roblox processing failed") if isinstance(error_data, dict) else str(error_data)
                            await interaction.followup.send(
                                f"❌ Roblox processing failed: {error_message[:500]}", ephemeral=True
                            )
                            return
                        asset_id = (status_data.get("response") or {}).get("assetId")
                        if asset_id:
                            await interaction.followup.send(
                                f"✅ Roblox accepted the audio upload.\n"
                                f"**Name:** {name}\n**Roblox User ID:** `{user_id_int}`\n"
                                f"**Asset ID:** `{asset_id}`\n"
                                f"**Asset URL:** https://create.roblox.com/store/asset/{asset_id}\n\n"
                                "The asset may still need moderation before it can be used.",
                                ephemeral=True,
                            )
                        else:
                            await interaction.followup.send(
                                "✅ Processing finished, but Roblox did not return an asset ID. "
                                f"Response: `{json.dumps(status_data)[:700]}`", ephemeral=True
                            )
                        return

        await interaction.followup.send(
            "⏳ Roblox accepted the upload, but processing is still pending. "
            f"Operation: `{operation_path}`. Check again in Creator Dashboard; moderation may take longer.",
            ephemeral=True,
        )
    except asyncio.TimeoutError:
        await interaction.followup.send(
            "⏳ Roblox took too long to respond. The upload may still be processing; check Creator Dashboard.",
            ephemeral=True,
        )
    except Exception as exc:
        log.exception("Roblox audio upload failed")
        await interaction.followup.send(
            f"❌ Roblox upload failed: {type(exc).__name__}. Check Fly.io logs and API permissions.",
            ephemeral=True,
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
