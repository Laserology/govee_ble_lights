"""Turn-on tests for the light entity.

A bare ``light.turn_on`` (no color/effect) must always paint, so the strip never
powers on dark, and effect resume must track the effect actually in use.
"""

import unittest

import _support

_support.ensure()

from govee_ble_lights.govee_ble import GoveeBLE  # noqa: E402
from govee_ble_lights.light import GoveeBluetoothLight  # noqa: E402
from govee_ble_lights.models import get_segment_count  # noqa: E402
from homeassistant.components.light import EFFECT_OFF  # noqa: E402


class FakeClient:
    """Minimal BleakClient stand-in recording written frames."""

    def __init__(self):
        self.is_connected = True
        self.writes: list[bytes] = []

    async def connect(self):
        self.is_connected = True

    async def write_gatt_char(self, characteristic, frame, response):
        self.writes.append(bytes(frame))


class FakeHub:
    address = "AA:BB:CC:DD:EE:FF"


class FakeHass:
    """Minimal HomeAssistant stand-in for spawning effect tasks."""

    is_stopping = False

    def __init__(self):
        self.tasks: list[str | None] = []

    def async_create_background_task(self, target, name=None):
        self.tasks.append(name)
        target.close()  # never scheduled; close it to avoid a warning
        return None


class FakeEntry:
    def __init__(self, model):
        self.data = {"model": model}


def make_light(model="H6006"):
    """Return a light wired to a recording client (no HA runtime needed)."""
    light = GoveeBluetoothLight(FakeHub(), None, FakeEntry(model))
    client = FakeClient()
    light._client = client
    return light, client


def commands(client):
    return [GoveeBLE.parse_frame(frame)[1] for frame in client.writes]


class TestBareTurnOn(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_state_paints_a_default_color(self):
        # No color learned (e.g. right after a restart): must still emit.
        light, client = make_light()

        await light.async_turn_on(transition=0)

        self.assertIn(GoveeBLE.LEDCommand.POWER, commands(client))
        self.assertIn(GoveeBLE.LEDCommand.COLOR, commands(client))
        _, cmd, payload = GoveeBLE.parse_frame(client.writes[-1])
        self.assertEqual(cmd, GoveeBLE.LEDCommand.COLOR)
        self.assertEqual(list(payload[1:4]), [255, 255, 255])
        self.assertTrue(light.is_on)

    async def test_known_color_is_repainted_not_defaulted(self):
        light, client = make_light()
        light._rgb_color = (10, 20, 30)
        light._segment_state = [[10, 20, 30]]

        await light.async_turn_on(transition=0)

        _, cmd, payload = GoveeBLE.parse_frame(client.writes[-1])
        self.assertEqual(cmd, GoveeBLE.LEDCommand.COLOR)
        self.assertEqual(list(payload[1:4]), [10, 20, 30])

    async def test_black_tracked_pattern_is_treated_as_unknown(self):
        # An interrupted fade-off can track an all-black pattern: repaint it.
        light, client = make_light()
        light._state = True
        light._segment_state = [[0, 0, 0]]

        await light.async_turn_on(transition=0)

        self.assertIn(GoveeBLE.LEDCommand.COLOR, commands(client))

    async def test_black_reported_color_is_treated_as_unknown(self):
        # A fade-off leaves the device black; if that color was learned (e.g.
        # from a state read after a reconnect), a bare turn-on must not paint it.
        light, client = make_light()
        light._rgb_color = (0, 0, 0)

        await light.async_turn_on(transition=0)

        _, cmd, payload = GoveeBLE.parse_frame(client.writes[-1])
        self.assertEqual(cmd, GoveeBLE.LEDCommand.COLOR)
        self.assertEqual(list(payload[1:4]), [255, 255, 255])

    async def test_brightness_only_change_while_on_does_not_repaint(self):
        # A slider move must not flash the strip from black.
        light, client = make_light()
        light._state = True
        light._rgb_color = (10, 20, 30)
        light._segment_state = [[10, 20, 30]]

        await light.async_turn_on(brightness=100, transition=0)

        self.assertNotIn(GoveeBLE.LEDCommand.COLOR, commands(client))
        self.assertIn(GoveeBLE.LEDCommand.BRIGHTNESS, commands(client))

    async def test_segmented_model_paints_every_segment(self):
        light, client = make_light("H6053")

        await light.async_turn_on(transition=0)

        self.assertEqual(len(light._resume_target()), get_segment_count("H6053"))
        self.assertIn(GoveeBLE.LEDCommand.COLOR, commands(client))


class TestEffectResume(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_color_drops_the_pending_effect(self):
        light, client = make_light("H6053")
        segments = get_segment_count("H6053")
        light._state = False
        light._effect_before_off = sorted(light._effects)[0]
        light._rgb_color = (10, 20, 30)
        light._segment_state = [[10, 20, 30]] * segments

        await light.async_turn_on(rgb_color=(1, 2, 3), transition=0)

        self.assertIsNone(light._effect_before_off)

        # The device powers off outside HA, then a bare turn-on arrives: keep
        # the chosen color, not the effect.
        light._state = False
        await light.async_turn_on(transition=0)

        self.assertEqual(light.effect, EFFECT_OFF)
        _, cmd, payload = GoveeBLE.parse_frame(client.writes[-1])
        self.assertEqual(cmd, GoveeBLE.LEDCommand.COLOR)
        self.assertEqual(list(payload[2:5]), [1, 2, 3])

    async def test_bare_turn_on_still_resumes_the_pending_effect(self):
        light, _ = make_light("H6053")
        light.hass = FakeHass()
        effect = sorted(light._effects)[0]
        light._state = False
        light._effect_before_off = effect

        await light.async_turn_on(transition=0)

        self.assertEqual(light.effect, effect)
        self.assertIsNone(light._effect_before_off)

    async def test_switching_effects_updates_the_resume_target(self):
        light, _ = make_light("H6053")
        light.hass = FakeHass()
        first, second = sorted(light._effects)[:2]
        light._state = True
        light._effect_before_off = first

        await light.async_turn_on(effect=second, transition=0)

        self.assertEqual(light.effect, second)
        self.assertEqual(light._effect_before_off, second)


if __name__ == "__main__":
    unittest.main()
