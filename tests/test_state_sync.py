"""Regression tests for effect/state sync and entity removal.

A color/segment packet repowers a Govee strip, so a running animation must stop
when the device reports itself off. Removal must release the device's single
BLE connection, or the light ignores commands until the host is restarted.
"""

import asyncio
import unittest

import _support

_support.ensure()

from govee_ble_lights.govee_ble import GoveeBLE  # noqa: E402
from govee_ble_lights.light import GoveeBluetoothLight  # noqa: E402
from homeassistant.components.light import EFFECT_OFF  # noqa: E402


class FakeClient:
    """Minimal BleakClient stand-in recording writes and disconnects."""

    def __init__(self):
        self.is_connected = True
        self.writes: list[bytes] = []
        self.disconnected = False

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.disconnected = True
        self.is_connected = False

    async def write_gatt_char(self, characteristic, frame, response):
        self.writes.append(bytes(frame))


class FakeHub:
    address = "AA:BB:CC:DD:EE:FF"


class FakeHass:
    is_stopping = False

    def async_create_background_task(self, target, name=None):
        target.close()
        return None


class FakeEntry:
    def __init__(self, model):
        self.data = {"model": model}


def make_light(model="H6053"):
    light = GoveeBluetoothLight(FakeHub(), None, FakeEntry(model))
    light.hass = FakeHass()
    client = FakeClient()
    light._client = client
    return light, client


def power_frame(on: bool) -> bytes:
    """A device-originated (REQUEST) power status frame."""
    return GoveeBLE.build_packet(
        GoveeBLE.LEDFrameType.REQUEST, GoveeBLE.LEDCommand.POWER, [0x01 if on else 0x00]
    )


class SpinTask:
    """A stand-in effect/animation task that never finishes on its own."""

    def __init__(self):
        self.task: asyncio.Task | None = None

    async def __aenter__(self):
        async def spin():
            while True:
                await asyncio.sleep(3600)

        self.task = asyncio.create_task(spin())
        return self.task

    async def __aexit__(self, *exc):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


class TestExternalPowerOff(unittest.IsolatedAsyncioTestCase):
    async def test_running_effect_stops_when_device_powers_off(self):
        light, _ = make_light()
        light._state = True
        light._current_effect = "Rainbow"

        async with SpinTask() as task:
            light._effect_task = task

            await light._process_notification(power_frame(False))

            self.assertFalse(light.is_on)
            self.assertEqual(light.effect, EFFECT_OFF)
            # The effect is remembered so a later bare turn-on can resume it.
            self.assertEqual(light._effect_before_off, "Rainbow")
            self.assertIsNone(light._effect_task)
            self.assertTrue(task.cancelled())

    async def test_power_off_without_effect_still_updates_state(self):
        light, _ = make_light()
        light._state = True

        await light._process_notification(power_frame(False))

        self.assertFalse(light.is_on)
        self.assertEqual(light.effect, EFFECT_OFF)

    async def test_repeated_power_off_is_idempotent(self):
        light, _ = make_light()
        light._state = True
        light._current_effect = "Rainbow"

        async with SpinTask() as task:
            light._effect_task = task

            await light._process_notification(power_frame(False))
            await light._process_notification(power_frame(False))

            self.assertFalse(light.is_on)
            self.assertEqual(light._effect_before_off, "Rainbow")


class TestRemoval(unittest.IsolatedAsyncioTestCase):
    async def test_removal_disconnects_and_cancels_background_tasks(self):
        light, client = make_light()

        async def spin():
            while True:
                await asyncio.sleep(3600)

        keepalive = asyncio.create_task(spin())
        connect = asyncio.create_task(spin())
        light._keepalive_task = keepalive
        light._connect_task = connect

        await light.async_will_remove_from_hass()

        self.assertTrue(client.disconnected)
        self.assertTrue(keepalive.cancelled())
        self.assertTrue(connect.cancelled())
        self.assertIsNone(light._keepalive_task)
        self.assertIsNone(light._connect_task)

    async def test_removal_without_connection_is_safe(self):
        light, _ = make_light()
        light._client = None

        await light.async_will_remove_from_hass()

        self.assertIsNone(light._keepalive_task)


if __name__ == "__main__":
    unittest.main()


def color_frame(red: int, green: int, blue: int) -> bytes:
    """A device-originated (REQUEST) solid color status frame."""
    return GoveeBLE.build_packet(
        GoveeBLE.LEDFrameType.REQUEST, GoveeBLE.LEDCommand.COLOR, [0x02, red, green, blue]
    )


class TestReportedColor(unittest.IsolatedAsyncioTestCase):
    async def test_black_color_report_keeps_the_last_color(self):
        # After a fade-off the device reports black; that must not replace the
        # color a later bare turn-on restores.
        light, _ = make_light("H6006")
        light._rgb_color = (10, 20, 30)
        light.async_write_ha_state = lambda: None

        await light._process_notification(color_frame(0, 0, 0))

        self.assertEqual(light._rgb_color, (10, 20, 30))

    async def test_real_color_report_is_adopted(self):
        light, _ = make_light("H6006")
        light.async_write_ha_state = lambda: None

        await light._process_notification(color_frame(10, 20, 30))

        self.assertEqual(light._rgb_color, (10, 20, 30))
