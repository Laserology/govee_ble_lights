"""Regression tests for the fade pacing used by animated effects.

The entity animates by crossfading toward a new pattern every ``step``
seconds. Each fade must take exactly its declared duration regardless of BLE
write latency, or successive steps collide and the strip appears to jitter
and snap. These tests drive the real production helper (``effects.run_fade``)
with a mock writer whose per-frame cost simulates BLE write latency, and
assert the properties that held the original implementation (which slept
after every write, letting latency accumulate) would violate.
"""

import asyncio
import time
import unittest

import _support

_support.ensure()

from govee_ble_lights.effects import run_fade, interpolate_segments  # noqa: E402

INTERVAL = 1 / 30
START = [[255, 0, 0], [0, 255, 0], [0, 0, 255]] * 5
TARGET = [[0, 0, 255], [255, 0, 0], [0, 255, 0]] * 5
WRITE_LATENCY = 0.02  # simulated per-write BLE cost in seconds


class Writer:
    """Mock fade target: records frames and simulates per-write latency."""

    def __init__(self, latency: float):
        self.latency = latency
        self.frames: list[list[list[int]]] = []

    async def write(self, frame: list[list[int]]) -> None:
        self.frames.append(frame)
        await asyncio.sleep(self.latency)


class TestFadePacing(unittest.IsolatedAsyncioTestCase):
    async def test_fade_stays_within_its_window_under_write_latency(self):
        # Wall-clock pacing: total time is the fade window plus at most one
        # write's latency, no matter the per-frame cost. Naive
        # sleep-after-write pacing accumulates latency and takes well over
        # 1.0 + 0.15s here.
        writer = Writer(WRITE_LATENCY)
        fade = 1.0
        begin = time.monotonic()
        await run_fade(START, TARGET, fade, INTERVAL, writer.write, lambda: True)
        self.assertLess(time.monotonic() - begin, fade + 0.15)

    async def test_target_lands_by_the_fade_deadline(self):
        # The exact target is reserved as the final frame of the window, so a
        # fade == step transition finishes at its deadline instead of
        # ``fade`` + one write latency (which made animation steps lag their
        # configured cadence). Old behavior ended around 1.02-1.05s here.
        writer = Writer(WRITE_LATENCY)
        fade = 1.0
        begin = time.monotonic()
        await run_fade(START, TARGET, fade, INTERVAL, writer.write, lambda: True)
        self.assertLess(time.monotonic() - begin, fade + 0.04)
        self.assertEqual(writer.frames[-1], TARGET)

    async def test_writes_the_exact_target_last(self):
        writer = Writer(WRITE_LATENCY)
        await run_fade(START, TARGET, 1.0, INTERVAL, writer.write, lambda: True)
        self.assertEqual(writer.frames[-1], TARGET)

    async def test_frames_interpolate_toward_target(self):
        writer = Writer(WRITE_LATENCY)
        await run_fade(START, TARGET, 1.0, INTERVAL, writer.write, lambda: True)
        first = writer.frames[0]
        self.assertLess(_distance(first, START), _distance(first, TARGET))

    async def test_aborts_when_superseded(self):
        calls = 0
        writer = Writer(0.0)

        def superseded():
            nonlocal calls
            calls += 1
            return calls <= 2  # supersede after the second frame

        await run_fade(START, TARGET, 5.0, INTERVAL, writer.write, superseded)
        self.assertLess(len(writer.frames), 6)


def _distance(colors_a, colors_b):
    total = 0
    for a, b in zip(colors_a, colors_b):
        total += sum(abs(x - y) for x, y in zip(a, b))
    return total


if __name__ == "__main__":
    unittest.main()
