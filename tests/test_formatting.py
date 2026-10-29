from __future__ import annotations

import unittest

from audiobot.formatting import (
    clean_error_message,
    format_duration,
    normalize_url,
    safe_filename,
    unique_ogg_filename,
)


class FormatDurationTests(unittest.TestCase):
    def test_unknown_values(self):
        for value in (None, 0, -5, "abc"):
            with self.subTest(value=value):
                self.assertEqual(format_duration(value), "ไม่ทราบ")

    def test_short_duration(self):
        self.assertEqual(format_duration(59), "0:59")

    def test_minute_duration(self):
        self.assertEqual(format_duration(60), "1:00")

    def test_hour_duration(self):
        self.assertEqual(format_duration(3661), "1:01:01")


class NormalizeUrlTests(unittest.TestCase):
    def test_keeps_full_url(self):
        self.assertEqual(
            normalize_url("https://youtu.be/abc"),
            "https://youtu.be/abc",
        )
        self.assertEqual(
            normalize_url("https://www.tiktok.com/@artist/video/123"),
            "https://www.tiktok.com/@artist/video/123",
        )
        self.assertEqual(
            normalize_url("https://example.com/audio"),
            "https://example.com/audio",
        )
        self.assertEqual(
            normalize_url("https://soundcloud.com/artist/track"),
            "https://soundcloud.com/artist/track",
        )
        self.assertEqual(
            normalize_url("https://on.soundcloud.com/abc123"),
            "https://on.soundcloud.com/abc123",
        )
        self.assertEqual(
            normalize_url("snd.sc/abc123"),
            "https://snd.sc/abc123",
        )

    def test_adds_scheme(self):
        self.assertEqual(
            normalize_url("youtu.be/abc"),
            "https://youtu.be/abc",
        )
        self.assertEqual(
            normalize_url("www.youtube.com/watch?v=abc"),
            "https://www.youtube.com/watch?v=abc",
        )
        self.assertEqual(
            normalize_url("music.youtube.com/watch?v=abc"),
            "https://music.youtube.com/watch?v=abc",
        )

    def test_strips_angle_brackets(self):
        self.assertEqual(
            normalize_url(" <https://youtu.be/abc> "),
            "https://youtu.be/abc",
        )

    def test_rejects_invalid_input(self):
        for value in (
            "",
            "ftp://example.com/audio",
            "https://",
            "https://[invalid/audio",
        ):
            with self.subTest(value=value):
                self.assertIsNone(normalize_url(value))


class SafeFilenameTests(unittest.TestCase):
    def test_removes_illegal_characters(self):
        name = safe_filename('a/b:c?"d')
        self.assertEqual(name, "a b c d.mp3")

    def test_soundcloud_quoted_title_has_no_added_underscores(self):
        self.assertEqual(
            safe_filename('First "Phonk Gang" audio'),
            "First Phonk Gang audio.mp3",
        )

    def test_uses_fallback_for_empty_title(self):
        self.assertEqual(safe_filename(""), "audio.mp3")

    def test_truncates_long_title(self):
        name = safe_filename("x" * 200)
        self.assertEqual(name, f"{'x' * 80}.mp3")

    def test_supports_custom_extension(self):
        self.assertEqual(safe_filename("Song", ".ogg"), "Song.ogg")
        self.assertEqual(safe_filename("Song", "ogg"), "Song.ogg")


class UniqueOggFilenameTests(unittest.TestCase):
    def test_returns_unique_ogg_names(self):
        first = unique_ogg_filename()
        second = unique_ogg_filename()
        self.assertTrue(first.endswith(".ogg"))
        self.assertNotEqual(first, second)


class CleanErrorMessageTests(unittest.TestCase):
    def test_removes_ansi_and_prefix(self):
        self.assertEqual(
            clean_error_message("\x1b[31mERROR: failed\x1b[0m"),
            "failed",
        )

    def test_truncates(self):
        self.assertEqual(len(clean_error_message("x" * 500, limit=100)), 100)


if __name__ == "__main__":
    unittest.main()
