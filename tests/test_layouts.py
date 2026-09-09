"""Unit tests for the layout descriptors."""

import unittest

import _support

_support.ensure()

from govee_ble_lights.layouts import (  # noqa: E402
    BLENDED,
    SEQUENTIAL,
    blend_color,
    expand_palette,
    visible_zones,
)


class TestExpandPalette(unittest.TestCase):
    def test_sequential_identity(self):
        palette = [[255, 0, 0], [0, 255, 0]]
        self.assertEqual(expand_palette(SEQUENTIAL, palette), palette)

    def test_sequential_does_not_mutate_input(self):
        palette = [[255, 0, 0], [0, 255, 0]]
        expand_palette(SEQUENTIAL, palette)
        self.assertEqual(palette, [[255, 0, 0], [0, 255, 0]])

    def test_blended_doubles_each_color(self):
        expanded = expand_palette(BLENDED, [[255, 0, 0], [255, 255, 255]])
        self.assertEqual(
            expanded, [[255, 0, 0], [255, 0, 0], [255, 255, 255], [255, 255, 255]]
        )

    def test_unknown_layout_treated_as_sequential(self):
        expanded = expand_palette("made-up", [[1, 2, 3]])
        self.assertEqual(expanded, [[1, 2, 3]])


class TestBlendColor(unittest.TestCase):
    def test_average(self):
        self.assertEqual(blend_color([255, 0, 0], [0, 255, 0]), [128, 128, 0])


class TestVisibleZones(unittest.TestCase):
    def test_sequential_matches_input(self):
        colors = [[255, 0, 0], [0, 255, 0]]
        self.assertEqual(visible_zones(SEQUENTIAL, colors), colors)

    def test_blended_pure_and_mix_bands(self):
        addresses = [[255, 0, 0], [0, 255, 0], [0, 0, 255]]
        zones = visible_zones(BLENDED, addresses)
        self.assertEqual(len(zones), 2 * len(addresses))
        # Odd zones are the pure address colors.
        self.assertEqual(zones[0], [255, 0, 0])
        self.assertEqual(zones[2], [0, 255, 0])
        self.assertEqual(zones[4], [0, 0, 255])
        # Even zones are the blend with the following address.
        self.assertEqual(zones[1], blend_color([255, 0, 0], [0, 255, 0]))
        self.assertEqual(zones[3], blend_color([0, 255, 0], [0, 0, 255]))
        self.assertEqual(zones[5], blend_color([0, 0, 255], [255, 0, 0]))

    def test_blended_wraps_last_neighbour(self):
        addresses = [[10, 0, 0], [0, 10, 0]]
        zones = visible_zones(BLENDED, addresses)
        # The final blend band wraps around to the first address.
        self.assertEqual(zones[-1], blend_color([0, 10, 0], [10, 0, 0]))


if __name__ == "__main__":
    unittest.main()
