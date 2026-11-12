import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
import wave
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nova_mic_switch import media_probe
from PySide6 import QtMultimedia


class MediaProbeTests(unittest.TestCase):
    class Signal:
        def connect(self, callback):
            self.callback = callback

        def disconnect(self, callback):
            if self.callback is callback:
                self.callback = None

        def emit(self, *args):
            if self.callback is None:
                return
            self.callback(*args)

    class FakeLoop:
        def __init__(self):
            self.quit_called = False

        def quit(self):
            self.quit_called = True

        def exec(self):
            if not self.quit_called:
                raise AssertionError("Fake loop must be ended by the media result or deadline")

    class FakeTimer:
        def setSingleShot(self, value):
            self.single_shot = value

        def start(self, milliseconds):
            self.milliseconds = milliseconds
            if self.timeout_on_start:
                self.timeout.callback()

        def stop(self):
            pass

    class FakePlayer:
        def __init__(self, outcome):
            self.mediaStatusChanged = MediaProbeTests.Signal()
            self.errorOccurred = MediaProbeTests.Signal()
            self.outcome = outcome
            self.source = None
            self.status = "NoMedia"

        def setSource(self, source):
            self.source = source
            kind, *payload = self.outcome
            if kind == "loaded":
                self.status = "LoadedMedia"
                self.mediaStatusChanged.emit(self.status)
            elif kind == "invalid":
                self.status = "InvalidMedia"
                self.mediaStatusChanged.emit(self.status)
            elif kind == "error":
                self.errorOccurred.emit("ResourceError", payload[0])

        def mediaStatus(self):
            return self.status

    def fake_qt(self, player, *, timeout=False):
        loop = self.FakeLoop()
        timer = self.FakeTimer()
        timer.timeout = self.Signal()
        timer.timeout_on_start = timeout

        class Core:
            QEventLoop = lambda: loop
            QTimer = lambda: timer
            QUrl = type("URL", (), {"fromLocalFile": staticmethod(lambda path: path)})

        class MediaStatus:
            LoadedMedia = "LoadedMedia"
            InvalidMedia = "InvalidMedia"

        class Multimedia:
            QMediaPlayer = type("QMediaPlayer", (), {"MediaStatus": MediaStatus})
            QMediaFormat = QtMultimedia.QMediaFormat

        return Core, Multimedia

    def test_writes_valid_silent_pcm_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "silent.wav"
            media_probe._write_silent_wav(path)
            with wave.open(str(path), "rb") as sample:
                self.assertEqual((sample.getnchannels(), sample.getsampwidth(), sample.getframerate()), (1, 2, 8000))
                self.assertEqual(sample.getnframes(), 800)
                self.assertEqual(sample.readframes(800), b"\0\0" * 800)

    def test_async_media_loaded_invalid_error_and_deadline_results(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "silent.wav"
            media_probe._write_silent_wav(source)
            for outcome, expected in ((('loaded',), "LoadedMedia"), (('invalid',), "InvalidMedia")):
                player = self.FakePlayer(outcome)
                core, multimedia = self.fake_qt(player)
                state = media_probe._load_media(player, core, multimedia, source, 12000)
                self.assertEqual(state["status"], expected)
                self.assertEqual(player.source, str(source))
            player = self.FakePlayer(('error', 'decoder failed'))
            core, multimedia = self.fake_qt(player)
            state = media_probe._load_media(player, core, multimedia, source, 12000)
            self.assertEqual(state["error"], {"code": "RESOURCEERROR", "message": "decoder failed"})
            player = self.FakePlayer(('pending',))
            core, multimedia = self.fake_qt(player, timeout=True)
            state = media_probe._load_media(player, core, multimedia, source, 12000)
            self.assertTrue(state["timed_out"])
            self.assertEqual(media_probe.DEADLINE_SECONDS * 1000, 12000)

    def test_wave_decode_capability_uses_real_qt_wave_enum_constants(self):
        wave_codec = QtMultimedia.QMediaFormat.AudioCodec.Wave
        wave_format = QtMultimedia.QMediaFormat.FileFormat.Wave
        self.assertEqual(media_probe._wave_capabilities(QtMultimedia, [wave_codec], [wave_format]), (True, True))
        self.assertEqual(media_probe._wave_capabilities(QtMultimedia, [], [wave_format]), (False, True))
        self.assertEqual(media_probe._wave_capabilities(QtMultimedia, [wave_codec], []), (True, False))

    def test_deadline_is_bounded_and_output_is_machine_readable_on_import_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "probe.json"
            with patch.dict(os.environ, {"QT_MEDIA_BACKEND": "other", "QT_QPA_PLATFORM": "xcb"}), \
                 patch.dict(sys.modules, {"PySide6": None}):
                result = media_probe.main(["--output-json", str(output)])
                self.assertEqual(os.environ["QT_MEDIA_BACKEND"], "ffmpeg")
                self.assertEqual(os.environ["QT_QPA_PLATFORM"], "offscreen")
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result, 1)
            self.assertFalse(report["success"])
            self.assertIn("ModuleNotFoundError", report["errors"][0])
            self.assertEqual(media_probe.DEADLINE_SECONDS, 12)

    def test_invalid_cli_does_not_accept_missing_output(self):
        with self.assertRaises(SystemExit):
            media_probe._parse_args([])

    def test_frozen_entry_dispatch_is_lazy_and_probe_precedes_desktop_import(self):
        entry = ROOT / "packaging" / "frozen_entry.py"
        received = []
        probe = types.ModuleType("nova_mic_switch.media_probe")
        probe.main = lambda argv: received.append(argv) or 0
        with patch.dict(sys.modules, {"nova_mic_switch.media_probe": probe, "nova_mic_switch.desktop": None}), \
             patch.object(sys, "argv", [str(entry), "--media-probe", "--output-json", "probe.json"]):
            with self.assertRaisesRegex(SystemExit, "0"):
                runpy.run_path(str(entry), run_name="__main__")
            self.assertIsNone(sys.modules["nova_mic_switch.desktop"])
        self.assertEqual(received, [["--output-json", "probe.json"]])


if __name__ == "__main__":
    unittest.main()
