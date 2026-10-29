from __future__ import annotations

import asyncio
import functools
import logging
import math
import shutil
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import discord
from discord.ext import commands

from audiobot.attachments import resolve_audio_attachment, resolve_audio_attachments
from audiobot.audio import (
    MAX_HIGHPASS_HZ,
    MAX_SHELF_GAIN_DB,
    MIN_HIGHPASS_HZ,
    MIN_SHELF_GAIN_DB,
    AudioProcessingError,
    parse_pitch,
    parse_speed,
    parse_stack_count,
    parse_tempo,
    parse_time_value,
    parse_volume_db,
    process_aeksets,
    process_aekloudlowhz,
    process_cut,
    process_eight_mono,
    process_fcut,
    process_mix,
    process_probe,
    process_pitch,
    process_speed,
    process_stack,
    process_tempo,
    process_thirty_two_tracks,
    process_volume,
)
from audiobot.bypass import process_bypass
from audiobot.downloader import AudioDownloader, DownloadFailed
from audiobot.formatting import (
    format_duration,
    normalize_url,
    safe_filename,
    unique_ogg_filename,
)

log = logging.getLogger(__name__)

LOADING_TEXT = "<a:483862loading:1564284084019003543> กำลังดาวน์โหลดเพลง"
SUCCESS_TEXT = "<a:752793check:1564288273948483676> ดาวน์โหลดเพลงเสร็จแล้วครับ"
FAILURE_TEXT = "<:6426error:1564288600135442484> ไม่สามารถดาวน์โหลดเพลงได้ในขณะนี้"
EIGHT_LOADING_TEXT = "<a:483862loading:1564284084019003543> กำลังประมวลผลเสียง"
EIGHT_SUCCESS_TEXT = "<a:752793check:1564288273948483676> ประมวลผลเสียงเสร็จแล้วครับ"
EIGHT_FAILURE_TEXT = "<:6426error:1564288600135442484> ไม่สามารถประมวลผลเสียงได้ในขณะนี้"
MIX_PROMPT_TEXT = "<a:2923printsdark:1564608850798190752> จะให้ต่อแบบไหนครับ?"
MIX_TIMEOUT_TEXT = "<:6426error:1564288600135442484> หมดเวลาให้เลือกแล้วครับ"
MIX_LOADING_TEXT = "<a:483862loading:1564284084019003543> กำลังประมวลผลเสียง"
MIX_SUCCESS_TEXT = "<a:752793check:1564288273948483676> ประมวลผลเสียงเสร็จแล้วครับ"
MIX_FAILURE_TEXT = "<:6426error:1564288600135442484> ไม่สามารถประมวลผลเสียงได้ในขณะนี้"
BOT_COLOR = 0x1DB954
DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MB = 1024 * 1024


async def _safe_edit(message: discord.Message, content: str) -> None:
    try:
        await message.edit(content=content)
    except discord.HTTPException:
        pass


def _upload_limit(ctx: commands.Context) -> int:
    limit = getattr(ctx.guild, "filesize_limit", None) if ctx.guild else None
    if not limit:
        limit = DEFAULT_UPLOAD_LIMIT
    return max(int(limit * 0.98), 1 * MB)


def _parse_processing_args(
    highpass: str | None,
    bass: str | None,
    treble: str | None,
) -> tuple[int | None, float, float]:
    highpass_hz: int | None = None
    if highpass is not None and highpass.casefold() not in {"0", "off", "none", "-"}:
        highpass_hz = int(highpass)
        if not MIN_HIGHPASS_HZ <= highpass_hz <= MAX_HIGHPASS_HZ:
            raise ValueError

    bass_db = 0.0 if bass is None else float(bass)
    treble_db = 0.0 if treble is None else float(treble)
    if not MIN_SHELF_GAIN_DB <= bass_db <= MAX_SHELF_GAIN_DB:
        raise ValueError
    if not MIN_SHELF_GAIN_DB <= treble_db <= MAX_SHELF_GAIN_DB:
        raise ValueError

    return highpass_hz, bass_db, treble_db


def _mix_label(filename: str) -> str:
    name = Path(filename).stem.strip() or Path(filename).name.strip()
    return (name or "?")[:35]


def _format_dbfs(value: float) -> str:
    if math.isinf(value) and value < 0:
        return "-inf"
    return f"{value:.1f}"


class MixOrderView(discord.ui.View):
    def __init__(
        self,
        ctx: commands.Context,
        attachments: list[discord.Attachment],
        executor: ThreadPoolExecutor,
    ) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.attachments = attachments
        self.executor = executor
        self.message: discord.Message | None = None

        first = _mix_label(attachments[0].filename)
        second = _mix_label(attachments[1].filename)
        self._add_order_button(
            first,
            second,
            0,
            discord.ButtonStyle.primary,
        )
        self._add_order_button(
            second,
            first,
            1,
            discord.ButtonStyle.success,
        )

    def _add_order_button(
        self,
        front: str,
        back: str,
        order: int,
        style: discord.ButtonStyle,
    ) -> None:
        button = discord.ui.Button(
            label=f"{front} | {back}"[:80],
            style=style,
        )
        button.callback = functools.partial(self._choose, order=order)
        self.add_item(button)

    async def _choose(
        self,
        interaction: discord.Interaction,
        order: int,
    ) -> None:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "<:6426error:1564288600135442484> ไม่ใช่ของคุณนะครับ",
                ephemeral=True,
            )
            return

        self.stop()
        try:
            await interaction.response.edit_message(
                content=MIX_LOADING_TEXT,
                view=None,
            )
        except discord.HTTPException:
            return

        if order == 0:
            first, second = self.attachments
        else:
            first, second = self.attachments[1], self.attachments[0]

        tmpdir = Path(tempfile.mkdtemp(prefix="audiobot_mix_"))
        loop = asyncio.get_running_loop()

        try:
            first_path = tmpdir / f"first{Path(first.filename).suffix.lower()}"
            second_path = tmpdir / f"second{Path(second.filename).suffix.lower()}"
            await first.save(str(first_path))
            await second.save(str(second_path))

            output_name = unique_ogg_filename()
            output_path = tmpdir / output_name

            await loop.run_in_executor(
                self.executor,
                functools.partial(
                    process_mix,
                    first_path,
                    second_path,
                    output_path,
                ),
            )

            if output_path.stat().st_size > _upload_limit(self.ctx):
                await self._edit(MIX_FAILURE_TEXT)
                return

            file = discord.File(str(output_path), filename=output_name)
            await self._edit(MIX_SUCCESS_TEXT, attachments=[file])

        except AudioProcessingError as exc:
            log.exception("รวมไฟล์เสียงไม่สำเร็จ: %s", exc)
            await self._edit(MIX_FAILURE_TEXT)
        except discord.HTTPException as exc:
            log.warning("อัปโหลดไม่สำเร็จ: %s", exc)
        except Exception:
            log.exception("เกิดข้อผิดพลาดในคำสั่ง mix")
            await self._edit(MIX_FAILURE_TEXT)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def _edit(
        self,
        content: str,
        attachments: list[discord.File] | None = None,
    ) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(
                content=content,
                attachments=attachments or [],
                view=None,
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        await self._edit(MIX_TIMEOUT_TEXT)


class DownloadCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        downloader: AudioDownloader,
        executor: ThreadPoolExecutor,
    ) -> None:
        self.bot = bot
        self.downloader = downloader
        self.executor = executor

    async def _apply_effect(
        self,
        ctx: commands.Context,
        name: str,
        value: str | None,
        parser: Callable[[str], float],
        processor: Callable[[Path, Path, float], None],
    ) -> None:
        attachment = await resolve_audio_attachment(ctx)
        if (
            not value
            or attachment is None
            or attachment.size > MAX_ATTACHMENT_BYTES
        ):
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        try:
            factor = parser(value)
        except ValueError:
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        tmpdir = Path(tempfile.mkdtemp(prefix=f"audiobot_{name}_"))
        status = await ctx.reply(EIGHT_LOADING_TEXT, mention_author=False)
        loop = asyncio.get_running_loop()

        try:
            suffix = Path(attachment.filename).suffix.lower()
            source_path = tmpdir / f"input{suffix}"
            await attachment.save(str(source_path))

            output_name = unique_ogg_filename()
            output_path = tmpdir / output_name

            await loop.run_in_executor(
                self.executor,
                functools.partial(
                    processor,
                    source_path,
                    output_path,
                    factor,
                ),
            )

            if output_path.stat().st_size > _upload_limit(ctx):
                await _safe_edit(status, EIGHT_FAILURE_TEXT)
                return

            file = discord.File(str(output_path), filename=output_name)
            await status.edit(content=EIGHT_SUCCESS_TEXT, attachments=[file])

        except AudioProcessingError as exc:
            log.warning("ประมวลผลเสียงไม่สำเร็จ (%s): %s", name, exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except discord.HTTPException as exc:
            log.warning("อัปโหลดไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except Exception:
            log.exception("เกิดข้อผิดพลาดในคำสั่ง %s", name)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def _apply_tone_effect(
        self,
        ctx: commands.Context,
        name: str,
        value: str | None,
        highpass: str | None,
        bass: str | None,
        treble: str | None,
        parser: Callable[[str], float | int],
        processor: Callable[..., None],
    ) -> None:
        attachment = await resolve_audio_attachment(ctx)
        if (
            value is None
            or attachment is None
            or attachment.size > MAX_ATTACHMENT_BYTES
        ):
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        try:
            amount = parser(value)
            highpass_hz, bass_db, treble_db = _parse_processing_args(
                highpass,
                bass,
                treble,
            )
        except ValueError:
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        tmpdir = Path(tempfile.mkdtemp(prefix=f"audiobot_{name}_"))
        status = await ctx.reply(EIGHT_LOADING_TEXT, mention_author=False)
        loop = asyncio.get_running_loop()

        try:
            suffix = Path(attachment.filename).suffix.lower()
            source_path = tmpdir / f"input{suffix}"
            await attachment.save(str(source_path))

            output_name = unique_ogg_filename()
            output_path = tmpdir / output_name

            await loop.run_in_executor(
                self.executor,
                functools.partial(
                    processor,
                    source_path,
                    output_path,
                    amount,
                    highpass_hz,
                    bass_db,
                    treble_db,
                ),
            )

            if output_path.stat().st_size > _upload_limit(ctx):
                await _safe_edit(status, EIGHT_FAILURE_TEXT)
                return

            file = discord.File(str(output_path), filename=output_name)
            await status.edit(content=EIGHT_SUCCESS_TEXT, attachments=[file])

        except AudioProcessingError as exc:
            log.warning("ประมวลผลเสียงไม่สำเร็จ (%s): %s", name, exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except discord.HTTPException as exc:
            log.warning("อัปโหลดไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except Exception:
            log.exception("เกิดข้อผิดพลาดในคำสั่ง %s", name)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @commands.command(name="dl")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def dl(self, ctx: commands.Context, *, link: str | None = None) -> None:
        if not link:
            await ctx.reply(FAILURE_TEXT, mention_author=False)
            return

        url = normalize_url(link)
        if url is None:
            await ctx.reply(FAILURE_TEXT, mention_author=False)
            return

        tmpdir = Path(tempfile.mkdtemp(prefix="audiobot_"))
        status = await ctx.reply(LOADING_TEXT, mention_author=False)
        loop = asyncio.get_running_loop()

        try:
            result = await loop.run_in_executor(
                self.executor,
                functools.partial(self.downloader.download, url, tmpdir),
            )

            audio_path = result.path
            limit = _upload_limit(ctx)

            if audio_path.stat().st_size > limit:
                shrunk = await loop.run_in_executor(
                    self.executor,
                    functools.partial(self.downloader.shrink, audio_path, limit),
                )
                if shrunk is None:
                    await _safe_edit(status, FAILURE_TEXT)
                    return
                audio_path = shrunk

            filename = safe_filename(result.title)
            file = discord.File(str(audio_path), filename=filename)
            await status.edit(content=SUCCESS_TEXT, attachments=[file])

        except DownloadFailed as exc:
            log.warning("ดาวน์โหลดไม่สำเร็จ: %s", exc)
            await _safe_edit(status, FAILURE_TEXT)
        except RuntimeError as exc:
            log.warning("ดาวน์โหลดไม่สำเร็จ: %s", exc)
            await _safe_edit(status, FAILURE_TEXT)
        except discord.HTTPException as exc:
            log.warning("อัปโหลดไม่สำเร็จ: %s", exc)
            await _safe_edit(status, FAILURE_TEXT)
        except Exception:
            log.exception("เกิดข้อผิดพลาดในคำสั่ง dl")
            await _safe_edit(status, FAILURE_TEXT)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @commands.command(name="8mono")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def eight_mono(
        self,
        ctx: commands.Context,
        highpass: str | None = None,
        bass: str | None = None,
        treble: str | None = None,
    ) -> None:
        attachment = await resolve_audio_attachment(ctx)
        if attachment is None or attachment.size > MAX_ATTACHMENT_BYTES:
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        try:
            highpass_hz, bass_db, treble_db = _parse_processing_args(
                highpass,
                bass,
                treble,
            )
        except ValueError:
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        tmpdir = Path(tempfile.mkdtemp(prefix="audiobot_8mono_"))
        status = await ctx.reply(EIGHT_LOADING_TEXT, mention_author=False)
        loop = asyncio.get_running_loop()

        try:
            suffix = Path(attachment.filename).suffix.lower()
            source_path = tmpdir / f"input{suffix}"
            await attachment.save(str(source_path))

            output_name = unique_ogg_filename()
            output_path = tmpdir / output_name

            await loop.run_in_executor(
                self.executor,
                functools.partial(
                    process_eight_mono,
                    source_path,
                    output_path,
                    highpass_hz,
                    bass_db,
                    treble_db,
                ),
            )

            if output_path.stat().st_size > _upload_limit(ctx):
                await _safe_edit(status, EIGHT_FAILURE_TEXT)
                return

            file = discord.File(str(output_path), filename=output_name)
            await status.edit(content=EIGHT_SUCCESS_TEXT, attachments=[file])

        except AudioProcessingError as exc:
            log.exception("ประมวลผลเสียงไม่สำเร็จในคำสั่ง bypass: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except discord.HTTPException as exc:
            log.warning("อัปโหลดไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except Exception:
            log.exception("เกิดข้อผิดพลาดในคำสั่ง 8mono")
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @commands.command(name="32tracks")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def thirty_two_tracks(
        self,
        ctx: commands.Context,
        highpass: str | None = None,
        bass: str | None = None,
        treble: str | None = None,
    ) -> None:
        attachment = await resolve_audio_attachment(ctx)
        if attachment is None or attachment.size > MAX_ATTACHMENT_BYTES:
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        try:
            highpass_hz, bass_db, treble_db = _parse_processing_args(
                highpass,
                bass,
                treble,
            )
        except ValueError:
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        tmpdir = Path(tempfile.mkdtemp(prefix="audiobot_32tracks_"))
        status = await ctx.reply(EIGHT_LOADING_TEXT, mention_author=False)
        loop = asyncio.get_running_loop()

        try:
            suffix = Path(attachment.filename).suffix.lower()
            source_path = tmpdir / f"input{suffix}"
            await attachment.save(str(source_path))

            output_name = unique_ogg_filename()
            output_path = tmpdir / output_name

            await loop.run_in_executor(
                self.executor,
                functools.partial(
                    process_thirty_two_tracks,
                    source_path,
                    output_path,
                    highpass_hz,
                    bass_db,
                    treble_db,
                ),
            )

            if output_path.stat().st_size > _upload_limit(ctx):
                await _safe_edit(status, EIGHT_FAILURE_TEXT)
                return

            file = discord.File(str(output_path), filename=output_name)
            await status.edit(content=EIGHT_SUCCESS_TEXT, attachments=[file])

        except AudioProcessingError as exc:
            log.warning("ประมวลผลเสียงไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except discord.HTTPException as exc:
            log.warning("อัปโหลดไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except Exception:
            log.exception("เกิดข้อผิดพลาดในคำสั่ง 32tracks")
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @commands.command(name="vol")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def vol(
        self,
        ctx: commands.Context,
        value: str | None = None,
        highpass: str | None = None,
        bass: str | None = None,
        treble: str | None = None,
    ) -> None:
        await self._apply_tone_effect(
            ctx,
            "vol",
            value,
            highpass,
            bass,
            treble,
            parse_volume_db,
            process_volume,
        )

    @commands.command(name="stack")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def stack(
        self,
        ctx: commands.Context,
        value: str | None = None,
        highpass: str | None = None,
        bass: str | None = None,
        treble: str | None = None,
    ) -> None:
        await self._apply_tone_effect(
            ctx,
            "stack",
            value,
            highpass,
            bass,
            treble,
            parse_stack_count,
            process_stack,
        )

    @commands.command(name="bypass")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def bypass(self, ctx: commands.Context) -> None:
        attachment = await resolve_audio_attachment(ctx)
        if attachment is None or attachment.size > MAX_ATTACHMENT_BYTES:
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        tmpdir = Path(tempfile.mkdtemp(prefix="audiobot_bypass_"))
        status = await ctx.reply(EIGHT_LOADING_TEXT, mention_author=False)
        loop = asyncio.get_running_loop()

        try:
            suffix = Path(attachment.filename).suffix.lower()
            source_path = tmpdir / f"input{suffix}"
            await attachment.save(str(source_path))

            output_path = await loop.run_in_executor(
                self.executor,
                functools.partial(process_bypass, source_path, tmpdir),
            )

            if output_path.stat().st_size > _upload_limit(ctx):
                await _safe_edit(status, EIGHT_FAILURE_TEXT)
                return

            file = discord.File(str(output_path), filename=output_path.name)
            await status.edit(content=EIGHT_SUCCESS_TEXT, attachments=[file])

        except AudioProcessingError as exc:
            log.warning("ประมวลผลเสียงไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except discord.HTTPException as exc:
            log.warning("อัปโหลดไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except Exception:
            log.exception("เกิดข้อผิดพลาดในคำสั่ง bypass")
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @commands.command(name="aeksets")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def aeksets(self, ctx: commands.Context) -> None:
        attachment = await resolve_audio_attachment(ctx)
        if attachment is None or attachment.size > MAX_ATTACHMENT_BYTES:
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        tmpdir = Path(tempfile.mkdtemp(prefix="audiobot_aeksets_"))
        status = await ctx.reply(EIGHT_LOADING_TEXT, mention_author=False)
        loop = asyncio.get_running_loop()

        try:
            suffix = Path(attachment.filename).suffix.lower()
            source_path = tmpdir / f"input{suffix}"
            await attachment.save(str(source_path))

            output_name = unique_ogg_filename()
            output_path = tmpdir / output_name

            await loop.run_in_executor(
                self.executor,
                functools.partial(process_aeksets, source_path, output_path),
            )

            if output_path.stat().st_size > _upload_limit(ctx):
                await _safe_edit(status, EIGHT_FAILURE_TEXT)
                return

            file = discord.File(str(output_path), filename=output_name)
            await status.edit(content=EIGHT_SUCCESS_TEXT, attachments=[file])

        except AudioProcessingError as exc:
            log.warning("ประมวลผลเสียงไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except discord.HTTPException as exc:
            log.warning("อัปโหลดไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except Exception:
            log.exception("เกิดข้อผิดพลาดในคำสั่ง aeksets")
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @commands.command(name="aekloudlowhz")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def aekloudlowhz(self, ctx: commands.Context) -> None:
        attachment = await resolve_audio_attachment(ctx)
        if attachment is None or attachment.size > MAX_ATTACHMENT_BYTES:
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        tmpdir = Path(tempfile.mkdtemp(prefix="audiobot_aekloudlowhz_"))
        status = await ctx.reply(EIGHT_LOADING_TEXT, mention_author=False)
        loop = asyncio.get_running_loop()

        try:
            suffix = Path(attachment.filename).suffix.lower()
            source_path = tmpdir / f"input{suffix}"
            await attachment.save(str(source_path))

            output_name = unique_ogg_filename()
            output_path = tmpdir / output_name

            await loop.run_in_executor(
                self.executor,
                functools.partial(process_aekloudlowhz, source_path, output_path),
            )

            if output_path.stat().st_size > _upload_limit(ctx):
                await _safe_edit(status, EIGHT_FAILURE_TEXT)
                return

            file = discord.File(str(output_path), filename=output_name)
            await status.edit(content=EIGHT_SUCCESS_TEXT, attachments=[file])

        except AudioProcessingError as exc:
            log.warning("ประมวลผลเสียงไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except discord.HTTPException as exc:
            log.warning("อัปโหลดไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except Exception:
            log.exception("เกิดข้อผิดพลาดในคำสั่ง aekloudlowhz")
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @commands.command(name="speed")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def speed(
        self,
        ctx: commands.Context,
        value: str | None = None,
    ) -> None:
        await self._apply_effect(ctx, "speed", value, parse_speed, process_speed)

    @commands.command(name="pitch")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def pitch(
        self,
        ctx: commands.Context,
        value: str | None = None,
    ) -> None:
        await self._apply_effect(ctx, "pitch", value, parse_pitch, process_pitch)

    @commands.command(name="tempo")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def tempo(
        self,
        ctx: commands.Context,
        value: str | None = None,
    ) -> None:
        await self._apply_effect(ctx, "tempo", value, parse_tempo, process_tempo)

    @commands.command(name="cut")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def cut(
        self,
        ctx: commands.Context,
        value: str | None = None,
    ) -> None:
        await self._apply_effect(ctx, "cut", value, parse_time_value, process_cut)

    @commands.command(name="fcut")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def fcut(
        self,
        ctx: commands.Context,
        value: str | None = None,
    ) -> None:
        await self._apply_effect(ctx, "fcut", value, parse_time_value, process_fcut)

    @commands.command(name="probe")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def probe(self, ctx: commands.Context) -> None:
        attachment = await resolve_audio_attachment(ctx)
        if attachment is None or attachment.size > MAX_ATTACHMENT_BYTES:
            await ctx.reply(EIGHT_FAILURE_TEXT, mention_author=False)
            return

        tmpdir = Path(tempfile.mkdtemp(prefix="audiobot_probe_"))
        status = await ctx.reply(EIGHT_LOADING_TEXT, mention_author=False)
        loop = asyncio.get_running_loop()

        try:
            suffix = Path(attachment.filename).suffix.lower()
            source_path = tmpdir / f"input{suffix}"
            await attachment.save(str(source_path))

            waveform_name = "waveform.png"
            waveform_path = tmpdir / waveform_name
            result = await loop.run_in_executor(
                self.executor,
                functools.partial(process_probe, source_path, waveform_path),
            )

            if waveform_path.stat().st_size > _upload_limit(ctx):
                await _safe_edit(status, EIGHT_FAILURE_TEXT)
                return

            embed = discord.Embed(color=discord.Color(0xFF69B4))
            embed.set_author(
                name=f"Checked Audio For {ctx.author.display_name}"[:256],
                icon_url=(
                    "https://cdn.discordapp.com/emojis/"
                    "1564636387771158680.gif?size=44&animated=true"
                ),
            )
            embed.add_field(
                name="LUFS",
                value=f"{result.integrated_lufs:.1f}",
                inline=True,
            )
            embed.add_field(
                name="Peak",
                value=_format_dbfs(result.true_peak_dbfs),
                inline=True,
            )
            embed.add_field(
                name="Duration",
                value=format_duration(result.duration_seconds),
                inline=True,
            )
            embed.set_image(url=f"attachment://{waveform_name}")

            waveform_file = discord.File(str(waveform_path), filename=waveform_name)
            await status.edit(
                content=None,
                embed=embed,
                attachments=[waveform_file],
            )

        except AudioProcessingError as exc:
            log.warning("วิเคราะห์เสียงไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except discord.HTTPException as exc:
            log.warning("ส่งผลวิเคราะห์เสียงไม่สำเร็จ: %s", exc)
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        except Exception:
            log.exception("เกิดข้อผิดพลาดในคำสั่ง probe")
            await _safe_edit(status, EIGHT_FAILURE_TEXT)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @commands.command(name="mix")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def mix(self, ctx: commands.Context) -> None:
        attachments = await resolve_audio_attachments(ctx, 2)
        if len(attachments) < 2 or any(
            attachment.size > MAX_ATTACHMENT_BYTES for attachment in attachments
        ):
            await ctx.reply(MIX_FAILURE_TEXT, mention_author=False)
            return

        view = MixOrderView(ctx, attachments, self.executor)
        view.message = await ctx.reply(
            MIX_PROMPT_TEXT,
            view=view,
            mention_author=False,
        )

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        await ctx.reply(f"Pong! {self.bot.latency * 1000:.0f} ms", mention_author=False)

    @commands.command(name="help")
    async def help_command(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            color=discord.Color(0xFF69B4),
        )
        embed.set_author(
            name=":  All commands for audio",
            icon_url="https://cdn.discordapp.com/emojis/1564613366969016450.png",
        )
        embed.add_field(
            name=">dl <link>",
            value="ดาวน์โหลดเพลงจาก YouTube Tiktok Soundcloud",
            inline=False,
        )
        embed.add_field(
            name=">8mono <highpass> <bass> <treble>",
            value=(
                "ทำเพลงดังสูตร 8 Mono"
            ),
            inline=False,
        )
        embed.add_field(
            name=">32tracks <highpass> <bass> <treble>",
            value=(
                "ทำเพลงดังสูตร 32 Tracks"
            ),
            inline=False,
        )
        embed.add_field(
            name=">vol <value> <highpass> <bass> <treble>",
            value="ปรับเสียงดังเบา (ค่า dB ตั้งแต่ -30 ถึง 100)",
            inline=False,
        )
        embed.add_field(
            name=">stack <value> <highpass> <bass> <treble>",
            value="ซ้อนเสียงจนดัง",
            inline=False,
        )
        embed.add_field(
            name=">probe",
            value="เช็คความดัง",
            inline=False,
        )
        embed.add_field(
            name=">bypass",
            value=(
                "บายพาสเพลงดัง"
            ),
            inline=False,
        )
        embed.add_field(
            name=">aeksets",
            value=("ทำเพลงดังสูตรเอกลั่นๆ"),
            inline=False,
        )
        embed.add_field(
            name=">aekloudlowhz",
            value="ทำดังสูตรเอกย่านตํ่า",
            inline=False,
        )
        embed.add_field(
            name=">speed <value>",
            value=(
                "เร่งสปีดเพลงเร็วช้า"
            ),
            inline=False,
        )
        embed.add_field(
            name=">pitch <value>",
            value=(
                "ปรับเสียงใหญ่เสียงเล็ก"
            ),
            inline=False,
        )
        embed.add_field(
            name=">tempo <value>",
            value=(
                "เร่งสปีดเพลงเร็วช้าแบบไม่ปรับ pitch"
            ),
            inline=False,
        )
        embed.add_field(
            name=">cut <value>",
            value="ตัดเวลาเพลง",
            inline=False,
        )
        embed.add_field(
            name=">fcut <value>",
            value="ตัดเวลาหน้าเพลงออก",
            inline=False,
        )
        embed.add_field(
            name=">mix",
            value=(
                "ต่ออินโทรใส่เพลง"
            ),
            inline=False,
        )

        embed.set_footer(
            text="@AEKz | Core i3 Gang | Special Edition",
            icon_url="https://cdn.discordapp.com/emojis/1564617578435838052.png",
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def cog_command_error(
        self,
        ctx: commands.Context,
        error: Exception,
    ) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            return

        log.error("คำสั่ง %s ผิดพลาด", ctx.command, exc_info=error)
        try:
            await ctx.reply(FAILURE_TEXT, mention_author=False)
        except discord.HTTPException:
            pass
