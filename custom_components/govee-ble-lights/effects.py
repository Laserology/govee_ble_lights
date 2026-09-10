"""
Rendering of effect definitions into segment write commands.

Segmented Govee strips accept one packet per color, with a bitmask selecting
which segments to paint (mask_lo = segments 1-8, mask_hi = 9-15). Effects are
pattern palettes; this module resolves them into per-segment frames and the
minimal per-color writes. Fading is done in software (firmware has no native
fade), so fades are driven here too:

- ``effect_target`` — one animation frame for any motion (shift/pulse/wipe).
- ``segments_to_writes`` — group per-segment colors into minimal mask packets.
- ``interpolate_segments`` / ``run_fade`` — software crossfades.

Each returned "write" is ``{"color": ..., "mask_lo": int, "mask_hi": int}``.
"""

from __future__ import annotations

import asyncio
import time

from .models import MAX_SEGMENT_COUNT


def _effect_colors(effect: dict) -> list[list[int]]:
    """Return the validated palette from an effect definition.

    Raises:
        ValueError: If the effect has no colors or any color is malformed.
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
    return colors


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
    colors = _effect_colors(effect)
    count = min(segment_count, MAX_SEGMENT_COUNT)
    return [
        list(colors[(offset + segment) % len(colors)])
        for segment in range(count)
    ]


def effect_target(
    effect: dict,
    segment_count: int,
    offset: int = 0,
    direction: int = 1,
) -> list[list[int]]:
    """
    One animation frame's per-segment colors for any motion type.

    ``motion`` selects the behaviour (default ``shift``):
    - ``shift``: pattern advances one segment per step (``direction=-1``
      reverses the travel).
    - ``pulse``: pattern stays put; brightness alternates full / ``pulse_low``
      (default 0.25) each step.
    - ``wipe``: pattern fills in from one end, one segment per step, then
      wraps back to the full strip; offset 0 paints the full (start) state.

    Args:
        effect: Effect definition dict.
        segment_count: Number of segments on the target device (<= 15).
        offset: Animation step counter.
        direction: 1 for forward travel, -1 for reverse.

    Raises:
        ValueError: If the effect definition is malformed.
    """
    count = min(segment_count, MAX_SEGMENT_COUNT)
    if count == 0:
        return []

    motion = effect.get("motion", "shift")

    if motion == "pulse":
        colors = _effect_colors(effect)
        low = float(effect.get("pulse_low", 0.25))
        factor = 1.0 if offset % 2 == 0 else low
        return [
            [round(channel * factor) for channel in colors[segment % len(colors)]]
            for segment in range(count)
        ]

    if motion == "wipe":
        colors = _effect_colors(effect)
        filled = offset % count or count
        result = []
        for segment in range(count):
            if direction > 0:
                active = segment < filled
            else:
                active = segment >= count - filled
            result.append(
                list(colors[segment % len(colors)]) if active else [0, 0, 0]
            )
        return result

    # shift (also the fallback for unknown motion values)
    shifted = offset if direction > 0 else -offset
    return segment_colors(effect, count, offset=shifted)


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
    Crossfade *start* to *target* over *fade* seconds, wall-clock paced.

    The exact target is the final frame of the window: one frame's measured
    write time is reserved so it lands by ``fade``, not ``fade`` + latency
    (which stretched animation cadence). Deadline pacing keeps BLE write
    latency from accumulating.

    Args:
        start: Starting per-segment colors.
        target: Target per-segment colors (same length as start).
        fade: Total fade duration in seconds (> 0).
        interval: Maximum spacing between frames.
        send_frame: Async callback painting one interpolated frame.
        is_current: Callable returning False once superseded; the fade then
            aborts without writing the final target.
    """
    if fade <= 0:
        await send_frame(target)
        return

    begin = time.monotonic()
    last_write = 0.0
    while is_current():
        elapsed = time.monotonic() - begin
        # Reserve one frame's write time so the exact target lands by ``fade``.
        if elapsed + last_write >= fade:
            await send_frame(target)
            break
        fraction = elapsed / fade
        # The start state is already on the strip; don't re-write it.
        if fraction > 0:
            write_start = time.monotonic()
            await send_frame(interpolate_segments(start, target, fraction))
            last_write = time.monotonic() - write_start
        await asyncio.sleep(
            min(interval, max(0.0, fade - (time.monotonic() - begin)))
        )
