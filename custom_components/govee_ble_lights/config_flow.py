"""Config flow for Govee BLE Lights."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfo
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_MODEL, DOMAIN, SUPPORTED_MODELS

# Pattern to extract model from BLE local name
MODEL_PATTERN = re.compile(r"(?:ihoment|Govee|GBK|GVH)_(H\w+)")


def _extract_model(name: str) -> str | None:
    """Extract model from BLE advertisement name."""
    match = MODEL_PATTERN.search(name)
    if match:
        model = match.group(1)
        # Strip trailing BLE suffix (e.g. H617A_ABCD -> H617A)
        for known in SUPPORTED_MODELS:
            if model.startswith(known):
                return known
    return None


class GoveeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Govee BLE Lights."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfo | None = None

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfo) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info

        model = _extract_model(discovery_info.name) or "H617A"
        self.context["title_placeholders"] = {"name": discovery_info.name}

        return self.async_create_entry(
            title=discovery_info.name,
            data={CONF_MODEL: model},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the user step for manual setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper().strip()
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"Govee {user_input[CONF_MODEL]}",
                data={CONF_MODEL: user_input[CONF_MODEL]},
            )

        model_list = list(SUPPORTED_MODELS.keys())
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Required(CONF_MODEL, default=model_list[0]): vol.In(model_list),
                }
            ),
            errors=errors,
        )
