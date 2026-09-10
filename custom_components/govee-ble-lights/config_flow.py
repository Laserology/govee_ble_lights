from __future__ import annotations

"""
Config flows for adding Govee BLE devices: Bluetooth discovery or manual.
"""

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_ADDRESS, CONF_MODEL, CONF_TYPE
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, CONF_TYPE_BLE
from .models import detect_model, get_available_models


class GoveeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery and manual configuration of Govee BLE lights."""

    # Version number for this configuration flow
    # Used to determine when configuration entries need to be migrated
    VERSION = 1

    def __init__(self) -> None:
        """Initialize the configuration flow state."""
        self._discovery_info: None = None
        self._discovered_devices: dict[str, str] = {}
        self._available_models: list[str] = []
        self._available_config_types: dict[str, str] = {
            CONF_TYPE_BLE: "BLE",
        }

    async def _async_load_models(self) -> None:
        """Load and cache the available model list from config.json."""
        # If models are already loaded, return early to avoid redundant work
        if self._available_models:
            return

        self._available_models = get_available_models()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Start configuring a discovered Bluetooth device: claim its address
        as the unique ID (aborting if already configured), then confirm.
        """
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """
        Confirm a discovered device and let the user pick its model.

        On submit, creates a config entry with the selected model.
        """
        # Load models if not already loaded
        await self._async_load_models()

        # Ensure we have discovery info (should always be set from previous step)
        assert self._discovery_info is not None

        # Get the discovered device name from discovery info
        discovery_info = self._discovery_info
        title = discovery_info.name

        # Handle form submission vs form display
        if user_input is not None:
            # User submitted the form - they selected a model
            model = user_input[CONF_MODEL]
            return self.async_create_entry(title=title, data={CONF_MODEL: model})

        # Prepare to show the confirmation form
        self._set_confirm_only()

        # Govee advertisements embed the model ID (e.g. "Govee_H617C_2482");
        # pre-select it in the dropdown when it matches a bundled model.
        detected = detect_model(discovery_info.name)
        placeholders = {"name": title, "model": detected or "Device model"}
        schema = {vol.Required(CONF_MODEL): vol.In(self._available_models)}
        if detected is not None:
            schema = {
                vol.Required(CONF_MODEL, default=detected): vol.In(
                    self._available_models
                )
            }

        self.context["title_placeholders"] = placeholders

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=placeholders,
            # Schema defines what input fields to show
            data_schema=vol.Schema(schema),
        )

    async def async_step_ble(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """
        Manually configure a device by selecting its address and model.

        Lists currently discovered BLE devices for the address dropdown;
        on submit, creates a config entry keyed by the device address.
        """
        # Load models if not already loaded
        await self._async_load_models()

        # Prepare for any errors
        errors = {}

        # Get currently configured entries to avoid duplicates
        current_addresses = self._async_current_ids()

        # Scan for all currently discovered BLE devices
        for discovery_info in async_discovered_service_info(self.hass, False):
            address = discovery_info.address

            # Skip devices we're already configuring or have discovered before
            if address in current_addresses or address in self._discovered_devices:
                continue

            # Store device name for the dropdown
            self._discovered_devices[address] = discovery_info.name

        # Handle form submission
        if (
            user_input is not None
            and CONF_ADDRESS in user_input
            and user_input[CONF_ADDRESS] is not None
            and CONF_MODEL in user_input
            and user_input[CONF_MODEL] is not None
        ):
            address = user_input[CONF_ADDRESS]
            model = user_input[CONF_MODEL]

            # Set unique ID to the device address (for manual entry)
            await self.async_set_unique_id(address, raise_on_progress=False)

            # Check if device is already configured at this address
            self._abort_if_unique_id_configured()

            # Create configuration entry with the device info
            return self.async_create_entry(
                title=self._discovered_devices[address], data={CONF_MODEL: model}
            )

        # No form submitted - show the form
        return self.async_show_form(
            step_id="ble",
            # Schema defines address dropdown and model dropdown
            data_schema=vol.Schema(
                {
                    # Dropdown of all currently discovered BLE devices
                    vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices),
                    # Dropdown of available Govee models
                    vol.Required(CONF_MODEL): vol.In(self._available_models),
                }
            ),
            # Error dict - empty unless validation fails
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """
        First manual step: pick the configuration type (BLE for now).

        Selecting BLE continues to async_step_ble.
        """
        # Handle form submission
        if user_input is not None and user_input[CONF_TYPE] == CONF_TYPE_BLE:
            # User selected BLE - continue to BLE configuration step
            return await self.async_step_ble(user_input)

        # No form submitted - show the initial user step form
        return self.async_show_form(
            step_id="user",
            # Schema defines configuration type dropdown
            data_schema=vol.Schema(
                {
                    # Dropdown of available configuration types
                    vol.Required(CONF_TYPE): vol.In(self._available_config_types),
                }
            ),
        )
