"""Windows capture endpoint routing through Core Audio and PolicyConfig."""

from __future__ import annotations

import ctypes
import sys
from contextlib import AbstractContextManager
from typing import Any, Callable

from .audio_common import AudioError, AudioSource


HRESULT = ctypes.c_int32
ULONG = ctypes.c_uint32
DWORD = ctypes.c_uint32
UINT = ctypes.c_uint32


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", DWORD)]


class _CountedBlob(ctypes.Structure):
    _fields_ = [("cbSize", ULONG), ("pBlobData", ctypes.c_void_p)]


class _PropValue(ctypes.Union):
    _fields_ = [("pwszVal", ctypes.c_void_p), ("blob", _CountedBlob)]


class PROPVARIANT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("vt", ctypes.c_uint16),
        ("wReserved1", ctypes.c_uint16),
        ("wReserved2", ctypes.c_uint16),
        ("wReserved3", ctypes.c_uint16),
        ("value", _PropValue),
    ]


assert ctypes.sizeof(GUID) == 16
assert ctypes.sizeof(PROPERTYKEY) == 20
assert ctypes.sizeof(PROPVARIANT) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)


_CLSID_MMDEVICE_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
_IID_MMDEVICE_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
_CLSID_POLICY_CONFIG = "870AF99C-171D-4F9E-AF0D-E63DF40C2BC9"
_IID_POLICY_CONFIG = "F8679F50-850A-41CF-9C72-430F290290C8"
_PKEY_FRIENDLY_NAME = PROPERTYKEY(
    GUID(0xA45C254E, 0xDF1C, 0x4EFD, (ctypes.c_ubyte * 8)(0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0)),
    14,
)

_E_CAPTURE = 1
_STATE_ACTIVE = 1
_ROLE_COMMUNICATIONS = 2
_ROLES = (0, 1, 2)
_VT_LPWSTR = 31


def _guid(text: str) -> GUID:
    import uuid

    value = uuid.UUID(text)
    raw = value.bytes_le
    return GUID.from_buffer_copy(raw)


def _check_hr(hr: int, operation: str) -> None:
    if hr < 0:
        raise AudioError(f"{operation} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


def _vcall(pointer: ctypes.c_void_p, index: int, result: Any, *args: Any) -> Any:
    if not pointer or not pointer.value:
        raise AudioError("Core Audio returned a null COM interface")
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *args)
    function = prototype(table[index])
    return lambda *values: function(pointer, *values)


class _WindowsAudioSession(AbstractContextManager["_WindowsAudioSession"]):
    """A single-threaded COM session; no interface escapes its context."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise AudioError("Windows audio routing is only available on Windows")
        self._ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        self._ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, DWORD]
        self._ole32.CoInitializeEx.restype = HRESULT
        self._ole32.CoUninitialize.argtypes = []
        self._ole32.CoUninitialize.restype = None
        self._ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(GUID), ctypes.c_void_p, DWORD,
            ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        self._ole32.CoCreateInstance.restype = HRESULT
        self._ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self._ole32.CoTaskMemFree.restype = None
        self._ole32.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
        self._ole32.PropVariantClear.restype = HRESULT
        self._initialized = False
        self._enumerator = ctypes.c_void_p()

    def __enter__(self) -> "_WindowsAudioSession":
        hr = self._ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        if hr not in (0, 1):
            _check_hr(hr, "CoInitializeEx")
        self._initialized = True
        try:
            clsid, iid = _guid(_CLSID_MMDEVICE_ENUMERATOR), _guid(_IID_MMDEVICE_ENUMERATOR)
            hr = self._ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(self._enumerator))
            _check_hr(hr, "CoCreateInstance(IMMDeviceEnumerator)")
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_: object) -> None:
        if self._enumerator:
            self._release(self._enumerator)
            self._enumerator = ctypes.c_void_p()
        if self._initialized:
            self._ole32.CoUninitialize()
            self._initialized = False

    @staticmethod
    def _release(pointer: ctypes.c_void_p) -> None:
        _vcall(pointer, 2, ULONG)()

    def _enumerate(self) -> list[tuple[str, str]]:
        collection = ctypes.c_void_p()
        hr = _vcall(self._enumerator, 3, HRESULT, ctypes.c_int, DWORD, ctypes.POINTER(ctypes.c_void_p))(
            _E_CAPTURE, _STATE_ACTIVE, ctypes.byref(collection)
        )
        _check_hr(hr, "EnumAudioEndpoints")
        results: list[tuple[str, str]] = []
        try:
            count = UINT()
            _check_hr(_vcall(collection, 3, HRESULT, ctypes.POINTER(UINT))(ctypes.byref(count)), "IMMDeviceCollection.GetCount")
            for index in range(count.value):
                device = ctypes.c_void_p()
                _check_hr(_vcall(collection, 4, HRESULT, UINT, ctypes.POINTER(ctypes.c_void_p))(index, ctypes.byref(device)), "IMMDeviceCollection.Item")
                try:
                    results.append((self._device_id(device), self._friendly_name(device)))
                finally:
                    self._release(device)
            return results
        finally:
            self._release(collection)

    def _device_id(self, device: ctypes.c_void_p) -> str:
        result = ctypes.c_void_p()
        _check_hr(_vcall(device, 5, HRESULT, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(result)), "IMMDevice.GetId")
        if not result.value:
            raise AudioError("IMMDevice.GetId succeeded without returning an endpoint ID")
        try:
            return ctypes.wstring_at(result.value)
        finally:
            self._ole32.CoTaskMemFree(result)

    def _friendly_name(self, device: ctypes.c_void_p) -> str:
        store = ctypes.c_void_p()
        _check_hr(_vcall(device, 4, HRESULT, DWORD, ctypes.POINTER(ctypes.c_void_p))(0, ctypes.byref(store)), "IMMDevice.OpenPropertyStore")
        variant = PROPVARIANT()
        try:
            try:
                _check_hr(_vcall(store, 5, HRESULT, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(
                    ctypes.byref(_PKEY_FRIENDLY_NAME), ctypes.byref(variant)
                ), "IPropertyStore.GetValue")
                if variant.vt != _VT_LPWSTR or not variant.pwszVal:
                    raise AudioError("Capture endpoint has no valid friendly name")
                name = ctypes.wstring_at(variant.pwszVal)
            except BaseException:
                # Preserve the useful operation error if variant cleanup also fails.
                try:
                    _check_hr(self._ole32.PropVariantClear(ctypes.byref(variant)), "PropVariantClear")
                except BaseException:
                    pass
                raise
            _check_hr(self._ole32.PropVariantClear(ctypes.byref(variant)), "PropVariantClear")
            return name
        finally:
            self._release(store)

    def active_capture_ids(self) -> list[tuple[str, str]]:
        return self._enumerate()

    def default_capture_id(self, role: int) -> str:
        device = ctypes.c_void_p()
        hr = _vcall(self._enumerator, 4, HRESULT, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            _E_CAPTURE, role, ctypes.byref(device)
        )
        _check_hr(hr, f"GetDefaultAudioEndpoint(role={role})")
        try:
            return self._device_id(device)
        finally:
            self._release(device)

    def set_default_capture(self, endpoint_id: str, role: int) -> None:
        clsid, iid = _guid(_CLSID_POLICY_CONFIG), _guid(_IID_POLICY_CONFIG)
        policy = ctypes.c_void_p()
        hr = self._ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(policy))
        _check_hr(hr, "CoCreateInstance(IPolicyConfig)")
        try:
            # IPolicyConfig::SetDefaultEndpoint is an undocumented Windows implementation API.
            _check_hr(_vcall(policy, 13, HRESULT, ctypes.c_wchar_p, ctypes.c_int)(endpoint_id, role), f"SetDefaultEndpoint(role={role})")
        finally:
            self._release(policy)


class WindowsAudioRouter:
    """Routes the system-wide Windows default capture endpoint."""

    def __init__(self, native_factory: Callable[[], AbstractContextManager[Any]] | None = None) -> None:
        self._native_factory = native_factory or _WindowsAudioSession

    def list_sources(self) -> list[AudioSource]:
        with self._session() as native:
            endpoints = native.active_capture_ids()
            try:
                default_id = native.default_capture_id(_ROLE_COMMUNICATIONS)
            except AudioError:
                default_id = None
            return [AudioSource(id, name, id == default_id) for id, name in endpoints]

    def current_source(self) -> str:
        with self._session() as native:
            try:
                return native.default_capture_id(_ROLE_COMMUNICATIONS)
            except AudioError as exc:
                raise AudioError(f"No default communications capture endpoint is available: {exc}") from exc

    def select_source(self, source_id: str) -> dict[str, Any]:
        with self._session() as native:
            active_ids = {endpoint_id for endpoint_id, _ in native.active_capture_ids()}
            if source_id not in active_ids:
                raise AudioError("Requested source is not an active capture endpoint")
            changed: list[int] = []
            for role in _ROLES:
                try:
                    before = native.default_capture_id(role)
                except AudioError:
                    before = None
                if before == source_id:
                    continue
                try:
                    native.set_default_capture(source_id, role)
                except AudioError as exc:
                    suffix = f"; roles already changed: {changed}" if changed else ""
                    raise AudioError(f"Could not set default capture role {role}{suffix}: {exc}") from exc
                changed.append(role)

            mismatches: list[str] = []
            for role in _ROLES:
                try:
                    actual_id = native.default_capture_id(role)
                    if actual_id != source_id:
                        mismatches.append(f"role {role} returned {actual_id!r}")
                except AudioError as exc:
                    mismatches.append(f"role {role} read failed: {exc}")
            if mismatches:
                raise AudioError(
                    f"Default capture verification failed ({'; '.join(mismatches)}); roles changed: {changed}"
                )
            return {"scope": "system", "target": source_id, "changed_roles": changed}

    def _session(self) -> AbstractContextManager[Any]:
        try:
            return self._native_factory()
        except AudioError:
            raise
        except Exception as exc:
            raise AudioError(f"Could not create Windows audio session: {exc}") from exc
