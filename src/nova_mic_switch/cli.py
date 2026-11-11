"""Safe, deliberately narrow Nova 5P HID diagnostic commands."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from .device import (
    EXPECTED_INTERFACES,
    PRODUCT_ID,
    STATUS_REQUEST,
    VENDOR_ID,
    DeviceError,
    HidBackend,
    choose_device,
    enumerate_devices,
    open_interface,
)
from .audio_common import AudioError
from .audio_controller import SourceToggleController
from .audio_factory import create_audio_router, default_scope
from .logging import JsonlWriter, make_record
from .media import MediaReportDecoder


def _positive_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not 0 < result < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return result


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _probe_interval(value: str) -> float:
    result = _positive_float(value)
    if result < 0.1:
        raise argparse.ArgumentTypeError("must be at least 0.1 seconds")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nova-mic",
        description="Bounded, raw-only diagnostics for SteelSeries Nova 5P USB HID.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="enumerate the exact target without opening it")

    listen = commands.add_parser("listen", help="passively read one selected HID interface")
    listen.add_argument("--interface", required=True, choices=tuple(EXPECTED_INTERFACES))
    listen.add_argument("--duration", type=_positive_float, required=True, help="seconds")
    listen.add_argument("--label", default="unlabeled")
    listen.add_argument("--output", type=Path, help="append raw JSONL records to this file")

    watch = commands.add_parser("watch", help="diagnose media-button reports; dry-run only")
    watch.add_argument("--trigger", choices=("play-pause", "next-track", "previous-track"),
                       default="previous-track")
    watch.add_argument("--duration", type=_positive_float, required=True, help="seconds")
    watch.add_argument("--label", default="unlabeled")
    watch.add_argument("--output", type=Path, help="append raw JSONL records to this file")
    watch.add_argument("--dry-run", action="store_true", required=True,
                       help="required; report trigger intents without taking action")

    probe = commands.add_parser("probe", help="send only the documented status query")
    probe.add_argument("--duration", type=_positive_float, required=True, help="seconds")
    probe.add_argument("--interval", type=_probe_interval, default=1.0,
                       help="seconds between query starts (minimum 0.1)")
    probe.add_argument("--samples", type=_positive_int, help="maximum number of queries")
    probe.add_argument("--label", default="unlabeled")
    probe.add_argument("--output", type=Path, help="append raw JSONL records to this file")

    analyze = commands.add_parser("analyze", help="summarize raw JSONL captures offline")
    analyze.add_argument("files", nargs="+", type=Path)

    commands.add_parser("audio-list", help="list available capture sources (read-only)")

    switch = commands.add_parser("switch", help="dry-run or explicitly apply one source selection")
    switch.add_argument("--source", required=True, help="exact source ID from audio-list")
    switch.add_argument("--scope", choices=("discord", "system"))
    switch_mode = switch.add_mutually_exclusive_group()
    switch_mode.add_argument("--apply", action="store_true", help="apply this audio route")
    switch_mode.add_argument("--dry-run", action="store_true", help="report intent only (default)")

    run = commands.add_parser("run", help="watch media controls and toggle a configured source pair")
    run.add_argument("--desktop-source", required=True, help="exact source ID from audio-list")
    run.add_argument("--headset-source", required=True, help="exact source ID from audio-list")
    run.add_argument("--trigger", choices=("previous-track", "next-track", "play-pause"),
                     default="previous-track")
    run.add_argument("--scope", choices=("discord", "system"))
    run.add_argument("--duration", type=_positive_float, required=True, help="maximum seconds")
    run.add_argument("--label", default="unlabeled")
    run.add_argument("--output", type=Path, help="append raw JSONL and routing records")
    run_mode = run.add_mutually_exclusive_group()
    run_mode.add_argument("--apply", action="store_true", help="apply routing on trigger (default is dry-run)")
    run_mode.add_argument("--dry-run", action="store_true", help="report routing intent only (default)")
    return parser


def _json_safe_path(path: Any) -> str | None:
    if path is None:
        return None
    if isinstance(path, bytes):
        return path.hex()
    return str(path)


def _enum_rows(devices: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for device in devices:
        if device.get("vendor_id") != VENDOR_ID or device.get("product_id") != PRODUCT_ID:
            continue
        rows.append({
            "vendor_id": f"{VENDOR_ID:04x}",
            "product_id": f"{PRODUCT_ID:04x}",
            "interface_number": device.get("interface_number"),
            "usage_page": device.get("usage_page"),
            "usage": device.get("usage"),
            "path": _json_safe_path(device.get("path")),
            "manufacturer_string": device.get("manufacturer_string"),
            "product_string": device.get("product_string"),
        })
    return rows


def command_list(backend: HidBackend) -> int:
    devices = enumerate_devices(backend)
    for item in _enum_rows(devices):
        print(json.dumps(item, ensure_ascii=True, sort_keys=True))
    if not _enum_rows(devices):
        print("No matching Nova 5P USB device (1038:2232) found.", file=sys.stderr)
    return 0


def _open_writer(path: Path | None) -> JsonlWriter:
    try:
        return JsonlWriter(path)
    except OSError as exc:
        raise DeviceError(f"cannot open output file: {exc}") from exc


def _emit(writer: JsonlWriter, record: dict[str, Any]) -> None:
    try:
        writer.write(record)
    except OSError as exc:
        raise DeviceError(f"cannot write output file: {exc}") from exc
    print(json.dumps(record, ensure_ascii=True, sort_keys=True))


def _session_record(mode: str, interface: str, metadata: dict[str, int], label: str,
                    event: str, started: float, *, report_count: int = 0,
                    outcome: str | None = None, query_count: int | None = None,
                    trigger_count: int | None = None) -> dict[str, Any]:
    record = make_record(mode, interface, metadata, label, time.monotonic() - started)
    record.update({"event": event, "report_count": report_count})
    if outcome is not None:
        record["outcome"] = outcome
    if query_count is not None:
        record["query_count"] = query_count
    if trigger_count is not None:
        record["trigger_count"] = trigger_count
    return record


def command_listen(args: argparse.Namespace, backend: HidBackend) -> int:
    selected = choose_device(enumerate_devices(backend), args.interface)
    metadata = EXPECTED_INTERFACES[args.interface]
    writer = _open_writer(args.output)
    start = time.monotonic()
    report_count = 0
    outcome = "completed"
    try:
        _emit(writer, _session_record("listen", args.interface, metadata, args.label,
                                      "session_start", start))
        device = open_interface(backend, selected, args.interface)
        try:
            while True:
                elapsed = time.monotonic() - start
                remaining = args.duration - elapsed
                if remaining <= 0:
                    break
                timeout_ms = max(1, min(1000, int(remaining * 1000)))
                try:
                    data = device.read(128, timeout_ms)
                except Exception as exc:
                    outcome = "error"
                    _emit(writer, make_record("listen", args.interface, metadata, args.label,
                                              time.monotonic() - start, error=f"read failed: {exc}"))
                    raise DeviceError(f"HID read failed: {exc}") from exc
                if data:
                    record = make_record("listen", args.interface, metadata, args.label,
                                         time.monotonic() - start, bytes(data))
                    _emit(writer, record)
                    report_count += 1
        finally:
            device.close()
    except DeviceError:
        outcome = "error"
        raise
    finally:
        try:
            _emit(writer, _session_record("listen", args.interface, metadata, args.label,
                                           "session_end", start, report_count=report_count,
                                           outcome=outcome))
        finally:
            writer.close()
    return 0


def command_watch(args: argparse.Namespace, backend: HidBackend) -> int:
    selected = choose_device(enumerate_devices(backend), "media")
    metadata = EXPECTED_INTERFACES["media"]
    writer = _open_writer(args.output)
    start = time.monotonic()
    report_count = 0
    outcome = "completed"
    decoder = MediaReportDecoder()
    try:
        _emit(writer, _session_record("watch", "media", metadata, args.label,
                                      "session_start", start))
        device = open_interface(backend, selected, "media")
        try:
            while True:
                remaining = args.duration - (time.monotonic() - start)
                if remaining <= 0:
                    break
                timeout_ms = max(1, min(1000, int(remaining * 1000)))
                try:
                    data = device.read(128, timeout_ms)
                except Exception as exc:
                    outcome = "error"
                    _emit(writer, make_record("watch", "media", metadata, args.label,
                                              time.monotonic() - start,
                                              error=f"read failed: {exc}"))
                    raise DeviceError(f"HID media read failed: {exc}") from exc
                if not data:
                    continue
                raw = bytes(data)
                record = make_record("watch", "media", metadata, args.label,
                                     time.monotonic() - start, raw)
                report_count += 1
                try:
                    events = decoder.decode(raw)
                except ValueError as exc:
                    record["decoded_events"] = []
                    record["decode_error"] = str(exc)
                    record["dry_run_trigger_intent"] = False
                else:
                    record["decoded_events"] = list(events)
                    record["dry_run_trigger_intent"] = args.trigger in events
                    if args.trigger in events:
                        record["trigger"] = args.trigger
                record["dry_run"] = True
                _emit(writer, record)
        finally:
            device.close()
    except DeviceError:
        outcome = "error"
        raise
    finally:
        try:
            _emit(writer, _session_record("watch", "media", metadata, args.label,
                                           "session_end", start, report_count=report_count,
                                           outcome=outcome))
        finally:
            writer.close()
    return 0


def command_audio_list(router: Any) -> int:
    for source in router.list_sources():
        print(json.dumps({"id": source.id, "name": source.name,
                          "is_default": source.is_default}, sort_keys=True))
    return 0


def _validate_audio_pair(router: Any, source_ids: Iterable[str]) -> None:
    sources = router.list_sources()
    present = {source.id for source in sources}
    for source_id in source_ids:
        if source_id not in present:
            raise AudioError(f"selected audio source is not currently available: {source_id}")


def command_switch(args: argparse.Namespace, router: Any) -> int:
    _validate_audio_pair(router, [args.source])
    current = router.current_source()
    record: dict[str, Any] = {
        "mode": "switch",
        "scope": getattr(router, "scope", args.scope or default_scope()),
        "current_source": current,
        "target_source": args.source,
        "dry_run": not args.apply,
        "apply_requested": args.apply,
    }
    record["event"] = "routing_intent"
    print(json.dumps(record, sort_keys=True))
    if not args.apply:
        print("Dry run only; pass --apply to change routing.", file=sys.stderr)
    else:
        result = router.select_source(args.source)
        print(json.dumps({"event": "routing_result", "scope": record["scope"],
                          "target_source": args.source, "applied": True,
                          "routing_result": result}, sort_keys=True))
    return 0


def command_run(args: argparse.Namespace, router: Any, backend: HidBackend) -> int:
    if args.desktop_source == args.headset_source:
        raise AudioError("desktop and headset source IDs must be distinct")
    _validate_audio_pair(router, [args.desktop_source, args.headset_source])
    selected = choose_device(enumerate_devices(backend), "media")
    metadata = EXPECTED_INTERFACES["media"]
    writer = _open_writer(args.output)
    start = time.monotonic()
    report_count = 0
    toggle_count = 0
    routing_errors = 0
    outcome = "completed"
    decoder = MediaReportDecoder()
    controller = SourceToggleController(
        router, args.desktop_source, args.headset_source, apply=args.apply
    )
    try:
        _emit(writer, _session_record("run", "media", metadata, args.label,
                                      "session_start", start))
        device = open_interface(backend, selected, "media")
        try:
            while True:
                remaining = args.duration - (time.monotonic() - start)
                if remaining <= 0:
                    break
                timeout_ms = max(1, min(1000, int(remaining * 1000)))
                try:
                    data = device.read(128, timeout_ms)
                except Exception as exc:
                    outcome = "error"
                    _emit(writer, make_record("run", "media", metadata, args.label,
                                              time.monotonic() - start,
                                              error=f"read failed: {exc}"))
                    raise DeviceError(f"HID media read failed: {exc}") from exc
                if not data:
                    continue
                raw = bytes(data)
                report = make_record("run", "media", metadata, args.label,
                                     time.monotonic() - start, raw)
                report_count += 1
                try:
                    events = decoder.decode(raw)
                except ValueError as exc:
                    report.update({"decoded_events": [], "decode_error": str(exc)})
                    _emit(writer, report)
                    continue
                report["decoded_events"] = list(events)
                if args.trigger in events:
                    toggle_count += 1
                    try:
                        result = controller.trigger()
                    except AudioError as exc:
                        routing_errors += 1
                        report["toggle_intent"] = {
                            "trigger": args.trigger,
                            "scope": args.scope or getattr(router, "scope", default_scope()),
                            "dry_run": not args.apply,
                            "error": str(exc),
                        }
                    else:
                        report["toggle_intent"] = {
                            "trigger": args.trigger,
                            "scope": args.scope or getattr(router, "scope", default_scope()),
                            **result,
                        }
                _emit(writer, report)
        except KeyboardInterrupt:
            outcome = "interrupted"
            raise
        finally:
            device.close()
    except DeviceError:
        outcome = "error"
        raise
    except AudioError:
        outcome = "error"
        raise
    finally:
        try:
            _emit(writer, _session_record("run", "media", metadata, args.label,
                                          "session_end", start, report_count=report_count,
                                          outcome=("routing_errors" if routing_errors and outcome == "completed" else outcome),
                                          trigger_count=toggle_count))
        finally:
            writer.close()
    return 2 if routing_errors else 0


def command_probe(args: argparse.Namespace, backend: HidBackend) -> int:
    selected = choose_device(enumerate_devices(backend), "status")
    metadata = EXPECTED_INTERFACES["status"]
    writer = _open_writer(args.output)
    start = time.monotonic()
    completed = 0
    report_count = 0
    outcome = "completed"
    try:
        _emit(writer, _session_record("probe", "status", metadata, args.label,
                                      "session_start", start))
        device = open_interface(backend, selected, "status")
        try:
            while True:
                query_started = time.monotonic()
                elapsed = query_started - start
                if elapsed >= args.duration or (args.samples is not None and completed >= args.samples):
                    break
                try:
                    written = device.write(STATUS_REQUEST)
                except Exception as exc:
                    outcome = "error"
                    _emit(writer, make_record("probe", "status", metadata, args.label,
                                              time.monotonic() - start, error=f"write failed: {exc}"))
                    raise DeviceError(f"HID status query write failed: {exc}") from exc
                if written != len(STATUS_REQUEST):
                    outcome = "error"
                    _emit(writer, make_record("probe", "status", metadata, args.label,
                                              time.monotonic() - start,
                                              error=f"short write: {written}/{len(STATUS_REQUEST)}"))
                    raise DeviceError(f"short HID write ({written}/{len(STATUS_REQUEST)} bytes); stopping")
                completed += 1
                record = make_record("probe", "status", metadata, args.label, elapsed,
                                     request=STATUS_REQUEST)
                _emit(writer, record)
                remaining = args.duration - (time.monotonic() - start)
                timeout_budget = min(args.interval, max(0.001, remaining))
                timeout_ms = max(1, min(1000, int(timeout_budget * 1000)))
                try:
                    response = device.read(128, timeout_ms)
                except Exception as exc:
                    outcome = "error"
                    elapsed = time.monotonic() - start
                    _emit(writer, make_record("probe", "status", metadata, args.label,
                                              elapsed, error=f"read failed: {exc}"))
                    raise DeviceError(f"HID response read failed: {exc}") from exc
                if not response:
                    outcome = "timeout"
                    _emit(writer, make_record("probe", "status", metadata, args.label,
                                              time.monotonic() - start, error="response timeout"))
                    raise DeviceError("status query timed out; stopping without retry")
                response_record = make_record("probe", "status", metadata, args.label,
                                               time.monotonic() - start, bytes(response))
                _emit(writer, response_record)
                report_count += 1
                if args.samples is not None and completed >= args.samples:
                    break
                remaining = args.duration - (time.monotonic() - start)
                if remaining <= 0:
                    break
                wait = max(0.0, min(query_started + args.interval - time.monotonic(), remaining))
                if wait:
                    time.sleep(wait)
        finally:
            device.close()
    except DeviceError:
        if outcome == "completed":
            outcome = "error"
        raise
    finally:
        try:
            _emit(writer, _session_record("probe", "status", metadata, args.label,
                                           "session_end", start, report_count=report_count,
                                           outcome=outcome, query_count=completed))
        finally:
            writer.close()
    return 0


def command_analyze(paths: list[Path]) -> int:
    groups: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    malformed = 0
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as source:
                for line in source:
                    try:
                        record = json.loads(line)
                        raw = record.get("data_hex")
                        if raw is None:
                            continue
                        data = bytes.fromhex(raw)
                        label = str(record.get("label", "unlabeled"))
                        interface = str(record.get("interface", "unknown"))
                        if not data:
                            continue
                        mode = str(record.get("mode", "unknown"))
                        key = (mode, interface, len(data), data[0])
                        group = groups.setdefault(key, {"labels": {}})
                        label_data = group["labels"].setdefault(label, {
                            "report_count": 0,
                            "values": [set() for _ in data],
                        })
                        label_data["report_count"] += 1
                        for offset, value in enumerate(data):
                            label_data["values"][offset].add(value)
                    except (ValueError, TypeError, AttributeError):
                        malformed += 1
        except (OSError, UnicodeError) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            return 2
    for (mode, interface, report_length, report_id), group in sorted(groups.items()):
        labels = group["labels"]
        label_summaries = {
            label: {
                "report_count": details["report_count"],
                "distinct_byte_values_by_offset": [sorted(values) for values in details["values"]],
            }
            for label, details in sorted(labels.items())
        }
        differing = []
        for offset in range(report_length):
            value_sets = {label: sorted(details["values"][offset])
                          for label, details in sorted(labels.items())}
            if len({tuple(values) for values in value_sets.values()}) > 1:
                differing.append({"offset": offset, "labels": value_sets})
        print(json.dumps({
            "mode": mode,
            "interface": interface,
            "report_length": report_length,
            "report_id_first_byte": report_id,
            "labels": label_summaries,
            "cross_label_differing_offsets": differing,
            "interpretation": "raw value-set differences only; no semantic decoding",
        }, sort_keys=True))
    if malformed:
        print(json.dumps({"malformed_lines": malformed}), file=sys.stderr)
    return 0


def _load_hid() -> HidBackend:
    module_name = "hid" if sys.platform == "win32" else "hidraw" if sys.platform.startswith("linux") else None
    if module_name is None:
        raise DeviceError(f"unsupported platform for HID backend: {sys.platform}")
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        if module_name == "hidraw":
            raise DeviceError("Linux hidraw backend unavailable; install hidapi with hidraw support") from exc
        raise DeviceError("Windows hid backend unavailable; install the project dependencies") from exc


def main(argv: list[str] | None = None, backend: HidBackend | None = None,
         audio_router: Any | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "list"
    try:
        if command == "analyze":
            return command_analyze(args.files)
        if command == "audio-list":
            router = audio_router if audio_router is not None else create_audio_router()
            return command_audio_list(router)
        if command == "switch":
            router = audio_router if audio_router is not None else create_audio_router(args.scope)
            return command_switch(args, router)
        if command == "run":
            router = audio_router if audio_router is not None else create_audio_router(args.scope)
            active_backend = backend if backend is not None else _load_hid()
            return command_run(args, router, active_backend)
        active_backend = backend if backend is not None else _load_hid()
        if command == "list":
            return command_list(active_backend)
        if command == "listen":
            return command_listen(args, active_backend)
        if command == "watch":
            return command_watch(args, active_backend)
        if command == "probe":
            return command_probe(args, active_backend)
        parser.error("unknown command")
    except DeviceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except AudioError as exc:
        print(f"audio error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted; HID handle closed.", file=sys.stderr)
        return 130
    return 0
