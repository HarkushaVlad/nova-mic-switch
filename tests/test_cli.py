import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nova_mic_switch import cli
from nova_mic_switch.cli import main
from nova_mic_switch.device import EXPECTED_INTERFACES, PRODUCT_ID, VENDOR_ID
from nova_mic_switch.media import MediaReportDecoder


def hid_row(interface: str, *, number=None, vid=VENDOR_ID, pid=PRODUCT_ID, path=b"/dev/hidraw-test"):
    usage = EXPECTED_INTERFACES[interface]
    return {
        "vendor_id": vid,
        "product_id": pid,
        "usage_page": usage["usage_page"],
        "usage": usage["usage"],
        "interface_number": usage["interface_number"] if number is None else number,
        "path": path,
    }


class FakeHandle:
    def __init__(self, read_values=None, write_values=None):
        self.read_values = list(read_values or [])
        self.write_values = list(write_values or [])
        self.read_calls = []
        self.write_calls = []
        self.opened = []
        self.closed = False
        self.open_error = None

    def open_path(self, path):
        self.opened.append(path)
        if self.open_error:
            raise self.open_error

    def read(self, size, timeout):
        self.read_calls.append((size, timeout))
        value = self.read_values.pop(0) if self.read_values else []
        if isinstance(value, Exception):
            raise value
        return value

    def write(self, data):
        self.write_calls.append(data)
        value = self.write_values.pop(0) if self.write_values else len(data)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class FakeHid:
    def __init__(self, rows, handle=None):
        self.rows = rows
        self.handle = handle or FakeHandle()
        self.enumerate_calls = []
        self.device_calls = 0

    def enumerate(self, vid, pid):
        self.enumerate_calls.append((vid, pid))
        return self.rows

    def device(self):
        self.device_calls += 1
        return self.handle


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class CliTests(unittest.TestCase):
    def test_media_decoder_uses_exact_documented_bit_positions(self):
        cases = {
            "01000000": ("volume-up",),
            "02000000": ("volume-down",),
            "00010000": ("previous-track",),
            "00020000": ("play-pause",),
            "00040000": ("next-track",),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(MediaReportDecoder().decode(bytes.fromhex(raw)), expected)

    def test_media_decoder_zero_release_duplicate_hold_and_multibit_order(self):
        decoder = MediaReportDecoder()
        self.assertEqual(decoder.decode(bytes.fromhex("00020000")), ("play-pause",))
        self.assertEqual(decoder.decode(bytes.fromhex("00020000")), ())
        self.assertEqual(decoder.decode(bytes.fromhex("00000000")), ())
        self.assertEqual(decoder.decode(bytes.fromhex("00020000")), ("play-pause",))
        self.assertEqual(decoder.decode(bytes.fromhex("00000000")), ())
        self.assertEqual(decoder.decode(bytes.fromhex("03070000")),
                         ("volume-up", "volume-down", "previous-track", "play-pause", "next-track"))

    def test_media_decoder_rejects_malformed_lengths_and_nonzero_padding(self):
        decoder = MediaReportDecoder()
        for raw in (b"", b"\x00" * 3, b"\x00" * 5,
                    bytes.fromhex("04000000"), bytes.fromhex("00000080")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decoder.decode(raw)

    def test_watch_dry_run_reports_only_selected_trigger_edges_without_writes(self):
        handle = FakeHandle(read_values=[
            list(bytes.fromhex("00020000")),  # Play/pause rising edge.
            list(bytes.fromhex("00020000")),  # Held duplicate.
            list(bytes.fromhex("00000000")),  # Release.
            list(bytes.fromhex("00040000")),  # Next track, not selected.
            list(bytes.fromhex("00010000")),  # Previous track, selected.
        ])
        hid = FakeHid([hid_row("status", path=b"status"),
                       hid_row("media", path=b"media"),
                       hid_row("vendor", path=b"vendor")], handle)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["watch", "--duration", "0.01", "--trigger", "previous-track",
                                   "--dry-run", "--label", "retracted"], backend=hid), 0)
        self.assertEqual(handle.opened, [b"media"])
        self.assertEqual(handle.write_calls, [])
        self.assertTrue(handle.read_calls)
        self.assertTrue(all(0 < timeout <= 1000 for _, timeout in handle.read_calls))
        self.assertTrue(handle.closed)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        reports = [record for record in records if "data_hex" in record]
        self.assertEqual(len(reports), 5)
        self.assertEqual(reports[0]["decoded_events"], ["play-pause"])
        self.assertFalse(reports[0]["dry_run_trigger_intent"])
        self.assertEqual(reports[1]["decoded_events"], [])
        self.assertFalse(reports[1]["dry_run_trigger_intent"])
        self.assertEqual(reports[2]["decoded_events"], [])
        self.assertEqual(reports[3]["decoded_events"], ["next-track"])
        self.assertFalse(reports[3]["dry_run_trigger_intent"])
        self.assertEqual(reports[4]["decoded_events"], ["previous-track"])
        self.assertTrue(reports[4]["dry_run_trigger_intent"])
        self.assertTrue(all(record["dry_run"] for record in reports))

    def test_watch_malformed_report_has_no_trigger_intent_and_closes(self):
        handle = FakeHandle(read_values=[[0, 2, 0]])
        hid = FakeHid([hid_row("media")], handle)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["watch", "--duration", "0.01", "--dry-run"], backend=hid), 0)
        report = next(json.loads(line) for line in output.getvalue().splitlines()
                      if "data_hex" in json.loads(line))
        self.assertIn("decode_error", report)
        self.assertEqual(report["decoded_events"], [])
        self.assertFalse(report["dry_run_trigger_intent"])
        self.assertTrue(handle.closed)

    def test_watch_requires_explicit_dry_run_flag(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            with self.assertRaises(SystemExit):
                main(["watch", "--duration", "1"], backend=FakeHid([]))
        self.assertIn("--dry-run", error.getvalue())

    def test_list_enumerates_exact_device_without_open_or_write(self):
        hid = FakeHid([hid_row("status"), hid_row("media")])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["list"], backend=hid), 0)
        self.assertEqual(hid.enumerate_calls, [(VENDOR_ID, PRODUCT_ID)])
        self.assertEqual(hid.device_calls, 0)
        self.assertEqual(hid.handle.write_calls, [])
        self.assertEqual(len(output.getvalue().splitlines()), 2)

    def test_list_filters_unrelated_devices(self):
        hid = FakeHid([hid_row("status", vid=1), hid_row("media", pid=2)])
        output = io.StringIO()
        error = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(main([], backend=hid), 0)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("No matching", error.getvalue())
        self.assertEqual(hid.device_calls, 0)

    def test_listen_reads_only_explicitly_selected_interface(self):
        handle = FakeHandle(read_values=[[1, 2, 255]])
        hid = FakeHid([hid_row("status"), hid_row("media")], handle)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["listen", "--interface", "media", "--duration", "0.01",
                                   "--label", "near"], backend=hid), 0)
        self.assertEqual(handle.opened, [b"/dev/hidraw-test"])
        self.assertEqual(handle.write_calls, [])
        self.assertTrue(all(timeout > 0 for _, timeout in handle.read_calls))
        self.assertTrue(handle.closed)
        entry = next(json.loads(line) for line in output.getvalue().splitlines()
                     if '"data_hex"' in line)
        self.assertEqual(entry["interface"], "media")
        self.assertEqual(entry["data_hex"], "0102ff")

    def test_listen_report_time_is_taken_after_receive(self):
        clock = FakeClock()
        handle = FakeHandle(read_values=[[7]])
        original_read = handle.read

        def delayed_read(size, timeout):
            result = original_read(size, timeout)
            clock.now += 0.2
            return result

        handle.read = delayed_read
        hid = FakeHid([hid_row("vendor")], handle)
        output = io.StringIO()
        with patch.object(cli.time, "monotonic", side_effect=clock.monotonic), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(["listen", "--interface", "vendor", "--duration", "0.1"],
                                  backend=hid), 0)
        report = next(json.loads(line) for line in output.getvalue().splitlines()
                      if "data_hex" in json.loads(line))
        self.assertEqual(report["elapsed_seconds"], 0.2)

    def test_probe_emits_only_exact_status_request(self):
        handle = FakeHandle(read_values=[[0, 1, 2]])
        hid = FakeHid([hid_row("status"), hid_row("media"), hid_row("vendor")], handle)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["probe", "--duration", "10", "--samples", "1",
                                   "--label", "extended"], backend=hid), 0)
        self.assertEqual(handle.opened, [b"/dev/hidraw-test"])
        self.assertEqual(handle.write_calls, [b"\x00\xb0"])
        self.assertTrue(handle.closed)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([record["event"] for record in records if "event" in record],
                         ["session_start", "session_end"])
        self.assertEqual(sum("data_hex" in record for record in records), 1)

    def test_probe_query_starts_follow_interval_without_catchup(self):
        clock = FakeClock()
        handle = FakeHandle(read_values=[[1], [2], [3]])
        hid = FakeHid([hid_row("status")], handle)
        output = io.StringIO()
        with patch.object(cli.time, "monotonic", side_effect=clock.monotonic), \
                patch.object(cli.time, "sleep", side_effect=clock.sleep), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(["probe", "--duration", "0.25", "--interval", "0.1"],
                                  backend=hid), 0)
        self.assertEqual(handle.write_calls, [b"\x00\xb0"] * 3)
        starts = [json.loads(line)["elapsed_seconds"] for line in output.getvalue().splitlines()
                  if json.loads(line).get("request_hex")]
        self.assertEqual(starts[0], 0.0)
        self.assertGreaterEqual(starts[1] - starts[0], 0.1)
        self.assertGreaterEqual(starts[2] - starts[1], 0.1)
        self.assertLessEqual(clock.now, 0.25)

    def test_wrong_interface_number_refuses_to_open_or_write(self):
        handle = FakeHandle()
        hid = FakeHid([hid_row("status", number=4)], handle)
        error = io.StringIO()
        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["probe", "--duration", "1"], backend=hid), 2)
        self.assertIn("unexpected interface number", error.getvalue())
        self.assertEqual(hid.device_calls, 0)
        self.assertEqual(handle.write_calls, [])

    def test_ambiguous_usage_refuses_open(self):
        hid = FakeHid([hid_row("status", path=b"one"), hid_row("status", path=b"two")])
        error = io.StringIO()
        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["listen", "--interface", "status", "--duration", "1"],
                                  backend=hid), 2)
        self.assertIn("ambiguous", error.getvalue())
        self.assertEqual(hid.device_calls, 0)

    def test_probe_short_write_stops_and_closes(self):
        handle = FakeHandle(write_values=[1])
        hid = FakeHid([hid_row("status")], handle)
        error = io.StringIO()
        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["probe", "--duration", "1"], backend=hid), 2)
        self.assertIn("short HID write", error.getvalue())
        self.assertEqual(handle.write_calls, [b"\x00\xb0"])
        self.assertTrue(handle.closed)

    def test_probe_timeout_is_finite_stops_without_retry_and_closes(self):
        handle = FakeHandle(read_values=[[]])
        hid = FakeHid([hid_row("status")], handle)
        output = io.StringIO()
        error = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(main(["probe", "--duration", "10", "--interval", "0.25"],
                                  backend=hid), 2)
        self.assertEqual(len(handle.read_calls), 1)
        self.assertGreater(handle.read_calls[0][1], 0)
        self.assertLessEqual(handle.read_calls[0][1], 250)
        self.assertEqual(handle.write_calls, [b"\x00\xb0"])
        self.assertTrue(handle.closed)
        self.assertIn("without retry", error.getvalue())
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[-1]["outcome"], "timeout")

    def test_read_error_stops_and_closes(self):
        handle = FakeHandle(read_values=[OSError("read failed")])
        hid = FakeHid([hid_row("status")], handle)
        error = io.StringIO()
        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["listen", "--interface", "status", "--duration", "0.01"],
                                  backend=hid), 2)
        self.assertIn("HID read failed", error.getvalue())
        self.assertTrue(handle.closed)

    def test_open_permission_error_is_actionable_and_closes_handle(self):
        handle = FakeHandle()
        handle.open_error = OSError("permission denied")
        hid = FakeHid([hid_row("status")], handle)
        error = io.StringIO()
        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["listen", "--interface", "status", "--duration", "1"],
                                  backend=hid), 2)
        self.assertIn("No privileged retry", error.getvalue())
        self.assertTrue(handle.closed)

    def test_output_jsonl_and_offline_analyze(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.jsonl"
            handle = FakeHandle(read_values=[[1, 2], [1, 3]])
            hid = FakeHid([hid_row("vendor")], handle)
            with contextlib.redirect_stdout(io.StringIO()):
                main(["listen", "--interface", "vendor", "--duration", "0.01",
                      "--label", "muted", "--output", str(capture)], backend=hid)
            lines = capture.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 4)
            self.assertEqual(json.loads(lines[0])["event"], "session_start")
            self.assertEqual(json.loads(lines[-1])["event"], "session_end")
            result = io.StringIO()
            with contextlib.redirect_stdout(result):
                self.assertEqual(main(["analyze", str(capture)]), 0)
            summary = json.loads(result.getvalue())
            self.assertEqual(summary["labels"]["muted"]["report_count"], 2)
            self.assertEqual(summary["labels"]["muted"]["distinct_byte_values_by_offset"], [[1], [2, 3]])
            self.assertIn("no semantic decoding", summary["interpretation"])

    def test_analyze_compares_raw_values_across_labels_and_separates_report_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "compare.jsonl"
            records = [
                {"mode": "listen", "interface": "vendor", "label": "retracted", "data_hex": "010203"},
                {"mode": "listen", "interface": "vendor", "label": "extended", "data_hex": "010403"},
                {"mode": "listen", "interface": "vendor", "label": "other-id", "data_hex": "020403"},
            ]
            capture.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["analyze", str(capture)]), 0)
            summaries = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(summaries), 2)
            group = next(item for item in summaries if item["report_id_first_byte"] == 1)
            self.assertEqual(group["cross_label_differing_offsets"], [
                {"offset": 1, "labels": {"extended": [4], "retracted": [2]}}
            ])

    def test_invalid_duration_and_interval_rejected_by_argparse(self):
        for argv in (["listen", "--interface", "status", "--duration", "0"],
                     ["probe", "--duration", "1", "--interval", "nan"],
                     ["probe", "--duration", "1", "--interval", "0.01"],
                     ["probe", "--duration", "inf"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(argv, backend=FakeHid([]))

    def test_analyze_malformed_input_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "bad.jsonl"
            capture.write_text("not json\n{\"data_hex\":\"xyz\"}\n", encoding="utf-8")
            error = io.StringIO()
            with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["analyze", str(capture)]), 0)
            self.assertIn("malformed_lines", error.getvalue())

    def test_linux_loader_requires_hidraw_without_hid_fallback(self):
        hidraw = FakeHid([hid_row("status")])
        with patch.object(cli.sys, "platform", "linux"), \
                patch.object(cli.importlib, "import_module", return_value=hidraw) as importer, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["list"]), 0)
        importer.assert_called_once_with("hidraw")

    def test_linux_loader_missing_hidraw_is_actionable(self):
        error = io.StringIO()
        with patch.object(cli.sys, "platform", "linux"), \
                patch.object(cli.importlib, "import_module", side_effect=ImportError("missing")), \
                contextlib.redirect_stderr(error):
            self.assertEqual(main(["list"]), 2)
        self.assertIn("Linux hidraw backend unavailable", error.getvalue())

    def test_windows_loader_uses_hid(self):
        hid = FakeHid([hid_row("status")])
        with patch.object(cli.sys, "platform", "win32"), \
                patch.object(cli.importlib, "import_module", return_value=hid) as importer, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["list"]), 0)
        importer.assert_called_once_with("hid")

    def test_output_file_errors_are_clean_cli_errors_and_handles_close(self):
        hid = FakeHid([hid_row("vendor")])
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(main(["listen", "--interface", "vendor", "--duration", "1",
                                   "--output", "/path/that/does/not/exist/capture.jsonl"],
                                  backend=hid), 2)
        self.assertIn("cannot open output file", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())
        self.assertEqual(hid.device_calls, 0)


if __name__ == "__main__":
    unittest.main()
