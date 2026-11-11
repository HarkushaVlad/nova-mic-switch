import contextlib
import io
import json
import unittest
from unittest.mock import patch

from nova_mic_switch import cli
from nova_mic_switch.audio_common import AudioSource
from nova_mic_switch.cli import main
from nova_mic_switch.device import EXPECTED_INTERFACES, PRODUCT_ID, VENDOR_ID


DESKTOP = "desktop-source"
HEADSET = "headset-source"


def media_row(path=b"media"):
    usage = EXPECTED_INTERFACES["media"]
    return {"vendor_id": VENDOR_ID, "product_id": PRODUCT_ID,
            "usage_page": usage["usage_page"], "usage": usage["usage"],
            "interface_number": usage["interface_number"], "path": path}


class FakeHidHandle:
    def __init__(self, reports):
        self.reports = list(reports)
        self.opened = []
        self.closed = False
        self.writes = []
        self.read_calls = []

    def open_path(self, path):
        self.opened.append(path)

    def read(self, length, timeout):
        self.read_calls.append((length, timeout))
        return self.reports.pop(0) if self.reports else []

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def close(self):
        self.closed = True


class FakeHid:
    def __init__(self, handle):
        self.handle = handle
        self.device_calls = 0

    def enumerate(self, vid, pid):
        return [media_row()]

    def device(self):
        self.device_calls += 1
        return self.handle


class FakeRouter:
    scope = "discord"

    def __init__(self, current=DESKTOP, sources=None):
        self.current = current
        self.sources = sources or [AudioSource(DESKTOP, "Desktop"), AudioSource(HEADSET, "Headset")]
        self.selected = []
        self.current_calls = 0

    def list_sources(self):
        return self.sources

    def current_source(self):
        self.current_calls += 1
        return self.current

    def select_source(self, source_id):
        self.selected.append(source_id)
        self.current = source_id
        return {"scope": self.scope, "source_id": source_id, "verified": True}


class AudioCliTests(unittest.TestCase):
    def test_audio_list_is_read_only_and_does_not_load_hid(self):
        router = FakeRouter()
        output = io.StringIO()
        with patch.object(cli, "_load_hid", side_effect=AssertionError("HID must not load")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(["audio-list"], audio_router=router), 0)
        sources = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item["id"] for item in sources], [DESKTOP, HEADSET])
        self.assertEqual(router.selected, [])

    def test_switch_defaults_to_dry_run_without_selection(self):
        router = FakeRouter()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["switch", "--source", HEADSET], audio_router=router), 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["scope"], "discord")
        self.assertEqual(record["current_source"], DESKTOP)
        self.assertEqual(record["target_source"], HEADSET)
        self.assertTrue(record["dry_run"])
        self.assertEqual(router.selected, [])

    def test_switch_apply_requires_explicit_flag_and_calls_adapter_once(self):
        router = FakeRouter()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["switch", "--source", HEADSET, "--apply"], audio_router=router), 0)
        self.assertEqual(router.selected, [HEADSET])
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[0]["event"], "routing_intent")
        self.assertEqual(records[1]["event"], "routing_result")
        self.assertTrue(records[1]["applied"])

    def test_conflicting_apply_and_dry_run_flags_are_rejected(self):
        for argv in (["switch", "--source", HEADSET, "--apply", "--dry-run"],
                     ["run", "--desktop-source", DESKTOP, "--headset-source", HEADSET,
                      "--duration", "1", "--apply", "--dry-run"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(list(argv), audio_router=FakeRouter(), backend=FakeHid(FakeHidHandle([])))

    def test_switch_rejects_unavailable_target_before_reading_current_or_mutating(self):
        router = FakeRouter(sources=[AudioSource(DESKTOP, "Desktop")])
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(main(["switch", "--source", HEADSET, "--apply"], audio_router=router), 2)
        self.assertIn("not currently available", error.getvalue())
        self.assertEqual(router.current_calls, 0)
        self.assertEqual(router.selected, [])

    def test_run_dry_run_toggles_only_on_selected_rising_edge_and_never_writes_hid(self):
        handle = FakeHidHandle([
            list(bytes.fromhex("00020000")),  # Non-selected event.
            list(bytes.fromhex("00010000")),  # Selected default previous-track.
            list(bytes.fromhex("00010000")),  # Duplicate held report.
            list(bytes.fromhex("00000000")),  # Release.
        ])
        backend = FakeHid(handle)
        router = FakeRouter(current=DESKTOP)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["run", "--desktop-source", DESKTOP, "--headset-source", HEADSET,
                                   "--duration", "0.01"], backend=backend, audio_router=router), 0)
        self.assertEqual(handle.opened, [b"media"])
        self.assertTrue(handle.closed)
        self.assertEqual(handle.writes, [])
        self.assertTrue(all(0 < timeout <= 1000 for _, timeout in handle.read_calls))
        self.assertEqual(router.selected, [])
        self.assertEqual(router.current_calls, 1)
        reports = [json.loads(line) for line in output.getvalue().splitlines()
                   if "data_hex" in json.loads(line)]
        self.assertEqual(len(reports), 4)
        self.assertNotIn("toggle_intent", reports[0])
        self.assertTrue(reports[1]["toggle_intent"]["dry_run"])
        self.assertEqual(reports[1]["toggle_intent"]["target_source"], HEADSET)
        self.assertNotIn("toggle_intent", reports[2])
        self.assertNotIn("toggle_intent", reports[3])

    def test_run_apply_only_calls_adapter_on_selected_edge_and_closes(self):
        handle = FakeHidHandle([list(bytes.fromhex("00010000"))])
        backend = FakeHid(handle)
        router = FakeRouter(current=HEADSET)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["run", "--desktop-source", DESKTOP, "--headset-source", HEADSET,
                                   "--duration", "0.01", "--apply"],
                                  backend=backend, audio_router=router), 0)
        self.assertEqual(router.selected, [DESKTOP])
        self.assertTrue(handle.closed)
        self.assertEqual(handle.writes, [])

    def test_run_rejects_identical_sources_before_opening_hid(self):
        handle = FakeHidHandle([])
        backend = FakeHid(handle)
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(main(["run", "--desktop-source", DESKTOP, "--headset-source", DESKTOP,
                                   "--duration", "1"], backend=backend, audio_router=FakeRouter()), 2)
        self.assertIn("distinct", error.getvalue())
        self.assertEqual(backend.device_calls, 0)

    def test_run_current_outside_pair_is_logged_without_selection(self):
        handle = FakeHidHandle([list(bytes.fromhex("00010000"))])
        router = FakeRouter(current="third-source")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["run", "--desktop-source", DESKTOP, "--headset-source", HEADSET,
                                   "--duration", "0.01", "--apply"],
                                  backend=FakeHid(handle), audio_router=router), 2)
        report = next(json.loads(line) for line in output.getvalue().splitlines()
                      if "data_hex" in json.loads(line))
        self.assertIn("outside the configured pair", report["toggle_intent"]["error"])
        self.assertEqual(router.selected, [])
        self.assertTrue(handle.closed)


if __name__ == "__main__":
    unittest.main()
