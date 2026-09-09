"""
Segment layout descriptors.

Most segmented models expose one addressable segment per visible band (the
``sequential`` layout: address ``k`` shows the palette color at position
``k`` directly). Some strips (e.g. H617C) show *two* visible bands per
address, where the second band is a blend with the neighbouring address.

Whether an address maps to one or two visible bands is a property of the
*model*, not of each effect, so effect palettes are always written in visible
terms and :func:`expand_palette` turns them into address-space colors for the
model's layout:

- ``sequential``: each palette color occupies one address.
- ``blended``: each palette color is stretched over two addresses. On these
  strips adjacent address LEDs overlap optically, so painting two matching
  addresses per palette entry makes the unavoidable blend bands read as the
  same color, producing clean wide bands instead of a half-blended mess.

:func:`visible_zones` predicts what a set of address colors will look like on
a layout; it exists for tests and debugging. Playback only needs
:func:`expand_palette`.
"""

from __future__ import annotations

SEQUENTIAL = "sequential"
BLENDED = "blended"

# Number of addresses each palette color occupies, per layout.
_ADDRESSES_PER_COLOR = {
    SEQUENTIAL: 1,
    BLENDED: 2,
}


def expand_palette(layout: str, colors: list[list[int]]) -> list[list[int]]:
    """
    Expand a visible-space palette into address-space colors.

    Args:
        layout: Layout name (see :data:`SEQUENTIAL`, :data:`BLENDED`).
        colors: Palette, one [r, g, b] per intended visible band.

    Returns:
        list[list[int]]: Colors in address order; never mutates *colors*.
    """
    repeat = _ADDRESSES_PER_COLOR.get(layout, 1)
    expanded = []
    for color in colors:
        expanded.extend([list(color)] * repeat)
    return expanded


def blend_color(first: list[int], second: list[int]) -> list[int]:
    """Channel-wise average of two colors (approximate LED optical blend)."""
    return [round((first[i] + second[i]) / 2) for i in range(3)]


def visible_zones(layout: str, address_colors: list[list[int]]) -> list[list[int]]:
    """
    Predict the visible color sequence for a set of address colors.

    For ``blended`` strips each address produces one pure band plus one band
    blended with the following address (wrapping around the end). For any
    other layout the visible sequence equals the address colors.

    Args:
        layout: Layout name.
        address_colors: One color per address, in address order.

    Returns:
        list[list[int]]: Colors in visible-zone order.
    """
    if layout != BLENDED:
        return [list(color) for color in address_colors]

    zones = []
    count = len(address_colors)
    for index, color in enumerate(address_colors):
        zones.append(list(color))
        neighbour = (
            address_colors[index + 1] if index + 1 < count else address_colors[0]
        )
        zones.append(blend_color(color, neighbour))
    return zones
