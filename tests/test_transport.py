"""Tests for the BLE transport layer: packet building and write serialization.

The production fix for choppy animation was to make every packet go through
one per-connection write lock, so a whole rendering frame (one write per
palette color) is sent atomically and the keepalive ping can never interleave
between its packets. These tests drive the real ``govee_ble.GoveeBLE`` code
against a fake client and assert those properties.
"""

import asyncio
import unittest
from unittest.mock import patch

import _support

_support.ensure()

from govee_ble_lights.govee_ble import GoveeBLE  # noqa: E402


class FakeClient:
    """Minimal BleakClient stand-in recording writes; optionally gated."""

    def __init__(self, gate=None):
        self.is_connected = True
        self.writes: list[bytes] = []
        self.gate = gate

    async def connect(self):
        self.is_connected = True

    async def write_gatt_char(self, characteristic, frame, response):
        if self.gate is not None:
            await self.gate.wait()
        self.writes.append(bytes(frame))


class TestBuildPacket(unittest.TestCase):
    def test_layout_and_checksum(self):
        packet = GoveeBLE.build_packet(
            GoveeBLE.LEDFrameType.COMMAND, GoveeBLE.LEDCommand.POWER, [0x01]
        )
        self.assertEqual(len(packet), 20)
        self.assertEqual(packet[0], 0x33)  # COMMAND frame type
        self.assertEqual(packet[1], 0x01)  # POWER command
        self.assertEqual(packet[2], 0x01)  # payload
        self.assertEqual(packet[3:19], bytes(16))  # zero padding
        self.assertTrue(GoveeBLE.verify_frame(packet))

    def test_keepalive_frame_is_all_zeros(self):
        packet = GoveeBLE.build_packet(GoveeBLE.LEDFrameType.REQUEST, 0, [])
        self.assertEqual(packet[0], 0xAA)
        self.assertEqual(packet[1:19], bytes(18))
        self.assertTrue(GoveeBLE.verify_frame(packet))

    def test_rejects_oversized_payload(self):
        with self.assertRaises(ValueError):
            GoveeBLE.build_packet(0x33, 0x05, list(range(18)))


class TestSegmentPacket(unittest.TestCase):
    def test_segment_packet_layout(self):
        packet = GoveeBLE.build_segment_packet((255, 0, 0), 0x0F, 0x00)
        self.assertEqual(len(packet), 20)
        self.assertTrue(GoveeBLE.verify_frame(packet))
        self.assertEqual(packet[0], 0x33)
        self.assertEqual(packet[1], 0x05)  # COLOR command
        self.assertEqual(packet[2], 0x15)  # SEGMENTS mode
        self.assertEqual(packet[3], 0x01)  # segment color mode
        self.assertEqual(packet[4:7], bytes([255, 0, 0]))
        self.assertEqual(packet[12], 0x0F)  # mask_lo (segments 1-8)
        self.assertEqual(packet[13], 0x00)  # mask_hi (segments 9-15)

    def test_color_packet_layout(self):
        packet = GoveeBLE.build_color_packet(10, 20, 30)
        self.assertEqual(packet[0], 0x33)
        self.assertEqual(packet[1], 0x05)
        self.assertEqual(packet[2], 0x02)  # MANUAL mode
        self.assertEqual(packet[3:6], bytes([10, 20, 30]))


class TestWriteSerialization(unittest.IsolatedAsyncioTestCase):
    async def test_send_writes_sends_all_frames_in_order(self):
        client = FakeClient()
        packets = [
            GoveeBLE.build_packet(0x33, 0x01, [0x01]),
            GoveeBLE.build_packet(0x33, 0x05, [2, 3, 4]),
        ]
        await GoveeBLE.send_writes(client, packets)
        self.assertEqual(client.writes, packets)

    async def test_single_frame_cannot_interleave_between_frame_packets(self):
        # send_writes holds the write lock for the whole frame; a concurrent
        # single-frame write must land after it, never between its packets.
        gate = asyncio.Event()
        client = FakeClient(gate=gate)
        p1 = GoveeBLE.build_packet(0x33, 0x05, [1, 2, 3])
        p2 = GoveeBLE.build_packet(0x33, 0x05, [4, 5, 6])
        keeper = GoveeBLE.build_packet(0xAA, 0, [])

        frame_task = asyncio.create_task(GoveeBLE.send_writes(client, [p1, p2]))
        await asyncio.sleep(0)  # let frame_task acquire the lock and hit the gate
        racer = asyncio.create_task(GoveeBLE.send_single_frame(client, keeper))
        await asyncio.sleep(0)  # racer blocks on the lock
        gate.set()
        await asyncio.gather(frame_task, racer)

        self.assertEqual(client.writes, [p1, p2, keeper])


class TestKeepaliveDue(unittest.IsolatedAsyncioTestCase):
    async def test_due_when_nothing_written(self):
        self.assertTrue(GoveeBLE.keepalive_due(FakeClient()))

    async def test_not_due_after_recent_write(self):
        client = FakeClient()
        await GoveeBLE.send_writes(
            client, [GoveeBLE.build_packet(0x33, 0x05, [1, 2, 3])]
        )
        self.assertFalse(GoveeBLE.keepalive_due(client))

    async def test_due_again_after_interval_elapses(self):
        client = FakeClient()
        await GoveeBLE.send_writes(
            client, [GoveeBLE.build_packet(0x33, 0x05, [1, 2, 3])]
        )
        with patch.object(GoveeBLE, "BLE_KEEPALIVE_INTERVAL", 0.01):
            await asyncio.sleep(0.02)
            self.assertTrue(GoveeBLE.keepalive_due(client))


if __name__ == "__main__":
    unittest.main()