import struct
import tempfile
import unittest
import wave
from pathlib import Path
from enum import Enum

from nova_mic_switch.feedback import AudioFeedback, decode_output_id, encode_output_id, write_cue


class FeedbackToneTests(unittest.TestCase):
    def test_output_ids_round_trip_exact_bytes(self):
        raw = b"usb\x00device\xff"
        self.assertEqual(decode_output_id(encode_output_id(raw)), raw)

    def test_invalid_output_id_is_rejected(self):
        with self.assertRaises(ValueError):
            decode_output_id("not base64!")

    def test_tones_are_valid_quiet_pcm_and_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            tones = {}
            for name in ("headset", "desktop", "error", "test"):
                path = write_cue(Path(directory) / f"{name}.wav", name)
                with wave.open(str(path), "rb") as wav:
                    self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 22050))
                    samples = wav.readframes(wav.getnframes())
                    values = struct.unpack(f"<{len(samples) // 2}h", samples)
                    self.assertGreater(max(map(abs, values)), 0)
                    self.assertLessEqual(max(map(abs, values)), 5244)
                    tones[name] = samples
            self.assertNotEqual(tones["headset"], tones["desktop"])
            self.assertNotEqual(tones["error"], tones["test"])

    def test_unknown_cue_does_not_write_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "bad.wav"
            with self.assertRaises(ValueError):
                write_cue(target, "unknown")
            self.assertFalse(target.exists())


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def disconnect(self, callback):
        self.callbacks.remove(callback)

    def emit(self):
        for callback in list(self.callbacks):
            callback()


class FakeDevice:
    def __init__(self, device_id):
        self.device_id = device_id

    def id(self):
        return self.device_id


class FakeDevices:
    def __init__(self, outputs):
        self.outputs = outputs
        self.audioOutputsChanged = Signal()

    def audioOutputs(self):
        return self.outputs


class FakeEffect:
    class Status(Enum):
        Null = 0
        Loading = 1
        Ready = 2
        Error = 3

    def __init__(self, _owner):
        self.statusChanged = Signal()
        self.current_status = self.Status.Loading
        self.device = None
        self.volume = None
        self.source = None
        self.play_count = 0
        self.stop_count = 0
        self.deleted = False

    def setAudioDevice(self, device):
        self.device = device

    def setVolume(self, volume):
        self.volume = volume

    def setSource(self, source):
        self.source = source

    def status(self):
        return self.current_status

    def play(self):
        self.play_count += 1

    def stop(self):
        self.stop_count += 1

    def deleteLater(self):
        self.deleted = True

    def set_status(self, status):
        self.current_status = status
        self.statusChanged.emit()


class AudioFeedbackLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.device = FakeDevice(b"selected-headset")
        self.other = FakeDevice(b"system-default")
        self.devices = FakeDevices([self.device, self.other])
        self.warnings = []
        self.effects = []

        def factory(owner):
            effect = FakeEffect(owner)
            self.effects.append(effect)
            return effect

        self.feedback = AudioFeedback(
            Path(self.temp.name), output_id=encode_output_id(self.device.id()),
            warning=self.warnings.append, devices=self.devices, sound_effect_factory=factory,
        )

    def tearDown(self):
        self.feedback.close()
        self.temp.cleanup()

    def test_preloaded_ready_effect_plays_repeatedly_on_selected_output(self):
        effect = self.feedback._effects["headset"]
        self.assertIs(effect.device, self.device)
        effect.set_status(FakeEffect.Status.Ready)
        self.assertTrue(self.feedback.play("headset"))
        self.assertTrue(self.feedback.play("headset"))
        self.assertEqual(effect.play_count, 2)

    def test_loading_and_error_effects_never_claim_success(self):
        effect = self.feedback._effects["test"]
        self.assertFalse(self.feedback.play("test"))
        self.assertEqual(effect.play_count, 0)
        self.assertTrue(any("still loading" in message for message in self.warnings))
        effect.set_status(FakeEffect.Status.Error)
        self.assertTrue(any("could not be loaded" in message for message in self.warnings))
        self.assertFalse(self.feedback.play("test"))

    def test_disabled_dry_run_and_unknown_route_are_silent(self):
        effect = self.feedback._effects["headset"]
        effect.set_status(FakeEffect.Status.Ready)
        self.feedback.set_enabled(False)
        self.assertFalse(self.feedback.play("headset"))
        self.assertFalse(self.feedback.route_succeeded(
            {"type": "switch", "applied": True, "dry_run": True, "target_source": "headset"},
            desktop_source="desktop", headset_source="headset",
        ))
        self.assertFalse(self.feedback.route_succeeded(
            {"type": "switch", "applied": True, "target_source": "other"},
            desktop_source="desktop", headset_source="headset",
        ))
        self.assertEqual(effect.play_count, 0)

    def test_verified_routes_play_only_matching_cues_and_test_is_explicit(self):
        headset = self.feedback._effects["headset"]
        desktop = self.feedback._effects["desktop"]
        test = self.feedback._effects["test"]
        for effect in (headset, desktop, test):
            effect.set_status(FakeEffect.Status.Ready)
        self.assertTrue(self.feedback.route_succeeded(
            {"type": "switch", "applied": True, "target_source": "headset"},
            desktop_source="desktop", headset_source="headset",
        ))
        self.assertEqual((headset.play_count, desktop.play_count, test.play_count), (1, 0, 0))
        self.assertTrue(self.feedback.play("test"))
        self.assertEqual((headset.play_count, desktop.play_count, test.play_count), (1, 0, 1))

    def test_removed_output_disposes_effects_and_reappearance_rebinds(self):
        old_effects = list(self.feedback._effects.values())
        self.devices.outputs = [self.other]
        self.devices.audioOutputsChanged.emit()
        self.assertFalse(self.feedback._effects)
        self.assertTrue(all(effect.deleted and effect.stop_count == 1 for effect in old_effects))
        self.assertFalse(self.feedback.play("test"))
        replacement = FakeDevice(self.device.id())
        self.devices.outputs = [replacement, self.other]
        self.devices.audioOutputsChanged.emit()
        self.assertIsNot(self.feedback._device, self.device)
        self.assertIs(self.feedback._effects["test"].device, replacement)

    def test_close_stops_and_disposes_every_preloaded_effect(self):
        effects = list(self.feedback._effects.values())
        self.feedback.close()
        self.assertFalse(self.feedback._effects)
        self.assertTrue(all(effect.deleted and effect.stop_count == 1 for effect in effects))


if __name__ == "__main__":
    unittest.main()
