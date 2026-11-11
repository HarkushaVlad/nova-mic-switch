import ctypes
import importlib
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from nova_mic_switch.audio_common import AudioError, AudioSource
from nova_mic_switch.audio_windows import (
    GUID,
    PROPVARIANT,
    PROPERTYKEY,
    WindowsAudioRouter,
    _WindowsAudioSession,
)


class FakeNative:
    def __init__(self, endpoints=None, defaults=None):
        self.endpoints = (
            [("mic-a", "Microphone A"), ("mic-b", "Microphone B")]
            if endpoints is None
            else endpoints
        )
        self.defaults = (
            {0: "mic-a", 1: "mic-a", 2: "mic-a"}
            if defaults is None
            else defaults
        )
        self.set_calls = []
        self.fail_set_role = None
        self.fail_read_role = None
        self.force_mismatch_role = None

    def active_capture_ids(self):
        return list(self.endpoints)

    def default_capture_id(self, role):
        if role == self.fail_read_role or role not in self.defaults:
            raise AudioError("no default")
        if role == self.force_mismatch_role:
            return "unexpected"
        return self.defaults[role]

    def set_default_capture(self, endpoint_id, role):
        self.set_calls.append((endpoint_id, role))
        if role == self.fail_set_role:
            raise AudioError("native setter failed")
        self.defaults[role] = endpoint_id


def make_router(native):
    @contextmanager
    def factory():
        yield native

    return WindowsAudioRouter(factory)


class WindowsAudioRouterTests(unittest.TestCase):
    def test_list_sources_marks_communications_default(self):
        native = FakeNative(defaults={0: "mic-a", 1: "mic-a", 2: "mic-b"})

        self.assertEqual(
            make_router(native).list_sources(),
            [
                AudioSource("mic-a", "Microphone A", False),
                AudioSource("mic-b", "Microphone B", True),
            ],
        )

    def test_empty_captures_do_not_set_any_roles(self):
        native = FakeNative(endpoints=[], defaults={})

        self.assertEqual(make_router(native).list_sources(), [])
        with self.assertRaisesRegex(AudioError, "not an active capture"):
            make_router(native).select_source("mic-a")
        self.assertEqual(native.set_calls, [])

    def test_current_source_requires_communications_default(self):
        native = FakeNative()
        native.fail_read_role = 2

        with self.assertRaisesRegex(AudioError, "No default communications.*no default"):
            make_router(native).current_source()

    def test_native_factory_failure_is_reported_as_audio_error(self):
        def broken_factory():
            raise OSError("COM setup failed")

        with self.assertRaisesRegex(AudioError, "Could not create Windows audio session"):
            WindowsAudioRouter(broken_factory).list_sources()

    def test_select_sets_all_roles_in_order_and_returns_system_scope(self):
        native = FakeNative()

        self.assertEqual(
            make_router(native).select_source("mic-b"),
            {"scope": "system", "target": "mic-b", "changed_roles": [0, 1, 2]},
        )
        self.assertEqual(native.set_calls, [("mic-b", 0), ("mic-b", 1), ("mic-b", 2)])

    def test_select_rejects_non_active_capture_endpoints(self):
        for source_id in ("speaker", "missing"):
            with self.subTest(source_id=source_id):
                native = FakeNative()
                with self.assertRaisesRegex(AudioError, "not an active capture"):
                    make_router(native).select_source(source_id)
                self.assertEqual(native.set_calls, [])

    def test_select_reports_roles_changed_before_later_set_failure(self):
        native = FakeNative()
        native.fail_set_role = 1

        with self.assertRaisesRegex(AudioError, r"roles already changed: \[0\]"):
            make_router(native).select_source("mic-b")
        self.assertEqual(native.defaults[0], "mic-b")
        self.assertEqual(native.defaults[1], "mic-a")
        self.assertEqual(native.set_calls, [("mic-b", 0), ("mic-b", 1)])

    def test_select_reports_readback_mismatch_without_rollback(self):
        native = FakeNative()
        native.force_mismatch_role = 1

        with self.assertRaisesRegex(AudioError, r"verification failed \(role 1 returned 'unexpected'\)"):
            make_router(native).select_source("mic-b")
        self.assertEqual(native.defaults, {0: "mic-b", 1: "mic-b", 2: "mic-b"})

    def test_select_reports_readback_native_failure_reason(self):
        native = FakeNative()
        native.fail_read_role = 1

        with self.assertRaisesRegex(AudioError, r"role 1 read failed: no default"):
            make_router(native).select_source("mic-b")

    def test_select_can_recover_missing_default_by_setting_it(self):
        native = FakeNative(defaults={0: "mic-a", 1: "mic-a"})

        result = make_router(native).select_source("mic-b")

        self.assertEqual(result["changed_roles"], [0, 1, 2])
        self.assertEqual(native.defaults[2], "mic-b")

    def test_already_selected_roles_are_skipped(self):
        native = FakeNative(defaults={0: "mic-b", 1: "mic-b", 2: "mic-a"})

        result = make_router(native).select_source("mic-b")

        self.assertEqual(result["changed_roles"], [2])
        self.assertEqual(native.set_calls, [("mic-b", 2)])

    def test_com_struct_layout_matches_pointer_architecture(self):
        self.assertEqual(ctypes.sizeof(GUID), 16)
        self.assertEqual(ctypes.sizeof(PROPERTYKEY), 20)
        expected_size = 24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16
        self.assertEqual(ctypes.sizeof(PROPVARIANT), expected_size)

    def test_native_session_refuses_non_windows_before_loading_dll(self):
        import nova_mic_switch.audio_windows as module

        with patch.object(module.sys, "platform", "linux"), patch.object(
            module.ctypes,
            "WinDLL",
            side_effect=AssertionError("DLL loaded"),
            create=True,
        ):
            with self.assertRaisesRegex(AudioError, "only available on Windows"):
                _WindowsAudioSession()

    def test_module_import_does_not_create_native_factory(self):
        import nova_mic_switch.audio_windows as module

        with patch.object(module.ctypes, "WinDLL", side_effect=AssertionError("DLL loaded"), create=True):
            importlib.reload(module)
        self.assertIs(module.sys, sys)


class NativeCleanupTests(unittest.TestCase):
    class Callable:
        def __init__(self, result):
            self.result = result
            self.calls = 0

        def __call__(self, *args):
            self.calls += 1
            return self.result

    class FakeOle32:
        def __init__(self, init_result=0, create_result=0, clear_result=0):
            self.CoInitializeEx = NativeCleanupTests.Callable(init_result)
            self.CoUninitialize = NativeCleanupTests.Callable(None)
            self.CoCreateInstance = NativeCleanupTests.Callable(create_result)
            self.CoTaskMemFree = NativeCleanupTests.Callable(None)
            self.PropVariantClear = NativeCleanupTests.Callable(clear_result)

    def make_session(self, ole32):
        session = _WindowsAudioSession.__new__(_WindowsAudioSession)
        session._ole32 = ole32
        session._initialized = False
        session._enumerator = ctypes.c_void_p()
        return session

    def test_friendly_name_always_releases_store_when_variant_clear_fails(self):
        import nova_mic_switch.audio_windows as module

        ole32 = self.FakeOle32(clear_result=-1)
        session = self.make_session(ole32)
        release_calls = []

        def fake_vcall(pointer, index, result, *args):
            if index == 4:  # IMMDevice.OpenPropertyStore
                return lambda _flags, output: setattr(output._obj, "value", 123) or 0
            if index == 5:  # IPropertyStore.GetValue
                def get_value(_key, output):
                    output._obj.vt = module._VT_LPWSTR
                    output._obj.pwszVal = 456
                    return 0
                return get_value
            self.fail(f"Unexpected vtable index {index}")

        with patch.object(module, "_vcall", side_effect=fake_vcall), patch.object(
            session, "_release", side_effect=lambda pointer: release_calls.append(pointer.value)
        ), patch.object(module.ctypes, "wstring_at", return_value="Microphone"):
            with self.assertRaisesRegex(AudioError, "PropVariantClear failed"):
                session._friendly_name(ctypes.c_void_p(1))

        self.assertEqual(release_calls, [123])
        self.assertEqual(ole32.PropVariantClear.calls, 1)

    def test_friendly_name_preserves_primary_error_when_clear_fails(self):
        import nova_mic_switch.audio_windows as module

        ole32 = self.FakeOle32(clear_result=-1)
        session = self.make_session(ole32)
        release_calls = []

        def fake_vcall(pointer, index, result, *args):
            if index == 4:
                return lambda _flags, output: setattr(output._obj, "value", 123) or 0
            if index == 5:
                return lambda _key, _output: -2147467259
            self.fail(f"Unexpected vtable index {index}")

        with patch.object(module, "_vcall", side_effect=fake_vcall), patch.object(
            session, "_release", side_effect=lambda pointer: release_calls.append(pointer.value)
        ):
            with self.assertRaisesRegex(AudioError, r"IPropertyStore.GetValue failed \(HRESULT 0x80004005\)"):
                session._friendly_name(ctypes.c_void_p(1))

        self.assertEqual(release_calls, [123])
        self.assertEqual(ole32.PropVariantClear.calls, 1)

    def test_device_id_rejects_null_success_output_without_reading(self):
        import nova_mic_switch.audio_windows as module

        ole32 = self.FakeOle32()
        session = self.make_session(ole32)
        with patch.object(module, "_vcall", return_value=lambda _output: 0), patch.object(
            module.ctypes, "wstring_at", side_effect=AssertionError("null pointer read")
        ) as read_string:
            with self.assertRaisesRegex(AudioError, "without returning an endpoint ID"):
                session._device_id(ctypes.c_void_p(1))
        read_string.assert_not_called()
        self.assertEqual(ole32.CoTaskMemFree.calls, 0)

    def test_coinitialize_s_ok_and_s_false_are_balanced(self):
        for init_result in (0, 1):
            with self.subTest(init_result=init_result):
                ole32 = self.FakeOle32(init_result=init_result)
                session = self.make_session(ole32)
                with session:
                    self.assertTrue(session._initialized)
                self.assertEqual(ole32.CoInitializeEx.calls, 1)
                self.assertEqual(ole32.CoCreateInstance.calls, 1)
                self.assertEqual(ole32.CoUninitialize.calls, 1)

    def test_changed_mode_does_not_uninitialize_or_create_enumerator(self):
        ole32 = self.FakeOle32(init_result=-2147417850)
        session = self.make_session(ole32)

        with self.assertRaisesRegex(AudioError, "CoInitializeEx failed"):
            session.__enter__()

        self.assertEqual(ole32.CoInitializeEx.calls, 1)
        self.assertEqual(ole32.CoCreateInstance.calls, 0)
        self.assertEqual(ole32.CoUninitialize.calls, 0)

    def test_enumerator_activation_failure_uninitializes_once(self):
        ole32 = self.FakeOle32(create_result=-2147467259)
        session = self.make_session(ole32)

        with self.assertRaisesRegex(AudioError, r"CoCreateInstance\(IMMDeviceEnumerator\) failed"):
            session.__enter__()

        self.assertEqual(ole32.CoInitializeEx.calls, 1)
        self.assertEqual(ole32.CoCreateInstance.calls, 1)
        self.assertEqual(ole32.CoUninitialize.calls, 1)


if __name__ == "__main__":
    unittest.main()
