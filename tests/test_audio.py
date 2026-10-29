from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from audiobot.audio import (
    AUDACITY_BASS_HZ,
    AUDACITY_TREBLE_HZ,
    LOW_HZ_SAMPLE_RATE,
    AudioProbeResult,
    AudioProcessingError,
    _audacity_shelf_coefficients,
    build_aeksets_filter,
    build_cut_filter,
    build_eight_mono_filter,
    build_fcut_filter,
    build_mix_filter,
    build_pitch_filter,
    build_speed_filter,
    build_tempo_filter,
    build_thirty_two_tracks_filter,
    build_volume_filter,
    build_stack_filter,
    process_aeksets,
    process_aekloudlowhz,
    process_cut,
    process_eight_mono,
    process_fcut,
    process_mix,
    process_pitch,
    process_probe,
    process_speed,
    process_tempo,
    process_volume,
    process_stack,
    process_thirty_two_tracks,
    _resample_to_wav,
    parse_pitch,
    parse_speed,
    parse_tempo,
    parse_volume_db,
    parse_stack_count,
    parse_time_value,
)


def _magnitude_db(
    coefficients: tuple[float, float, float, float, float, float],
    frequency_hz: float,
    sample_rate: float = 48000.0,
) -> float:
    b0, b1, b2, a0, a1, a2 = coefficients
    w = 2.0 * math.pi * frequency_hz / sample_rate
    z1 = complex(math.cos(w), -math.sin(w))
    z2 = z1 * z1
    numerator = b0 + b1 * z1 + b2 * z2
    denominator = a0 + a1 * z1 + a2 * z2
    return 20.0 * math.log10(abs(numerator / denominator))


def _make_wav(
    path: Path,
    channels: int = 2,
    frequency_hz: float = 440.0,
) -> None:
    sample_rate = 48000
    duration = 0.25
    frames = bytearray()
    for index in range(int(sample_rate * duration)):
        value = int(
            12000 * math.sin(2 * math.pi * frequency_hz * index / sample_rate)
        )
        frames.extend(struct.pack("<h", value) * channels)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)


def _make_wav_with_distinct_stereo_channels(path: Path) -> None:
    sample_rate = 48000
    frames = bytearray()
    for index in range(sample_rate // 4):
        left = int(12000 * math.sin(2 * math.pi * 220 * index / sample_rate))
        right = int(12000 * math.sin(2 * math.pi * 880 * index / sample_rate))
        frames.extend(struct.pack("<hh", left, right))

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)


class AudioEffectParsingTests(unittest.TestCase):
    def test_speed_accepts_ratio_and_percent(self):
        self.assertEqual(parse_speed("1.5"), 1.5)
        self.assertEqual(parse_speed("150%"), 1.5)

    def test_tempo_accepts_ratio_and_percent(self):
        self.assertEqual(parse_tempo("1.5x"), 1.5)
        self.assertEqual(parse_tempo("150%"), 1.5)

    def test_volume_accepts_db_in_supported_range(self):
        self.assertEqual(parse_volume_db("+6"), 6.0)
        self.assertEqual(parse_volume_db("-12.5"), -12.5)
        self.assertEqual(parse_volume_db("50"), 50.0)
        self.assertEqual(parse_volume_db("60"), 60.0)
        self.assertEqual(parse_volume_db("100"), 100.0)

    def test_stack_accepts_track_count_in_supported_range(self):
        self.assertEqual(parse_stack_count("8"), 8)

    def test_pitch_accepts_ratio_and_semitones(self):
        self.assertAlmostEqual(parse_pitch("1.5"), 1.5)
        self.assertAlmostEqual(parse_pitch("+2"), 2 ** (2 / 12))
        self.assertAlmostEqual(parse_pitch("-2st"), 2 ** (-2 / 12))

    def test_pitch_rejects_out_of_range_semitones(self):
        for value in ("+13", "-13st", "+1e1000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_pitch(value)

    def test_volume_and_stack_reject_out_of_range_values(self):
        for value in ("-31", "101", "nan"):
            with self.subTest(volume=value), self.assertRaises(ValueError):
                parse_volume_db(value)
        for value in ("0", "33", "1.5"):
            with self.subTest(stack=value), self.assertRaises(ValueError):
                parse_stack_count(value)


class TimeValueParsingTests(unittest.TestCase):
    def test_compact_time_is_minutes_and_seconds(self):
        self.assertEqual(parse_time_value("230"), 150)

    def test_colon_time_formats(self):
        self.assertEqual(parse_time_value("2:30"), 150)
        self.assertEqual(parse_time_value("1:02:03"), 3723)

    def test_short_values_are_seconds(self):
        self.assertEqual(parse_time_value("3"), 3)
        self.assertEqual(parse_time_value("3.5"), 3.5)

    def test_rejects_invalid_or_out_of_range_times(self):
        for value in ("", "0", "160", "1:60", "1:2:3:4", "-3", "inf"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_time_value(value)


class BuildAudioEffectFilterTests(unittest.TestCase):
    def test_filters_apply_requested_values(self):
        self.assertIn("asetrate=72000", build_speed_filter(1.5))
        self.assertIn("rubberband=pitch=1.5", build_pitch_filter(1.5))
        self.assertIn("atempo=1.5", build_tempo_filter(1.5))

    def test_volume_filter_applies_gain_and_tone_controls(self):
        audio_filter = build_volume_filter(6, 120, -3, 4)

        self.assertIn("highpass=f=120", audio_filter)
        self.assertEqual(audio_filter.count("biquad="), 2)
        self.assertIn("volume=6dB", audio_filter)
        self.assertNotIn("channel_layouts=mono", audio_filter)
        self.assertIn("channel_layouts=stereo", audio_filter)

    def test_stack_filter_duplicates_requested_track_count(self):
        audio_filter = build_stack_filter(8, 120, -3, 4)

        self.assertIn("highpass=f=120", audio_filter)
        self.assertIn("asplit=8", audio_filter)
        self.assertIn("amix=inputs=8:normalize=0", audio_filter)
        self.assertEqual(audio_filter.count("biquad="), 2)

    def test_tempo_chains_cover_supported_factor_range(self):
        self.assertEqual(build_tempo_filter(0.25).count("atempo=0.5"), 2)
        self.assertEqual(build_tempo_filter(4.0).count("atempo=2"), 2)

    def test_trim_filters_cut_end_or_start(self):
        self.assertIn("atrim=start=0:end=150", build_cut_filter(150))
        self.assertIn("atrim=start=3", build_fcut_filter(3))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
class ProcessAudioEffectTests(unittest.TestCase):
    def test_speed_pitch_and_tempo_create_audio_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            _make_wav(input_path)

            processors = (
                ("speed", process_speed),
                ("pitch", process_pitch),
                ("tempo", process_tempo),
            )
            for name, processor in processors:
                with self.subTest(effect=name):
                    output_path = Path(tmp) / f"{name}.ogg"
                    processor(input_path, output_path, 1.25)
                    self.assertTrue(output_path.is_file())
                    self.assertGreater(output_path.stat().st_size, 0)

    def test_volume_and_stack_create_audio_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            _make_wav(input_path)

            process_volume(input_path, Path(tmp) / "volume.ogg", 6, 120, -3, 4)
            process_stack(input_path, Path(tmp) / "stack.ogg", 4, 120, -3, 4)

            self.assertTrue((Path(tmp) / "volume.ogg").is_file())
            self.assertTrue((Path(tmp) / "stack.ogg").is_file())

    def test_volume_preserves_distinct_stereo_channels(self):
        ffmpeg = shutil.which("ffmpeg")
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "volume.ogg"
            _make_wav_with_distinct_stereo_channels(input_path)

            process_volume(input_path, output_path, 0)
            decoded = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(output_path),
                    "-f",
                    "s16le",
                    "-acodec",
                    "pcm_s16le",
                    "-",
                ],
                capture_output=True,
                check=True,
            ).stdout

            self.assertTrue(any(left != right for left, right in struct.iter_unpack("<hh", decoded)))

    def test_tempo_changes_duration(self):
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("ffprobe is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "tempo.ogg"
            _make_wav(input_path)

            process_tempo(input_path, output_path, 2.0)

            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(output_path),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
            self.assertAlmostEqual(float(proc.stdout.strip()), 0.125, delta=0.04)

    def test_effects_reject_out_of_range_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(input_path)

            for processor in (process_speed, process_pitch, process_tempo):
                with self.subTest(processor=processor.__name__):
                    with self.assertRaises(AudioProcessingError):
                        processor(input_path, output_path, 5.0)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg and ffprobe are not installed",
)
class ProcessProbeTests(unittest.TestCase):
    def test_returns_loudness_duration_and_horizontal_waveform(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            waveform_path = Path(tmp) / "waveform.png"
            _make_wav(input_path)

            result = process_probe(input_path, waveform_path)

            self.assertIsInstance(result, AudioProbeResult)
            self.assertAlmostEqual(result.duration_seconds, 0.25, delta=0.01)
            self.assertEqual(result.sample_rate, 48000)
            self.assertTrue(math.isfinite(result.integrated_lufs))
            self.assertTrue(math.isfinite(result.true_peak_dbfs))
            image = waveform_path.read_bytes()
            self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", image[16:24]), (640, 240))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
class ProcessTrimTests(unittest.TestCase):
    def _duration(self, path: Path) -> float:
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("ffprobe is not installed")
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            check=True,
            text=True,
        )
        return float(proc.stdout.strip())

    def test_cut_keeps_audio_until_requested_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "cut.ogg"
            _make_wav(input_path)

            process_cut(input_path, output_path, parse_time_value("0.10"))

            self.assertAlmostEqual(self._duration(output_path), 0.1, delta=0.04)

    def test_fcut_removes_requested_time_from_audio_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "fcut.ogg"
            _make_wav(input_path)

            process_fcut(input_path, output_path, parse_time_value("0.05"))

            self.assertAlmostEqual(self._duration(output_path), 0.2, delta=0.04)

    def test_trim_processors_reject_nonpositive_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(input_path)

            for processor in (process_cut, process_fcut):
                with self.subTest(processor=processor.__name__):
                    with self.assertRaises(AudioProcessingError):
                        processor(input_path, output_path, 0)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
class ProcessEightMonoTests(unittest.TestCase):
    def test_outputs_stereo_vorbis_at_48khz(self):
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("ffprobe is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(input_path)

            process_eight_mono(input_path, output_path)

            self.assertTrue(output_path.is_file())
            self.assertGreater(output_path.stat().st_size, 0)

            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name,sample_rate,channels",
                    "-of",
                    "default=nw=1",
                    str(output_path),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
            info = dict(
                line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
            )
            self.assertEqual(info["codec_name"], "vorbis")
            self.assertEqual(info["sample_rate"], "48000")
            self.assertEqual(info["channels"], "2")

    def test_rejects_non_audio_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.txt"
            output_path = Path(tmp) / "output.ogg"
            input_path.write_text("not audio", encoding="utf-8")

            with self.assertRaises(AudioProcessingError):
                process_eight_mono(input_path, output_path)

    def test_rejects_invalid_highpass(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(input_path)

            with self.assertRaises(AudioProcessingError):
                process_eight_mono(input_path, output_path, highpass_hz=9)

    def test_rejects_invalid_shelf_gain(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(input_path)

            with self.assertRaises(AudioProcessingError):
                process_eight_mono(input_path, output_path, bass_db=-31)

    def test_eight_mono_accepts_bass_and_treble(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(input_path)

            process_eight_mono(input_path, output_path, 120, -30, 15)

            self.assertTrue(output_path.is_file())
            self.assertGreater(output_path.stat().st_size, 0)

    def test_thirty_two_tracks_outputs_stereo_vorbis_at_48khz(self):
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("ffprobe is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(input_path)

            process_thirty_two_tracks(input_path, output_path)

            self.assertTrue(output_path.is_file())
            self.assertGreater(output_path.stat().st_size, 0)

            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name,sample_rate,channels",
                    "-of",
                    "default=nw=1",
                    str(output_path),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
            info = dict(
                line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
            )
            self.assertEqual(info["codec_name"], "vorbis")
            self.assertEqual(info["sample_rate"], "48000")
            self.assertEqual(info["channels"], "2")

    def test_thirty_two_tracks_rejects_invalid_highpass(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(input_path)

            with self.assertRaises(AudioProcessingError):
                process_thirty_two_tracks(input_path, output_path, highpass_hz=9)

    def test_thirty_two_tracks_accepts_bass_and_treble(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(input_path)

            process_thirty_two_tracks(input_path, output_path, 120, -30, 15)

            self.assertTrue(output_path.is_file())
            self.assertGreater(output_path.stat().st_size, 0)

    def test_aeksets_outputs_stereo_vorbis_at_48khz(self):
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("ffprobe is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(input_path)

            process_aeksets(input_path, output_path)

            self.assertTrue(output_path.is_file())
            self.assertGreater(output_path.stat().st_size, 0)

            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name,sample_rate,channels",
                    "-of",
                    "default=nw=1",
                    str(output_path),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
            info = dict(
                line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
            )
            self.assertEqual(info["codec_name"], "vorbis")
            self.assertEqual(info["sample_rate"], "48000")
            self.assertEqual(info["channels"], "2")

    def test_aekloudlowhz_uses_22050_hz_intermediate_and_exports_48000_hz(self):
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("ffprobe is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.wav"
            intermediate_path = Path(tmp) / "intermediate.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(input_path)

            _resample_to_wav(input_path, intermediate_path, LOW_HZ_SAMPLE_RATE)
            intermediate_probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=sample_rate",
                    "-of",
                    "default=nw=1:nk=1",
                    str(intermediate_path),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
            self.assertEqual(intermediate_probe.stdout.strip(), "22050")

            process_aekloudlowhz(input_path, output_path)
            output_probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=sample_rate,channels",
                    "-of",
                    "default=nw=1",
                    str(output_path),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
            info = dict(
                line.split("=", 1)
                for line in output_probe.stdout.splitlines()
                if "=" in line
            )
            self.assertEqual(info["sample_rate"], "48000")
            self.assertEqual(info["channels"], "2")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
class ProcessMixTests(unittest.TestCase):
    def test_concatenates_two_files_in_order(self):
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("ffprobe is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first.wav"
            second_path = Path(tmp) / "second.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(first_path, frequency_hz=220)
            _make_wav(second_path, frequency_hz=880)

            process_mix(first_path, second_path, output_path)

            self.assertTrue(output_path.is_file())
            self.assertGreater(output_path.stat().st_size, 0)

            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name,sample_rate,channels:format=duration",
                    "-of",
                    "default=nw=1",
                    str(output_path),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
            info = dict(
                line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
            )
            self.assertEqual(info["codec_name"], "vorbis")
            self.assertEqual(info["sample_rate"], "48000")
            self.assertEqual(info["channels"], "2")
            self.assertAlmostEqual(float(info["duration"]), 0.5, delta=0.15)

    def test_normalizes_different_channel_layouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first.wav"
            second_path = Path(tmp) / "second.wav"
            output_path = Path(tmp) / "output.ogg"
            _make_wav(first_path, channels=1)
            _make_wav(second_path, channels=2)

            process_mix(first_path, second_path, output_path)

            self.assertTrue(output_path.is_file())
            self.assertGreater(output_path.stat().st_size, 0)


class AudacityShelfCoefficientTests(unittest.TestCase):
    def test_bass_is_half_gain_at_250hz(self):
        coefficients = _audacity_shelf_coefficients(
            AUDACITY_BASS_HZ, -30.0, is_bass=True
        )
        self.assertAlmostEqual(
            _magnitude_db(coefficients, AUDACITY_BASS_HZ), -15.0, places=6
        )

    def test_treble_is_half_gain_at_4000hz(self):
        coefficients = _audacity_shelf_coefficients(
            AUDACITY_TREBLE_HZ, -30.0, is_bass=False
        )
        self.assertAlmostEqual(
            _magnitude_db(coefficients, AUDACITY_TREBLE_HZ), -15.0, places=6
        )


class BuildEightMonoFilterTests(unittest.TestCase):
    def test_resamples_to_48khz(self):
        self.assertIn("aresample=48000,", build_eight_mono_filter())

    def test_highpass_is_added_when_requested(self):
        self.assertIn("highpass=f=120,", build_eight_mono_filter(120))

    def test_highpass_is_omitted_by_default(self):
        self.assertNotIn("highpass", build_eight_mono_filter())

    def test_bass_and_treble_are_added_when_requested(self):
        audio_filter = build_eight_mono_filter(120, -30, 15)

        self.assertIn("highpass=f=120,", audio_filter)
        self.assertEqual(audio_filter.count("biquad="), 2)


class BuildThirtyTwoTracksFilterTests(unittest.TestCase):
    def test_resamples_to_48khz(self):
        self.assertIn("aresample=48000,", build_thirty_two_tracks_filter())

    def test_contains_32_track_chain(self):
        audio_filter = build_thirty_two_tracks_filter()

        self.assertIn("asplit=32", audio_filter)
        self.assertIn("amix=inputs=32:normalize=0", audio_filter)
        self.assertIn("volume=32", audio_filter)
        self.assertIn("[mono]pan=stereo", audio_filter)

    def test_highpass_is_added_when_requested(self):
        self.assertIn("highpass=f=120,", build_thirty_two_tracks_filter(120))

    def test_highpass_is_omitted_by_default(self):
        self.assertNotIn("highpass", build_thirty_two_tracks_filter())

    def test_bass_and_treble_are_added_when_requested(self):
        audio_filter = build_thirty_two_tracks_filter(120, -30, 15)

        self.assertIn("highpass=f=120,", audio_filter)
        self.assertEqual(audio_filter.count("biquad="), 2)


class BuildMixFilterTests(unittest.TestCase):
    def test_concatenates_two_inputs(self):
        mix_filter = build_mix_filter()

        self.assertIn("[0:a]", mix_filter)
        self.assertIn("[1:a]", mix_filter)
        self.assertIn("concat=n=2:v=0:a=1", mix_filter)
        self.assertIn("aresample=48000", mix_filter)
        self.assertIn("[stereo]", mix_filter)


class BuildAeksetsFilterTests(unittest.TestCase):
    def test_contains_requested_processing_chain(self):
        audio_filter = build_aeksets_filter()

        self.assertIn("aresample=48000", audio_filter)
        self.assertIn("asplit=2", audio_filter)
        self.assertEqual(audio_filter.count("biquad="), 4)
        self.assertIn("volume=15dB", audio_filter)
        self.assertIn("amix=inputs=2:normalize=0", audio_filter)
        self.assertIn("asplit=32", audio_filter)
        self.assertIn("amix=inputs=32:normalize=0", audio_filter)
        self.assertIn("[mono]pan=stereo", audio_filter)


if __name__ == "__main__":
    unittest.main()
