"""
Rendering of effect definitions into segment write commands.

Govee lights that support segments share the same input system: a packet can
paint an arbitrary *subset* of segments in one color by setting one bit per
segment in a two-byte mask. Effect definitions therefore only describe a
pattern (see ``models.py``), and this module resolves that pattern against a
concrete segment count into the minimal set of per-color writes needed.

Each returned "write" is::

    {"color": (r, g, b), "mask_lo": int, "mask_hi": int}

where ``mask_lo`` addresses segments 1-8 (bit 0 = segment 1) and ``mask_hi``
addresses segments 9-15 (bit 0 = segment 9).
"""

from __future__ import annotations

from .models import MAX_SEGMENT_COUNT


def render_pattern(effect: dict, segment_count: int, offset: int = 0) -> list[dict]:
    """
    Resolve a pattern effect into minimal per-color segment writes.

    Args:
        effect: Effect definition dict. Currently supports:
            - colors: list of [r, g, b] values repeated across segments.
        segment_count: Number of segments on the target device (<= 15).
        offset: How many positions to shift the palette before applying it.
            Animation advances this each step to make the pattern move.

    Returns:
        list[dict]: One entry per distinct color, in order of first use,
        each with the color and the segment mask it should be painted to.

    Raises:
        ValueError: If the effect definition is malformed.
    """
    colors = effect.get("colors")
    if not colors:
        raise ValueError("Effect has no 'colors' list")

    writes_by_color: dict[tuple[int, int, int], list[int]] = {}
    count = min(segment_count, MAX_SEGMENT_COUNT)

    for segment in range(1, count + 1):
        raw_color = colors[(offset + segment - 1) % len(colors)]
        if len(raw_color) != 3 or not all(
            isinstance(channel, int) and 0 <= channel <= 255 for channel in raw_color
        ):
            raise ValueError(f"Invalid color in effect: {raw_color}")

        color = tuple(raw_color)
        mask = writes_by_color.setdefault(color, [0, 0])
        if segment <= 8:
            mask[0] |= 1 << (segment - 1)
        else:
            mask[1] |= 1 << (segment - 9)

    return [
        {"color": color, "mask_lo": mask[0], "mask_hi": mask[1]}
        for color, mask in writes_by_color.items()
    ]
