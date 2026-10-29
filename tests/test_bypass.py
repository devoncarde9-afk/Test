from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from audiobot.bypass import probe_audio, process_bypass


def _make_audio(
    path: Path,
    duration: float,
    codec: str = "mp3",
    sample_rate: int = 44100,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is not installed")

    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}:sample_rate={sample_rate}",
        "-af",
        "pan=stereo|c0=c0|c1=c0",
    ]
    if codec == "mp3":
        args.extend(["-c:a", "libmp3lame", "-b:a", "128000"])
    else:
        args.extend(["-c:a", "libvorbis", "-q:a", "5"])
    args.extend(["-ar", str(sample_rate), "-ac", "2", str(path)])
    subprocess.run(args, check=True, capture_output=True)


def _intermediate(directory: Path, prefix: str, suffix: str) -> Path:
    matches = list(directory.glob(f"{prefix}_*{suffix}"))
    if len(matches) != 1:
        raise AssertionError(f"expected one {prefix} file, got {matches}")
    return matches[0]


def _ogg_serials(path: Path) -> set[int]:
    data = path.read_bytes()
    serials: set[int] = set()
    offset = 0
    while offset < len(data):
        start = data.find(b"OggS", offset)
        if start < 0 or start + 27 > len(data):
            break
        serials.add(struct.unpack_from("<I", data, start + 14)[0])
        segment_count = data[start + 26]
        header_end = start + 27 + segment_count
        if header_end > len(data):
            break
        offset = header_end + sum(data[start + 27 : header_end])
    return serials


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed"
)
class BypassTests(unittest.TestCase):
    def test_binary_concat_and_matching_mp3_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_dir = root / "out"
            out_dir.mkdir()
            source = root / "source.mp3"
            intro = root / "intro.mp3"
            outro = root / "outro.mp3"
            _make_audio(source, 5)
            _make_audio(intro, 1)
            _make_audio(outro, 2)

            output = process_bypass(
                source,
                out_dir,
                intro_path=intro,
                outro_path=outro,
            )

            self.assertEqual(output.suffix, ".mp3")
            intro_encoded = _intermediate(out_dir, "intro", ".mp3")
            outro_encoded = _intermediate(out_dir, "outro", ".mp3")
            self.assertEqual(
                output.read_bytes(),
                intro_encoded.read_bytes()
                + source.read_bytes()
                + outro_encoded.read_bytes(),
            )
            profile = probe_audio(output)
            self.assertEqual(profile.codec, "mp3")
            self.assertEqual(profile.sample_rate, 44100)
            self.assertEqual(profile.channels, 2)
            self.assertAlmostEqual(profile.duration, 8.0, delta=0.3)

    def test_trims_song_to_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_dir = root / "out"
            out_dir.mkdir()
            source = root / "source.mp3"
            intro = root / "intro.mp3"
            outro = root / "outro.mp3"
            _make_audio(source, 10)
            _make_audio(intro, 1)
            _make_audio(outro, 2)

            output = process_bypass(
                source,
                out_dir,
                intro_path=intro,
                outro_path=outro,
                max_output_seconds=8,
            )

            profile = probe_audio(output)
            self.assertLessEqual(profile.duration, 8.2)
            trimmed = _intermediate(out_dir, "song", ".mp3")
            self.assertLessEqual(probe_audio(trimmed).duration, 5.1)
            intro_encoded = _intermediate(out_dir, "intro", ".mp3")
            outro_encoded = _intermediate(out_dir, "outro", ".mp3")
            self.assertEqual(
                output.read_bytes(),
                intro_encoded.read_bytes()
                + trimmed.read_bytes()
                + outro_encoded.read_bytes(),
            )

    def test_ogg_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_dir = root / "out"
            out_dir.mkdir()
            source = root / "source.ogg"
            intro = root / "intro.ogg"
            outro = root / "outro.ogg"
            _make_audio(source, 5, codec="vorbis", sample_rate=48000)
            _make_audio(intro, 1, codec="vorbis", sample_rate=48000)
            _make_audio(outro, 2, codec="vorbis", sample_rate=48000)

            output = process_bypass(
                source,
                out_dir,
                intro_path=intro,
                outro_path=outro,
            )

            self.assertEqual(output.suffix, ".ogg")
            self.assertEqual(len(_ogg_serials(output)), 1)
            profile = probe_audio(output)
            self.assertEqual(profile.codec, "vorbis")
            self.assertEqual(profile.sample_rate, 48000)
            self.assertEqual(profile.channels, 2)
            self.assertAlmostEqual(profile.duration, 8.0, delta=0.3)


if __name__ == "__main__":
    unittest.main()
