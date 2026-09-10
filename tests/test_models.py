"""Unit tests for the bundled config.json front-end (models.py)."""

import unittest

import _support

_support.ensure()

from govee_ble_lights import effects, models  # noqa: E402


class TestAvailableModels(unittest.TestCase):
    def test_sorted_and_nonempty(self):
        available = models.get_available_models()
        self.assertTrue(available)
        self.assertEqual(available, sorted(available))
        self.assertIn("H617C", available)
        self.assertIn("H613A", available)


class TestModelFlags(unittest.TestCase):
    def test_segmented(self):
        self.assertTrue(models.is_segmented_model("H617C"))
        self.assertTrue(models.is_segmented_model("H6053"))
        self.assertFalse(models.is_segmented_model("H6006"))
        self.assertFalse(models.is_segmented_model("NOPE"))

    def test_percent_brightness(self):
        self.assertTrue(models.uses_percent_brightness("H6199"))
        self.assertFalse(models.uses_percent_brightness("H6006"))

    def test_segment_count(self):
        # H6053 declares 12; everything else defaults to 15, capped at 15.
        self.assertEqual(models.get_segment_count("H6053"), 12)
        self.assertEqual(models.get_segment_count("H6006"), 15)
        self.assertEqual(models.get_segment_count("H617C"), 15)
        self.assertEqual(models.get_segment_count("NOPE"), 15)


class TestEffects(unittest.TestCase):
    def test_shared_effects_exist(self):
        effects = models.get_effects()
        self.assertIn("Warm Christmas", effects)
        self.assertIn("Halloween", effects)

    def test_warm_christmas_is_merged_red_and_warm_white(self):
        # Christmas and Christmas Warm were merged: warm white (#ff842b)
        # accounts for the LEDs not being true color.
        self.assertEqual(
            models.get_effects()["Warm Christmas"]["colors"],
            [[255, 0, 0], [255, 132, 43]],
        )

    def test_model_effects_include_shared(self):
        effects = models.get_model_effects("H617C")
        self.assertIn("Warm Christmas", effects)
        self.assertGreaterEqual(len(effects), 2)

    def test_unknown_model_has_shared_effects(self):
        # Shared effects apply to every model; per-model overrides add on top.
        self.assertIn("Warm Christmas", models.get_model_effects("H6006"))


class TestDetectModel(unittest.TestCase):
    def test_detects_model_in_govee_name(self):
        self.assertEqual(models.detect_model("Govee_H617C_2482"), "H617C")

    def test_plain_model_name(self):
        self.assertEqual(models.detect_model("H6053"), "H6053")

    def test_lowercase_name(self):
        self.assertEqual(models.detect_model("govee_h617c_1234"), "H617C")

    def test_unknown_model_returns_none(self):
        self.assertIsNone(models.detect_model("Govee_H1234_5678"))

    def test_no_model_in_name(self):
        self.assertIsNone(models.detect_model("Living Room TV"))
        self.assertIsNone(models.detect_model(""))


class TestFades(unittest.TestCase):
    def test_fade_values_present(self):
        self.assertGreaterEqual(models.get_default_fade(), 0.0)
        self.assertGreaterEqual(models.get_fade_on(), 0.0)
        self.assertGreaterEqual(models.get_fade_off(), 0.0)


class TestBundledEffects(unittest.TestCase):
    def test_all_effects_render(self):
        """Every bundled effect must render for a full-length strip."""
        for name, effect in models.get_effects().items():
            colors = effects.segment_colors(effect, 15)
            self.assertEqual(len(colors), 15)
            for color in colors:
                self.assertEqual(len(color), 3)
                self.assertTrue(all(0 <= c <= 255 for c in color))

    def test_animation_timings_are_valid(self):
        for name, effect in models.get_effects().items():
            step = float(effect.get("step", 0))
            fade = float(effect.get("fade", models.get_default_fade()))
            self.assertGreaterEqual(step, 0.0, name)
            self.assertGreaterEqual(fade, 0.0, name)


if __name__ == "__main__":
    unittest.main()
