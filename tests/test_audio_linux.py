import json
import subprocess
import unittest

from nova_mic_switch.audio_common import AudioError
from nova_mic_switch.audio_linux import LinuxAudioRouter


SOLOCAST = "alsa_input.usb-Kingston_HyperX_SoloCast-00.analog-stereo"
NOVA = "alsa_input.usb-SteelSeries_SteelSeries_Arctis_Nova_5-00.mono-fallback"
ARBITRARY_MIC = "alsa_input.usb-Example_Conference_Mic-00.analog-stereo"


def source_rows():
    return [
        {"index": 10, "name": SOLOCAST, "properties": {"device.description": "SoloCast"}},
        {"index": 11, "name": NOVA, "properties": {"device.description": "Nova 5P"}},
        {"index": 12, "name": "alsa_output.monitor", "properties": {"device.class": "monitor"}},
        {"index": 13, "name": "hidden.monitor.device", "properties": {"device.class": "monitor"}},
    ]


def stream(index, source, application="Discord"):
    return {"index": index, "source": source,
            "properties": {"application.name": application}}


class PactlRunner:
    def __init__(self, streams=None, default=SOLOCAST):
        self.sources = source_rows()
        self.streams = list(streams or [])
        self.default = default
        self.calls = []
        self.fail_move_index = None
        self.stream_snapshots = []
        self.default_readbacks = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        self.assertions(args, kwargs)
        if args == ["pactl", "--format=json", "list", "sources"]:
            stdout = json.dumps(self.sources)
        elif args == ["pactl", "--format=json", "list", "source-outputs"]:
            if self.stream_snapshots:
                stdout = json.dumps(self.stream_snapshots.pop(0))
            else:
                stdout = json.dumps(self.streams)
        elif args == ["pactl", "get-default-source"]:
            stdout = self.default_readbacks.pop(0) if self.default_readbacks else self.default
        elif args[:2] == ["pactl", "set-default-source"]:
            self.default = args[2]
            stdout = ""
        elif args[:2] == ["pactl", "move-source-output"]:
            index, target = int(args[2]), args[3]
            if index == self.fail_move_index:
                raise subprocess.CalledProcessError(1, args, stderr="move rejected")
            for item in self.streams:
                if item["index"] == index:
                    item["source"] = next(row["index"] for row in self.sources if row["name"] == target)
            stdout = ""
        else:
            raise AssertionError(f"unexpected command: {args}")
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    @staticmethod
    def assertions(args, kwargs):
        assert isinstance(args, list)
        assert "shell" not in kwargs
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 5


class LinuxAudioRouterTests(unittest.TestCase):
    def test_list_sources_filters_monitors_and_marks_system_default(self):
        runner = PactlRunner(default=NOVA)
        sources = LinuxAudioRouter(runner=runner).list_sources()
        self.assertEqual([(s.id, s.name, s.is_default) for s in sources], [
            (SOLOCAST, "SoloCast", False),
            (NOVA, "Nova 5P", True),
        ])
        self.assertEqual(runner.calls[0][0], ["pactl", "--format=json", "list", "sources"])
        self.assertEqual(runner.calls[1][0], ["pactl", "get-default-source"])

    def test_invalid_source_json_and_schema_raise_audio_error(self):
        for payload in ("not json", "{}", '[{"index":1}]'):
            with self.subTest(payload=payload):
                runner = PactlRunner()

                def broken(args, **kwargs):
                    if args == ["pactl", "--format=json", "list", "sources"]:
                        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")
                    return runner(args, **kwargs)

                with self.assertRaises(AudioError):
                    LinuxAudioRouter(runner=broken).list_sources()

    def test_missing_pactl_and_command_timeout_are_actionable(self):
        for error, message in ((FileNotFoundError("pactl"), "pactl was not found"),
                               (subprocess.TimeoutExpired(["pactl"], 5), "timed out")):
            with self.subTest(error=error):
                def broken_runner(args, **kwargs):
                    self.assertFalse(kwargs.get("shell", False))
                    self.assertEqual(kwargs["timeout"], 5)
                    raise error

                with self.assertRaisesRegex(AudioError, message):
                    LinuxAudioRouter(runner=broken_runner).list_sources()

    def test_discord_current_ignores_unrelated_apps_and_matches_exact_identity(self):
        runner = PactlRunner(streams=[
            stream(1, 10, "Discord"),
            stream(2, 11, "notdiscord"),
            {"index": 3, "source": 10,
             "properties": {"application.process.binary": "/usr/bin/discordcanary"}},
            stream(4, 10, "Firefox"),
        ])
        self.assertEqual(LinuxAudioRouter(scope="discord", runner=runner).current_source(), SOLOCAST)

    def test_discord_no_capture_and_mixed_sources_fail(self):
        with self.assertRaisesRegex(AudioError, "start Discord voice capture"):
            LinuxAudioRouter(runner=PactlRunner(streams=[stream(9, 10, "Browser")])).current_source()
        mixed = PactlRunner(streams=[stream(1, 10), stream(2, 11)])
        with self.assertRaisesRegex(AudioError, "mixed sources"):
            LinuxAudioRouter(runner=mixed).current_source()

    def test_discord_route_moves_only_discord_streams_then_verifies(self):
        runner = PactlRunner(streams=[
            stream(1, 10, "Discord"),
            stream(2, 10, "Firefox"),
            stream(3, 10, "Discord PTB"),
        ])
        result = LinuxAudioRouter(scope="discord", runner=runner).select_source(NOVA)
        commands = [call[0] for call in runner.calls]
        moves = [command for command in commands if command[:2] == ["pactl", "move-source-output"]]
        self.assertEqual(moves, [
            ["pactl", "move-source-output", "1", NOVA],
            ["pactl", "move-source-output", "3", NOVA],
        ])
        self.assertEqual(result, {"scope": "discord", "source_id": NOVA,
                                  "moved_streams": [1, 3], "verified": True})
        self.assertEqual(runner.streams[1]["source"], 10)

    def test_discord_route_reports_stream_disappearance_after_move(self):
        runner = PactlRunner(streams=[stream(1, 10)])
        runner.stream_snapshots = [[stream(1, 11)], []]
        with self.assertRaisesRegex(AudioError, "disappeared or could not be re-read"):
            LinuxAudioRouter(scope="discord", runner=runner).select_source(NOVA)

    def test_discord_route_reports_stream_recreation_during_readback(self):
        runner = PactlRunner(streams=[stream(1, 10)])
        runner.stream_snapshots = [[stream(1, 11)], [stream(8, 11)]]
        with self.assertRaisesRegex(AudioError, r"disappeared=\[1\], appeared=\[8\]"):
            LinuxAudioRouter(scope="discord", runner=runner).select_source(NOVA)

    def test_discord_partial_move_error_is_reported_without_retry(self):
        runner = PactlRunner(streams=[stream(1, 10), stream(2, 10)])
        runner.fail_move_index = 2
        with self.assertRaisesRegex(AudioError, "1/2 successes"):
            LinuxAudioRouter(scope="discord", runner=runner).select_source(NOVA)
        moves = [call[0] for call in runner.calls if call[0][:2] == ["pactl", "move-source-output"]]
        self.assertEqual(len(moves), 2)

    def test_system_route_sets_default_and_requires_readback(self):
        runner = PactlRunner(default=SOLOCAST)
        result = LinuxAudioRouter(scope="system", runner=runner).select_source(NOVA)
        self.assertEqual(result, {"scope": "system", "source_id": NOVA, "verified": True})
        self.assertIn(["pactl", "set-default-source", NOVA], [call[0] for call in runner.calls])

        mismatch = PactlRunner(default=SOLOCAST)
        mismatch.default_readbacks = [SOLOCAST, SOLOCAST]
        with self.assertRaisesRegex(AudioError, "readback mismatch"):
            LinuxAudioRouter(scope="system", runner=mismatch).select_source(NOVA)

    def test_any_enumerated_active_microphone_can_be_selected(self):
        runner = PactlRunner(default=SOLOCAST)
        runner.sources.append({"index": 14, "name": ARBITRARY_MIC,
                               "properties": {"device.description": "Conference Mic"}})
        result = LinuxAudioRouter(scope="system", runner=runner).select_source(ARBITRARY_MIC)
        self.assertEqual(result, {"scope": "system", "source_id": ARBITRARY_MIC, "verified": True})
        self.assertIn(["pactl", "set-default-source", ARBITRARY_MIC], [call[0] for call in runner.calls])

        discord = PactlRunner(streams=[stream(21, 10)])
        discord.sources.append({"index": 14, "name": ARBITRARY_MIC,
                                "properties": {"device.description": "Conference Mic"}})
        result = LinuxAudioRouter(scope="discord", runner=discord).select_source(ARBITRARY_MIC)
        self.assertEqual(result["source_id"], ARBITRARY_MIC)
        self.assertEqual(result["verified"], True)
        self.assertIn(["pactl", "move-source-output", "21", ARBITRARY_MIC],
                      [call[0] for call in discord.calls])

    def test_missing_or_monitor_target_never_mutates(self):
        runner = PactlRunner()
        with self.assertRaisesRegex(AudioError, "active capture source"):
            LinuxAudioRouter(runner=runner).select_source("random-input")
        self.assertTrue(runner.calls)
        self.assertFalse(any("set-default-source" in call[0] for call in runner.calls))
        self.assertFalse(any("move-source-output" in call[0] for call in runner.calls))

        for monitor_target in ("alsa_output.monitor", "hidden.monitor.device"):
            with self.subTest(monitor_target=monitor_target):
                monitor_runner = PactlRunner()
                with self.assertRaisesRegex(AudioError, "active capture source"):
                    LinuxAudioRouter(scope="system", runner=monitor_runner).select_source(monitor_target)
                self.assertFalse(any("set-default-source" in call[0] for call in monitor_runner.calls))
                self.assertFalse(any("move-source-output" in call[0] for call in monitor_runner.calls))

        absent = PactlRunner()
        absent.sources = [source_rows()[0]]
        with self.assertRaisesRegex(AudioError, "active capture source"):
            LinuxAudioRouter(runner=absent).select_source(NOVA)
        self.assertFalse(any("set-default-source" in call[0] for call in absent.calls))
        self.assertFalse(any("move-source-output" in call[0] for call in absent.calls))


if __name__ == "__main__":
    unittest.main()
