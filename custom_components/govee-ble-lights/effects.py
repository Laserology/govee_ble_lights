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

from .layouts import SEQUENTIAL, expand_palette
from .models import MAX_SEGMENT_COUNT


def segment_colors(
    effect: dict, segment_count: int, offset: int = 0, layout: str = SEQUENTIAL
) -> list[list[int]]:
    """
    Resolve an effect definition into one color per segment (address).

    The palette is first expanded for the model's segment ``layout`` (see
    ``layouts.py``), so effect definitions always stay in visible terms and
    blended strips get the address stretching for free.

    Args:
        effect: Effect definition dict. Currently supports:
            - colors: list of [r, g, b] values repeated across segments.
        segment_count: Number of segments on the target device (<= 15).
        offset: How many positions to shift the palette before applying it.
            Animation advances this each step to make the pattern move.
        layout: Segment layout name used to expand the palette.

    Returns:
        list[list[int]]: A color per segment, in segment order.

    Raises:
        ValueError: If the effect definition is malformed.
    """
    colors = effect.get("colors")
    if not colors:
        raise ValueError("Effect has no 'colors' list")
    for raw_color in colors:
        if len(raw_color) != 3 or not all(
            isinstance(channel, int) and 0 <= channel <= 255 for channel in raw_color
        ):
            raise ValueError(f"Invalid color in effect: {raw_color}")

    palette = expand_palette(layout, colors)
    count = min(segment_count, MAX_SEGMENT_COUNT)
    return [
        list(palette[(offset + segment) % len(palette)]) for segment in range(count)
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


def render_pattern(
    effect: dict, segment_count: int, offset: int = 0, layout: str = SEQUENTIAL
) -> list[dict]:
    """
    Resolve an effect definition into per-color segment writes.

    Convenience wrapper equivalent to
    ``segments_to_writes(segment_colors(effect, segment_count, offset, layout))``.

    Args:
        effect: Effect definition dict (see :func:`segment_colors`).
        segment_count: Number of segments on the target device (<= 15).
        offset: Palette shift used by animations.
        layout: Segment layout name used to expand the palette.

    Returns:
        list[dict]: Minimal per-color writes (see :func:`segments_to_writes`).

    Raises:
        ValueError: If the effect definition is malformed.
    """
    return segments_to_writes(
        segment_colors(effect, segment_count, offset, layout=layout)
    )


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
    return [
        [round(source + (target - source) * fraction) for source, target in zip(f, t)]
        for f, t in zip(from_colors, to_colors)
    ]
