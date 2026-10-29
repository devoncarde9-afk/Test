from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp

from audiobot.downloader import AudioDownloader, DownloadFailed


class FakeYoutubeDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=True):
        out_dir = Path(self.options["outtmpl"]).parent
        (out_dir / "abc.mp3").write_bytes(b"fake")
        return {
            "id": "abc",
            "title": "Song",
            "duration": 61,
            "uploader": "Channel",
            "webpage_url": url,
        }


class BuildOptionsTests(unittest.TestCase):
    def test_defaults(self):
        options = AudioDownloader().build_options(Path("out"))
        self.assertEqual(options["format"], "bestaudio/best")
        self.assertTrue(options["noplaylist"])
        self.assertNotIn("cookiefile", options)
        self.assertNotIn("js_runtimes", options)

    def test_includes_cookies_and_js_runtimes(self):
        downloader = AudioDownloader(
            cookies_file=Path("cookies.txt"),
            js_runtimes={"node": {}},
        )
        options = downloader.build_options(Path("out"))
        self.assertEqual(options["cookiefile"], "cookies.txt")
        self.assertEqual(options["js_runtimes"], {"node": {}})


class DownloadTests(unittest.TestCase):
    def test_returns_audio_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("audiobot.downloader.yt_dlp.YoutubeDL", FakeYoutubeDL):
                result = AudioDownloader().download(
                    "https://youtu.be/abc",
                    Path(tmp),
                )

            self.assertEqual(result.title, "Song")
            self.assertEqual(result.duration, 61)
            self.assertEqual(result.uploader, "Channel")
            self.assertEqual(result.video_id, "abc")
            self.assertEqual(result.path.name, "abc.mp3")
            self.assertEqual(result.path.read_bytes(), b"fake")

    def test_rejects_live_video(self):
        class LiveYoutubeDL(FakeYoutubeDL):
            def extract_info(self, url, download=True):
                return {"id": "abc", "title": "Live", "is_live": True}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("audiobot.downloader.yt_dlp.YoutubeDL", LiveYoutubeDL),
            self.assertRaisesRegex(RuntimeError, "ไลฟ์สด"),
        ):
            AudioDownloader().download("https://youtu.be/abc", Path(tmp))

    def test_wraps_ytdlp_errors(self):
        class BrokenYoutubeDL(FakeYoutubeDL):
            def extract_info(self, url, download=True):
                raise yt_dlp.utils.DownloadError("boom")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("audiobot.downloader.yt_dlp.YoutubeDL", BrokenYoutubeDL),
            self.assertRaises(DownloadFailed),
        ):
            AudioDownloader().download("https://youtu.be/abc", Path(tmp))


if __name__ == "__main__":
    unittest.main()
