from __future__ import annotations

from pathlib import Path

import discord
from discord.ext import commands

AUDIO_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}


def audio_attachments(message: object | None) -> list[discord.Attachment]:
    if message is None:
        return []
    return [
        attachment
        for attachment in getattr(message, "attachments", ())
        if Path(attachment.filename).suffix.lower() in AUDIO_EXTENSIONS
    ]


def audio_attachment(message: object | None) -> discord.Attachment | None:
    attachments = audio_attachments(message)
    return attachments[0] if attachments else None


async def referenced_message(ctx: commands.Context) -> object | None:
    reference = ctx.message.reference
    if reference is None or reference.message_id is None:
        return None

    resolved = reference.resolved
    if resolved is None:
        try:
            resolved = await ctx.channel.fetch_message(reference.message_id)
        except discord.HTTPException:
            return None

    return resolved


async def referenced_audio_attachment(
    ctx: commands.Context,
) -> discord.Attachment | None:
    return audio_attachment(await referenced_message(ctx))


async def resolve_audio_attachment(
    ctx: commands.Context,
) -> discord.Attachment | None:
    attachment = audio_attachment(ctx.message)
    if attachment is not None:
        return attachment
    return await referenced_audio_attachment(ctx)


async def resolve_audio_attachments(
    ctx: commands.Context,
    limit: int = 2,
) -> list[discord.Attachment]:
    attachments = list(audio_attachments(ctx.message))
    if len(attachments) < limit:
        reply = await referenced_message(ctx)
        for attachment in audio_attachments(reply):
            if any(existing is attachment for existing in attachments):
                continue
            attachments.append(attachment)
            if len(attachments) >= limit:
                break
    return attachments[:limit]
