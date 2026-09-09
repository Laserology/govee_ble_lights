"""Unit tests for effect rendering (palette -> segment colors -> writes)."""

import unittest

import _support

_support.ensure()

from govee_ble_lights.effects import (  # noqa: E402
    interpolate_segments,
    segment_colors,
    segments_to_writes,
)

RED = [255, 0, 0]
GREEN = [0, 255, 0]
BLUE = [0, 0, 255]


class TestSegmentColors(unittest.TestCase):
    def test_cycles_palette(self):
        effect = {"colors": [RED, GREEN, BLUE]}
        self.assertEqual(
            segment_colors(effect, 5),
            [RED, GREEN, BLUE, RED, GREEN],
        )

    def test_offset_shifts_pattern(self):
        effect = {"colors": [RED, GREEN, BLUE]}
        self.assertEqual(
            segment_colors(effect, 3, offset=1),
            [GREEN, BLUE, RED],
        )

    def test_clamps_to_protocol_limit(self):
        effect = {"colors": [RED]}
        # Segment_count beyond the 15-segment protocol limit is clamped.
        self.assertEqual(len(segment_colors(effect, 40)), 15)

    def test_rejects_empty_palette(self):
        with self.assertRaises(ValueError):
            segment_colors({"colors": []}, 5)

    def test_rejects_invalid_color(self):
        with self.assertRaises(ValueError):
            segment_colors({"colors": [[300, 0, 0]]}, 5)
        with self.assertRaises(ValueError):
            segment_colors({"colors": [[1, 2]]}, 5)


class TestSegmentsToWrites(unittest.TestCase):
    def test_groups_by_color(self):
        writes = segments_to_writes([RED, RED, GREEN, GREEN, RED])
        self.assertEqual(len(writes), 2)
        red = next(w for w in writes if w["color"] == RED)
        green = next(w for w in writes if w["color"] == GREEN)
        # Segments 1, 2 and 5 are red -> bits 0, 1, 4 in the low byte.
        self.assertEqual(red["mask_lo"], 0x13)
        # Segments 3 and 4 are green -> bits 2 and 3 in the low byte.
        self.assertEqual(green["mask_lo"], 0x0C)

    def test_high_byte_for_segments_9_plus(self):
        colors = [RED] * 9 + [GREEN] * 6
        writes = segments_to_writes(colors)
        red = next(w for w in writes if w["color"] == RED)
        green = next(w for w in writes if w["color"] == GREEN)
        self.assertEqual(red["mask_lo"], 0xFF)
        self.assertEqual(red["mask_hi"], 0x01)  # segment 9
        self.assertEqual(green["mask_hi"], 0x7E)  # segments 10-15


class TestInterpolateSegments(unittest.TestCase):
    def test_midpoint(self):
        result = interpolate_segments([[255, 0, 0]], [[255, 255, 255]], 0.5)
        self.assertEqual(result, [[255, 128, 128]])

    def test_clamps_fraction(self):
        self.assertEqual(
            interpolate_segments([[0, 0, 0]], [[255, 0, 0]], 2.0), [[255, 0, 0]]
        )
        self.assertEqual(
            interpolate_segments([[0, 0, 0]], [[255, 0, 0]], -1.0), [[0, 0, 0]]
        )


if __name__ == "__main__":
    unittest.main()
