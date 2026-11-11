import unittest

from nova_mic_switch.audio_common import AudioError
from nova_mic_switch.audio_controller import SourceToggleController, opposite_source


DESKTOP = "desktop-source"
HEADSET = "headset-source"


class FakeRouter:
    def __init__(self, current=DESKTOP):
        self.current = current
        self.selected = []

    def list_sources(self):
        return []

    def current_source(self):
        return self.current

    def select_source(self, source_id):
        self.selected.append(source_id)
        self.current = source_id
        return {"source_id": source_id, "verified": True}


class AudioControllerTests(unittest.TestCase):
    def test_opposite_source_maps_only_configured_pair(self):
        self.assertEqual(opposite_source(DESKTOP, DESKTOP, HEADSET), HEADSET)
        self.assertEqual(opposite_source(HEADSET, DESKTOP, HEADSET), DESKTOP)
        with self.assertRaisesRegex(AudioError, "outside the configured pair"):
            opposite_source("other", DESKTOP, HEADSET)
        with self.assertRaisesRegex(AudioError, "distinct"):
            opposite_source(DESKTOP, DESKTOP, DESKTOP)

    def test_dry_run_finds_target_without_selecting(self):
        router = FakeRouter(DESKTOP)
        result = SourceToggleController(router, DESKTOP, HEADSET).trigger()
        self.assertEqual(result, {
            "current_source": DESKTOP,
            "target_source": HEADSET,
            "applied": False,
            "dry_run": True,
        })
        self.assertEqual(router.selected, [])

    def test_apply_selects_the_other_source_and_returns_verification(self):
        router = FakeRouter(HEADSET)
        result = SourceToggleController(router, DESKTOP, HEADSET, apply=True).trigger()
        self.assertEqual(router.selected, [DESKTOP])
        self.assertTrue(result["applied"])
        self.assertEqual(result["routing_result"], {"source_id": DESKTOP, "verified": True})

    def test_outside_pair_does_not_select_any_source(self):
        router = FakeRouter("other")
        controller = SourceToggleController(router, DESKTOP, HEADSET, apply=True)
        with self.assertRaises(AudioError):
            controller.trigger()
        self.assertEqual(router.selected, [])


if __name__ == "__main__":
    unittest.main()
