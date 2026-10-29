from __future__ import annotations

import unittest
from types import SimpleNamespace

from audiobot.attachments import (
    audio_attachment,
    resolve_audio_attachment,
    resolve_audio_attachments,
)


def _attachment(filename: str) -> SimpleNamespace:
    return SimpleNamespace(filename=filename, size=1)


class _FakeChannel:
    def __init__(self, message: SimpleNamespace) -> None:
        self.message = message
        self.fetched_ids: list[int] = []

    async def fetch_message(self, message_id: int) -> SimpleNamespace:
        self.fetched_ids.append(message_id)
        return self.message


class AudioAttachmentResolutionTests(unittest.IsolatedAsyncioTestCase):
    def test_audio_attachment_skips_non_audio(self):
        message = SimpleNamespace(
            attachments=[_attachment("notes.txt"), _attachment("song.MP3")]
        )

        attachment = audio_attachment(message)

        self.assertIsNotNone(attachment)
        self.assertEqual(attachment.filename, "song.MP3")

    async def test_own_attachment_takes_priority_over_reply(self):
        own = _attachment("own.wav")
        reply = _attachment("reply.ogg")
        ctx = SimpleNamespace(
            message=SimpleNamespace(
                attachments=[own],
                reference=SimpleNamespace(
                    message_id=1,
                    resolved=SimpleNamespace(attachments=[reply]),
                ),
            ),
            channel=None,
        )

        attachment = await resolve_audio_attachment(ctx)

        self.assertIs(attachment, own)

    async def test_uses_audio_from_replied_message(self):
        reply = _attachment("reply.ogg")
        ctx = SimpleNamespace(
            message=SimpleNamespace(
                attachments=[],
                reference=SimpleNamespace(
                    message_id=1,
                    resolved=SimpleNamespace(attachments=[reply]),
                ),
            ),
            channel=None,
        )

        attachment = await resolve_audio_attachment(ctx)

        self.assertIs(attachment, reply)

    async def test_falls_back_to_reply_when_own_attachment_is_not_audio(self):
        reply = _attachment("reply.ogg")
        ctx = SimpleNamespace(
            message=SimpleNamespace(
                attachments=[_attachment("notes.txt")],
                reference=SimpleNamespace(
                    message_id=1,
                    resolved=SimpleNamespace(attachments=[reply]),
                ),
            ),
            channel=None,
        )

        attachment = await resolve_audio_attachment(ctx)

        self.assertIs(attachment, reply)

    async def test_fetches_reference_when_not_cached(self):
        reply = _attachment("reply.ogg")
        channel = _FakeChannel(SimpleNamespace(attachments=[reply]))
        ctx = SimpleNamespace(
            message=SimpleNamespace(
                attachments=[],
                reference=SimpleNamespace(message_id=42, resolved=None),
            ),
            channel=channel,
        )

        attachment = await resolve_audio_attachment(ctx)

        self.assertIs(attachment, reply)
        self.assertEqual(channel.fetched_ids, [42])

    async def test_returns_none_without_attachment_or_reference(self):
        ctx = SimpleNamespace(
            message=SimpleNamespace(attachments=[], reference=None),
            channel=None,
        )

        self.assertIsNone(await resolve_audio_attachment(ctx))


class MultipleAudioAttachmentResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_two_attachments_in_message_order(self):
        first = _attachment("first.mp3")
        second = _attachment("second.ogg")
        ctx = SimpleNamespace(
            message=SimpleNamespace(
                attachments=[first, second],
                reference=None,
            ),
            channel=None,
        )

        attachments = await resolve_audio_attachments(ctx)

        self.assertEqual(attachments, [first, second])

    async def test_combines_own_attachment_with_reply(self):
        own = _attachment("own.wav")
        reply = _attachment("reply.ogg")
        ctx = SimpleNamespace(
            message=SimpleNamespace(
                attachments=[own],
                reference=SimpleNamespace(
                    message_id=1,
                    resolved=SimpleNamespace(attachments=[reply]),
                ),
            ),
            channel=None,
        )

        attachments = await resolve_audio_attachments(ctx)

        self.assertEqual(attachments, [own, reply])

    async def test_returns_fewer_when_not_enough_audio(self):
        ctx = SimpleNamespace(
            message=SimpleNamespace(
                attachments=[_attachment("only.mp3"), _attachment("notes.txt")],
                reference=None,
            ),
            channel=None,
        )

        attachments = await resolve_audio_attachments(ctx)

        self.assertEqual(len(attachments), 1)
