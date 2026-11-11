import unittest
from unittest.mock import patch

from nova_mic_switch import audio_factory
from nova_mic_switch.audio_common import AudioError
from nova_mic_switch.audio_linux import LinuxAudioRouter


class WindowsRouterStub:
    def __init__(self):
        self.scope = "system"


class AudioFactoryTests(unittest.TestCase):
    def test_linux_defaults_to_discord_router_without_windows_import(self):
        with patch.object(audio_factory.sys, "platform", "linux"), \
                patch.object(audio_factory.importlib, "import_module",
                             side_effect=AssertionError("Windows module must stay lazy")):
            self.assertEqual(audio_factory.default_scope(), "discord")
            router = audio_factory.create_audio_router()
        self.assertIsInstance(router, LinuxAudioRouter)
        self.assertEqual(router.scope, "discord")

    def test_linux_system_scope_is_explicit(self):
        with patch.object(audio_factory.sys, "platform", "linux"):
            router = audio_factory.create_audio_router("system")
        self.assertIsInstance(router, LinuxAudioRouter)
        self.assertEqual(router.scope, "system")

    def test_windows_defaults_to_system_and_imports_only_on_windows(self):
        with patch.object(audio_factory.sys, "platform", "win32"), \
                patch.object(audio_factory.importlib, "import_module", return_value=type(
                    "WindowsModule", (), {"WindowsAudioRouter": WindowsRouterStub}
                )) as importer:
            self.assertEqual(audio_factory.default_scope(), "system")
            router = audio_factory.create_audio_router()
        self.assertIsInstance(router, WindowsRouterStub)
        importer.assert_called_once_with(".audio_windows", package=audio_factory.__package__)

    def test_windows_rejects_discord_scope_before_import(self):
        with patch.object(audio_factory.sys, "platform", "win32"), \
                patch.object(audio_factory.importlib, "import_module",
                             side_effect=AssertionError("unsupported scope must fail first")):
            with self.assertRaisesRegex(AudioError, "only --scope system"):
                audio_factory.create_audio_router("discord")


if __name__ == "__main__":
    unittest.main()
