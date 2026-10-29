from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import discord
from discord.ext import commands

from audiobot.access import has_access
from audiobot.cogs.download import DownloadCog
from audiobot.config import Settings
from audiobot.downloader import AudioDownloader

log = logging.getLogger(__name__)


class AudioBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=">", intents=intents, help_command=None)
        self.settings = settings
        self.downloader = AudioDownloader(
            cookies_file=settings.cookies_file,
            js_runtimes=settings.js_runtimes,
        )
        self.download_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="audiobot-download",
        )

    async def setup_hook(self) -> None:
        await self.add_cog(DownloadCog(self, self.downloader, self.download_executor))

    async def on_ready(self) -> None:
        log.info(
            "เข้าสู่ระบบในชื่อ %s (id: %s)",
            self.user,
            self.user.id if self.user else "?",
        )
        log.info(
            "คำสั่งที่ใช้ได้: >dl <ลิงก์>, >8mono, >32tracks, >bypass, "
            ">aeksets, >aekloudlowhz, >vol, >stack, >speed <value>, "
            ">pitch <value>, >tempo <value>, "
            ">cut <time.value>, >fcut <time.value>, >probe, >mix, >help, >ping"
        )
        log.info(
            "จำกัดการใช้งานที่ห้อง %s และต้องมียศ %s",
            self.settings.allowed_channel_id,
            self.settings.required_role_id,
        )
        if self.settings.cookies_file:
            log.info("ใช้ cookie จากไฟล์ %s", self.settings.cookies_file.name)
        else:
            log.info("ไม่ได้ใช้ cookie")
        if self.settings.js_runtimes:
            log.info(
                "ใช้ JS runtime: %s",
                ", ".join(sorted(self.settings.js_runtimes)),
            )
        else:
            log.warning("ไม่พบ Node.js หรือ Deno; การใช้ cookie อาจล้มเหลว")

    async def on_message(self, message: discord.Message) -> None:
        if not has_access(
            message,
            allowed_channel_id=self.settings.allowed_channel_id,
            required_role_id=self.settings.required_role_id,
        ):
            return
        await self.process_commands(message)

    async def on_command_error(
        self,
        ctx: commands.Context,
        error: Exception,
    ) -> None:
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
            return

        log.error("คำสั่ง %s ผิดพลาด", ctx.command, exc_info=error)
        try:
            await ctx.reply(
                "เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
