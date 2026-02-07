"""Unit tests for the Govee BLE protocol module."""

import os
import sys
import unittest

# Add the protocol module's directory to the path for direct import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "govee_ble_lights"))

from protocol import (
    MUSIC_MODE_IDS,
    SCENE_IDS,
    build_brightness,
    build_color_rgb,
    build_color_rgb_simple,
    build_color_temp,
    build_keep_alive,
    build_music_mode,
    build_packet,
    build_power,
    build_scene,
    build_segment_color,
    build_state_query,
    kelvin_to_rgb,
    xor_checksum,
)


class TestChecksum(unittest.TestCase):
    def test_xor_all_zeros(self):
        self.assertEqual(xor_checksum(bytes(19)), 0x00)

    def test_xor_power_on(self):
        data = bytearray([0x33, 0x01, 0x01] + [0x00] * 16)
        self.assertEqual(xor_checksum(data), 0x33)

    def test_xor_power_off(self):
        data = bytearray([0x33, 0x01, 0x00] + [0x00] * 16)
        self.assertEqual(xor_checksum(data), 0x32)

    def test_xor_state_query(self):
        data = bytearray([0xAA, 0x01] + [0x00] * 17)
        self.assertEqual(xor_checksum(data), 0xAB)


class TestBuildPacket(unittest.TestCase):
    def test_packet_length(self):
        self.assertEqual(len(build_packet(0x33, 0x01, [0x01])), 20)

    def test_packet_checksum_valid(self):
        packet = build_packet(0x33, 0x01, [0x01])
        self.assertEqual(xor_checksum(packet[:19]), packet[19])

    def test_packet_padding(self):
        packet = build_packet(0x33, 0x01, [])
        for i in range(2, 19):
            self.assertEqual(packet[i], 0x00)

    def test_packet_truncates_long_params(self):
        params = list(range(20))
        packet = build_packet(0x33, 0x01, params)
        self.assertEqual(len(packet), 20)


class TestPower(unittest.TestCase):
    def test_power_on(self):
        expected = bytes.fromhex("3301010000000000000000000000000000000033")
        self.assertEqual(build_power(True), expected)

    def test_power_off(self):
        expected = bytes.fromhex("3301000000000000000000000000000000000032")
        self.assertEqual(build_power(False), expected)

    def test_power_length(self):
        self.assertEqual(len(build_power(True)), 20)
        self.assertEqual(len(build_power(False)), 20)


class TestBrightness(unittest.TestCase):
    def test_brightness_100(self):
        packet = build_brightness(100)
        self.assertEqual(len(packet), 20)
        self.assertEqual(packet[0], 0x33)
        self.assertEqual(packet[1], 0x04)
        self.assertEqual(packet[2], 100)

    def test_brightness_0(self):
        self.assertEqual(build_brightness(0)[2], 0)

    def test_brightness_clamp_high(self):
        self.assertEqual(build_brightness(200)[2], 100)

    def test_brightness_clamp_low(self):
        self.assertEqual(build_brightness(-10)[2], 0)

    def test_brightness_checksum(self):
        packet = build_brightness(50)
        self.assertEqual(xor_checksum(packet[:19]), packet[19])


class TestColorRGB(unittest.TestCase):
    def test_color_red_segmented(self):
        packet = build_color_rgb(255, 0, 0)
        self.assertEqual(len(packet), 20)
        self.assertEqual(packet[0], 0x33)
        self.assertEqual(packet[1], 0x05)
        self.assertEqual(packet[2], 0x15)  # segmented mode
        self.assertEqual(packet[3], 0x01)  # set color sub-command
        self.assertEqual(packet[4], 0xFF)  # R
        self.assertEqual(packet[5], 0x00)  # G
        self.assertEqual(packet[6], 0x00)  # B
        self.assertEqual(packet[12], 0xFF)  # seg_lo (all)
        self.assertEqual(packet[13], 0x7F)  # seg_hi (15 segments)

    def test_color_matches_known_format(self):
        packet = build_color_rgb(0xFF, 0x00, 0x00)
        self.assertEqual(
            packet[:14],
            bytes(
                [
                    0x33,
                    0x05,
                    0x15,
                    0x01,
                    0xFF,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0xFF,
                    0x7F,
                ]
            ),
        )

    def test_color_clamp(self):
        packet = build_color_rgb(300, -10, 128)
        self.assertEqual(packet[4], 255)
        self.assertEqual(packet[5], 0)
        self.assertEqual(packet[6], 128)


class TestColorRGBSimple(unittest.TestCase):
    def test_simple_mode_byte(self):
        packet = build_color_rgb_simple(255, 0, 0)
        self.assertEqual(packet[2], 0x02)

    def test_simple_rgb_values(self):
        packet = build_color_rgb_simple(0xAA, 0xBB, 0xCC)
        self.assertEqual(packet[3], 0xAA)
        self.assertEqual(packet[4], 0xBB)
        self.assertEqual(packet[5], 0xCC)

    def test_simple_length(self):
        self.assertEqual(len(build_color_rgb_simple(128, 128, 128)), 20)


class TestColorTemp(unittest.TestCase):
    def test_packet_length(self):
        self.assertEqual(len(build_color_temp(4000)), 20)

    def test_uses_segmented_mode(self):
        packet = build_color_temp(4000)
        self.assertEqual(packet[2], 0x15)

    def test_warm_has_high_red(self):
        r, g, b = kelvin_to_rgb(2700)
        self.assertEqual(r, 255)
        self.assertGreater(g, 100)
        self.assertLess(b, g)

    def test_cool_has_high_blue(self):
        r, g, b = kelvin_to_rgb(6500)
        self.assertEqual(r, 255)
        self.assertGreater(b, 200)


class TestScene(unittest.TestCase):
    def test_scene_sunrise(self):
        expected = bytes.fromhex("3305040000000000000000000000000000000032")
        self.assertEqual(build_scene(0x00), expected)

    def test_scene_sunset(self):
        expected = bytes.fromhex("3305040100000000000000000000000000000033")
        self.assertEqual(build_scene(0x01), expected)

    def test_scene_movie(self):
        expected = bytes.fromhex("3305040400000000000000000000000000000036")
        self.assertEqual(build_scene(0x04), expected)

    def test_scene_rainbow(self):
        expected = bytes.fromhex("3305041600000000000000000000000000000024")
        self.assertEqual(build_scene(0x16), expected)

    def test_all_scenes_valid(self):
        for name, sid in SCENE_IDS.items():
            packet = build_scene(sid)
            self.assertEqual(len(packet), 20, f"Scene {name} wrong length")
            self.assertEqual(
                xor_checksum(packet[:19]),
                packet[19],
                f"Scene {name} bad checksum",
            )


class TestMusicMode(unittest.TestCase):
    def test_music_energetic(self):
        expected = bytes.fromhex("3305130563000000000000000000000000000043")
        self.assertEqual(build_music_mode(0x05, 0x63), expected)

    def test_music_spectrum(self):
        expected = bytes.fromhex("3305130463000000000000000000000000000042")
        self.assertEqual(build_music_mode(0x04, 0x63), expected)

    def test_music_rhythm(self):
        expected = bytes.fromhex("3305130363000000000000000000000000000045")
        self.assertEqual(build_music_mode(0x03, 0x63), expected)

    def test_music_rolling(self):
        packet = build_music_mode(0x06, 0x63)
        self.assertEqual(packet[:5], bytes([0x33, 0x05, 0x13, 0x06, 0x63]))

    def test_music_separation(self):
        expected = bytes.fromhex("3305133263000000000000000000000000000074")
        self.assertEqual(build_music_mode(0x32, 0x63), expected)

    def test_all_music_modes_valid(self):
        for name, mid in MUSIC_MODE_IDS.items():
            packet = build_music_mode(mid)
            self.assertEqual(len(packet), 20, f"Music {name} wrong length")
            self.assertEqual(
                xor_checksum(packet[:19]),
                packet[19],
                f"Music {name} bad checksum",
            )


class TestSegmentColor(unittest.TestCase):
    def test_all_segments(self):
        packet = build_segment_color(255, 0, 0, list(range(1, 16)))
        self.assertEqual(packet[12], 0xFF)  # low byte: segs 1-8
        self.assertEqual(packet[13], 0x7F)  # high byte: segs 9-15

    def test_single_segment_1(self):
        packet = build_segment_color(255, 0, 0, [1])
        self.assertEqual(packet[12], 0x01)
        self.assertEqual(packet[13], 0x00)

    def test_single_segment_8(self):
        packet = build_segment_color(255, 0, 0, [8])
        self.assertEqual(packet[12], 0x80)
        self.assertEqual(packet[13], 0x00)

    def test_single_segment_9(self):
        packet = build_segment_color(255, 0, 0, [9])
        self.assertEqual(packet[12], 0x00)
        self.assertEqual(packet[13], 0x01)

    def test_single_segment_15(self):
        packet = build_segment_color(255, 0, 0, [15])
        self.assertEqual(packet[12], 0x00)
        self.assertEqual(packet[13], 0x40)

    def test_segment_combination(self):
        packet = build_segment_color(255, 0, 0, [1, 9])
        self.assertEqual(packet[12], 0x01)
        self.assertEqual(packet[13], 0x01)

    def test_empty_segments(self):
        packet = build_segment_color(255, 0, 0, [])
        self.assertEqual(packet[12], 0x00)
        self.assertEqual(packet[13], 0x00)

    def test_segment_length(self):
        self.assertEqual(len(build_segment_color(255, 0, 0, [1, 5, 10])), 20)

    def test_segment_ignores_invalid(self):
        packet = build_segment_color(255, 0, 0, [0, 16, 20])
        self.assertEqual(packet[12], 0x00)
        self.assertEqual(packet[13], 0x00)


class TestStateQuery(unittest.TestCase):
    def test_state_query(self):
        expected = bytes.fromhex("AA010000000000000000000000000000000000AB")
        self.assertEqual(build_state_query(), expected)

    def test_keep_alive_same_as_query(self):
        self.assertEqual(build_keep_alive(), build_state_query())

    def test_state_query_length(self):
        self.assertEqual(len(build_state_query()), 20)


class TestKelvinToRGB(unittest.TestCase):
    def test_warm_1000k(self):
        r, g, b = kelvin_to_rgb(1000)
        self.assertEqual(r, 255)
        self.assertGreater(g, 0)

    def test_daylight_5500k(self):
        r, g, b = kelvin_to_rgb(5500)
        self.assertEqual(r, 255)
        self.assertGreater(g, 200)
        self.assertGreater(b, 200)

    def test_cool_10000k(self):
        _, _, b = kelvin_to_rgb(10000)
        self.assertGreater(b, 200)

    def test_output_range(self):
        for k in range(1000, 10001, 500):
            r, g, b = kelvin_to_rgb(k)
            self.assertTrue(0 <= r <= 255, f"R={r} out of range at {k}K")
            self.assertTrue(0 <= g <= 255, f"G={g} out of range at {k}K")
            self.assertTrue(0 <= b <= 255, f"B={b} out of range at {k}K")

    def test_clamp_low(self):
        r, g, b = kelvin_to_rgb(500)
        self.assertTrue(0 <= r <= 255)

    def test_clamp_high(self):
        r, g, b = kelvin_to_rgb(20000)
        self.assertTrue(0 <= r <= 255)


class TestAllPacketsLength(unittest.TestCase):
    """Verify every command builder produces exactly 20 bytes."""

    def test_power_on(self):
        self.assertEqual(len(build_power(True)), 20)

    def test_power_off(self):
        self.assertEqual(len(build_power(False)), 20)

    def test_brightness(self):
        self.assertEqual(len(build_brightness(50)), 20)

    def test_color_rgb(self):
        self.assertEqual(len(build_color_rgb(128, 128, 128)), 20)

    def test_color_simple(self):
        self.assertEqual(len(build_color_rgb_simple(128, 128, 128)), 20)

    def test_color_temp(self):
        self.assertEqual(len(build_color_temp(4000)), 20)

    def test_scene(self):
        self.assertEqual(len(build_scene(0x00)), 20)

    def test_music_mode(self):
        self.assertEqual(len(build_music_mode(0x03)), 20)

    def test_segment_color(self):
        self.assertEqual(len(build_segment_color(255, 0, 0, [1, 2, 3])), 20)

    def test_state_query(self):
        self.assertEqual(len(build_state_query()), 20)

    def test_keep_alive(self):
        self.assertEqual(len(build_keep_alive()), 20)


if __name__ == "__main__":
    unittest.main()
