"""
Helpers for reading bundled device model metadata.

All model-specific data shipped with this integration lives in the single
``config.json`` file next to this module. Nothing else in the codebase should
hard-code model IDs or per-model behaviour; import these helpers instead so
the config file stays the one source of truth.

Schema (config.json)
--------------------

.. code-block:: json

    {
      "devices": {
        "H6006": {},
        "H6053": { "segmented": true, "segments": 12 },
        "H613A": { "brightness_percent": true },
        "H6199": { "segmented": true, "brightness_percent": true }
      },
      "fade": 1.0,
      "effects": {
        "Warm Christmas": { "colors": [[255, 0, 0], [255, 132, 43]] }
      }
    }

Per-model options:

- ``segmented``: device has individually addressable LED segments and needs
  segment-aware color commands.
- ``segments``: number of individually addressable segments (only meaningful
  for segmented models). Defaults to :data:`DEFAULT_SEGMENT_COUNT`.
- ``brightness_percent``: device expects brightness as a percentage (0-100)
  instead of a raw byte (0-255).
- ``effects``: extra effect definitions for this model (added on top of the
  shared effects).
- ``effects_file``: path, relative to the component directory, of a file
  containing extra effect definitions for this model (for definitions large
  enough that they would bloat ``config.json``).

Top-level options:

- ``fade``: default crossfade duration in seconds for color changes (and
  effect frame transitions, unless the effect overrides ``fade``). Device
  firmware has no native fading, so colors are interpolated in software by
  sending intermediate frames quickly. Defaults to 0 (instant).
- ``fade_on``: crossfade duration in seconds when the light powers on
  (ramps up from off/black to the requested color). Defaults to 0.
- ``fade_off``: crossfade duration in seconds when the light powers off
  (fades to black before switching off). Defaults to 0.

Effect definitions
------------------

Shared effects are defined once under the top-level ``effects`` key and are
available to every model that can play them (segmented models). Because the
definitions only describe *patterns*, they work on any segment count; the
per-model ``segments`` value is what makes a pattern concrete for a device.

Current format (static or animated segment pattern):

.. code-block:: json

    "Warm Christmas": {
      "colors": [[255, 0, 0], [255, 132, 43]],
      "step": 1.0,
      "fade": 0.9
    }

Segment ``n`` of the device is set to
``colors[(n - 1) % len(colors)]``, so a two-color list produces alternating
segments regardless of how many segments the model has. The optional ``step``
(seconds) makes the effect animated: every ``step`` the pattern shifts by one
segment, which looks like the colors moving along the strip. Without ``step``
the effect is static. The optional ``fade`` (seconds) crossfades between
consecutive frames in software instead of jumping; set it to ``step - 0.1``
so each transition completes with a safe margin before the next shift
(``fade: 0`` steps crisply). ``fade == step`` runs back-to-back transitions
that never settle and read as choppy on write-bound BLE links. Animated/
moving effects can be extended later with new keys on the same definition
object.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_COMPONENT_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _COMPONENT_DIR / "config.json"

# Fallback used when a segmented model does not declare a segment count. The
# segment bitmask in the protocol covers 15 segments.
DEFAULT_SEGMENT_COUNT = 15

# Largest addressable segment count supported by the mask protocol.
MAX_SEGMENT_COUNT = 15


@lru_cache(maxsize=1)
def _load_config() -> dict:
    """Read and cache the bundled ``config.json``."""
    with _CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def get_available_models() -> list[str]:
    """Return the sorted list of model IDs this integration supports."""
    return sorted(_load_config()["devices"])


def _model_config(model: str) -> dict:
    """Return the raw per-model entry from the config file (never raises)."""
    return _load_config()["devices"].get(model, {})


def is_segmented_model(model: str) -> bool:
    """Return True when *model* uses individually addressable segments."""
    return bool(_model_config(model).get("segmented", False))


def uses_percent_brightness(model: str) -> bool:
    """Return True when *model* expects brightness as a percentage."""
    return bool(_model_config(model).get("brightness_percent", False))


def get_segment_count(model: str) -> int:
    """Return the number of addressable segments for *model*.

    Defaults to :data:`DEFAULT_SEGMENT_COUNT` when the model does not declare
    one and is capped at :data:`MAX_SEGMENT_COUNT`, the protocol limit.
    """
    return min(
        int(_model_config(model).get("segments", DEFAULT_SEGMENT_COUNT)),
        MAX_SEGMENT_COUNT,
    )


def get_effects() -> dict[str, dict]:
    """Return the shared effect definitions (top-level ``effects`` key)."""
    return _load_config().get("effects", {})


def get_default_fade() -> float:
    """Return the default crossfade duration in seconds (top-level ``fade``)."""
    return float(_load_config().get("fade", 0.0))


def get_fade_on() -> float:
    """Return the power-on crossfade duration in seconds (top-level ``fade_on``)."""
    return float(_load_config().get("fade_on", 0.0))


def get_fade_off() -> float:
    """Return the power-off crossfade duration in seconds (top-level ``fade_off``)."""
    return float(_load_config().get("fade_off", 0.0))


def get_model_effects(model: str) -> dict[str, dict]:
    """Return the effect definitions available to *model*.

    The shared top-level effects are merged with any model-specific
    definitions declared inline (``effects``) or in an external file
    (``effects_file``); model-specific definitions override shared ones with
    the same name. Returns an empty dict when the model has no effects.

    Effect *playback* additionally requires the model to support the effect
    kind; callers decide that from the model's capabilities.
    """
    effects = dict(get_effects())
    effects.update(_model_config(model).get("effects", {}))

    effects_file = _model_config(model).get("effects_file")
    if effects_file:
        path = _COMPONENT_DIR / effects_file
        with path.open("r", encoding="utf-8") as file:
            effects.update(json.load(file))

    return effects
