"""HA diagnostics support: the "Download diagnostics" button per entry.

HA discovers this module by name; no manifest or setup wiring needed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import models
from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one config entry."""
    hub = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if hub is None:
        return {"error": "integration not set up"}

    model = entry.data.get("model", "unknown")

    # The light entity tags itself with govee_model; collect its live state.
    light_state = next(
        (
            state.as_dict()
            for state in hass.states.async_all()
            if state.attributes.get("govee_model") == model
        ),
        None,
    )

    return {
        "address": hub.address,
        "model": {
            "id": model,
            "segmented": models.is_segmented_model(model),
            "segments": models.get_segment_count(model),
            "percent_brightness": models.uses_percent_brightness(model),
            "bundled": model in models.get_available_models(),
        },
        "light": light_state,
    }