"""
Rendering of effect definitions into segment write commands.

Govee lights that support segments share the same input system: a packet can
paint an arbitrary *subset* of segments in one color by setting one bit per
segment in a two-byte mask. Effect definitions therefore only describe a
pattern (see ``models.py``), and this module resolves that pattern against a
concrete segment count into the minimal set of per-color writes needed.

Because most Govee firmware has no native fade, fading is done in software:
the light entity interpolates between per-segment color states and re-sends
frames quickly. This module provides the pure helpers for both concerns:

- ``segment_colors`` — resolve an effect definition to one color per segment.
- ``segments_to_writes`` — group per-segment colors into minimal mask packets.
- ``interpolate_segments`` — linear RGB interpolation between two states.

Each returned "write" is::

    {"color": (r, g, b), "mask_lo": int, "mask_hi": int}

where ``mask_lo`` addresses segments 1-8 (bit 0 = segment 1) and ``mask_hi``
addresses segments 9-15 (bit 0 = segment 9).
"""

from __future__ import annotations

import asyncio
import time

from .models import MAX_SEGMENT_COUNT


def segment_colors(
    effect: dict, segment_count: int, offset: int = 0
) -> list[list[int]]:
    """
    Resolve an effect definition into one color per segment (address).

    Palette colors map one-to-one to segments and the list repeats across
    them, so one definition works on any segment count.

    Args:
        effect: Effect definition dict. Currently supports:
            - colors: list of [r, g, b] values repeated across segments.
        segment_count: Number of segments on the target device (<= 15).
        offset: How many positions to shift the palette before applying it.
            Animation advances this each step to make the pattern move.

    Returns:
        list[list[int]]: A color per segment, in segment order.

    Raises:
        ValueError: If the effect definition is malformed.
    """
    colors = effect.get("colors")
    if not colors:
        raise ValueError("Effect has no 'colors' list")

    for raw_color in colors:
        valid = (
            len(raw_color) == 3
            and all(isinstance(channel, int) for channel in raw_color)
            and all(0 <= channel <= 255 for channel in raw_color)
        )
        if not valid:
            raise ValueError(f"Invalid color in effect: {raw_color}")

    count = min(segment_count, MAX_SEGMENT_COUNT)
    return [
        list(colors[(offset + segment) % len(colors)])
        for segment in range(count)
    ]


def segments_to_writes(segment_colors: list[list[int]]) -> list[dict]:
    """
    Group per-segment colors into the minimal set of mask packets.

    Segments painted in the same color share one write, so a pattern with few
    distinct colors needs few packets.

    Args:
        segment_colors: One color per segment, in segment order.

    Returns:
        list[dict]: One entry per distinct color, in order of first use, each
        with the color and the segment mask it should be painted to.
    """
    writes_by_color: dict[tuple[int, int, int], list[int]] = {}
    for segment, color in enumerate(segment_colors, start=1):
        key = tuple(color)
        mask = writes_by_color.setdefault(key, [0, 0])
        if segment <= 8:
            mask[0] |= 1 << (segment - 1)
        else:
            mask[1] |= 1 << (segment - 9)

    return [
        {"color": list(color), "mask_lo": mask[0], "mask_hi": mask[1]}
        for color, mask in writes_by_color.items()
    ]


def interpolate_segments(
    from_colors: list[list[int]], to_colors: list[list[int]], fraction: float
) -> list[list[int]]:
    """
    Linearly interpolate two per-segment color states.

    Args:
        from_colors: Starting per-segment colors.
        to_colors: Target per-segment colors (same length as from_colors).
        fraction: Position between states, 0.0 (from) to 1.0 (to).

    Returns:
        list[list[int]]: Interpolated per-segment colors, channel-wise.
    """
    fraction = max(0.0, min(1.0, fraction))

    result = []
    for from_color, to_color in zip(from_colors, to_colors):
        mixed = [
            round(source + (target - source) * fraction)
            for source, target in zip(from_color, to_color)
        ]
        result.append(mixed)
    return result


async def run_fade(
    start: list[list[int]],
    target: list[list[int]],
    fade: float,
    interval: float,
    send_frame,
    is_current,
) -> None:
    """
    Drive a wall-clock paced crossfade from *start* to *target*.

    The exact target is written as the *final frame of the window*, not after
    it: the loop reserves the measured write time of one frame so the target
    lands by ``fade``. An extra post-window write would stretch every
    transition to ``fade`` + one write latency - with ``fade`` equal to the
    animation step that pushed each step past its slot and the cadence
    visibly lagged. Frames are drawn against elapsed time so BLE write
    latency can neither stretch the fade nor let animation steps collide.

    Args:
        start: Starting per-segment colors.
        target: Target per-segment colors (same length as start).
        fade: Total fade duration in seconds (> 0).
        interval: Maximum spacing between frames.
        send_frame: Async callback painting one interpolated frame.
        is_current: Callable returning False once this fade was superseded;
            the fade then aborts (without writing the final target).
    """
    if fade <= 0:
        await send_frame(target)
        return

    begin = time.monotonic()
    last_write = 0.0
    while is_current():
        elapsed = time.monotonic() - begin
        # Not enough time left for another interpolated frame's write: the
        # exact target becomes the final frame, landing at roughly ``fade``.
        if elapsed + last_write >= fade:
            await send_frame(target)
            break
        fraction = elapsed / fade
        # The start state is already on the strip; writing it again is wasted
        # traffic on an already write-bound link.
        if fraction > 0:
            write_start = time.monotonic()
            await send_frame(interpolate_segments(start, target, fraction))
            last_write = time.monotonic() - write_start
        await asyncio.sleep(
            min(interval, max(0.0, fade - (time.monotonic() - begin)))
        )
