from __future__ import annotations

"""
Configuration flow for Govee BLE Lights integration.

This module handles the process of adding new Govee BLE devices to Home Assistant.
It manages device discovery via Bluetooth, user configuration input, and validation
before creating new configuration entries.

The config flow supports two main paths:
1. Bluetooth discovery - automatically detected BLE devices
2. Manual entry - user manually enters device address

"""

from typing import Any
from pathlib import Path

from homeassistant import config_entries
import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import (CONF_ADDRESS, CONF_MODEL, CONF_TYPE)
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, CONF_TYPE_BLE

class GoveeConfigFlow(ConfigFlow, domain=DOMAIN):
    """
    Main configuration flow class for adding Govee BLE lights.

    This class inherits from ConfigFlow and handles the step-by-step process
    of discovering and configuring Govee BLE devices. The flow supports:

    1. Bluetooth discovery: When a BLE device is detected, the user can
       select its model and add it to Home Assistant.

    2. Manual configuration: For devices not in range, users can manually
       enter the device address and model.

    Attributes:
        _config_type: Current configuration type being processed (BLE)
        _discovery_info: Bluetooth discovery information for the device
        _discovered_device: Current device name from discovery
        _discovered_devices: Dictionary of discovered devices and their names
        _available_models: List of available Govee light models
        _available_config_types: Dictionary of available configuration types
    """

    # Version number for this configuration flow
    # Used to determine when configuration entries need to be migrated
    VERSION = 1

    def __init__(self) -> None:
        """
        Initialize the configuration flow.

        Sets up all necessary instance variables for tracking the
        configuration flow state across multiple steps.

        Attributes:
            _config_type: Will store the current configuration type
            _discovery_info: Will store Bluetooth discovery info
            _discovered_device: Will store the discovered device name
            _discovered_devices: Will store discovered devices dictionary
            _available_models: Will store available Govee models
            _available_config_types: Will store available config types
        """
        self._config_type: str = ''
        self._discovery_info: None = None
        self._discovered_device: None = None
        self._discovered_devices: dict[str, str] = {}
        self._available_models: list[str] = []
        self._available_config_types: dict[str, str] = {
            CONF_TYPE_BLE: 'BLE',
        }

    async def _async_load_models(self) -> None:
        """
        Load available Govee light model names from bundled JSON files.

        This method asynchronously loads model information from JSON files
        stored in the 'jsons' directory. These files contain effect definitions
        and other model-specific data for different Govee light products.

        The loading is done using an executor to avoid blocking the Home
        Assistant main thread. Models are loaded once and cached in
        _available_models for subsequent use.

        Args:
            self: Configuration flow instance

        Returns:
            None - models are loaded into self._available_models
        """
        # If models are already loaded, return early to avoid redundant work
        if self._available_models:
            return

        # Get path of bundled JSON files.
        # The jsons directory is located alongside this config_flow.py file
        jsons_path = Path(Path(__file__).parent / "jsons")

        files = await self.hass.async_add_executor_job(lambda: list(jsons_path.iterdir()))
        self._available_models = sorted(f.name.replace(".json", "") for f in files)

    async def async_step_bluetooth(
            self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """
        Handle initial step of Bluetooth device discovery.

        This step is automatically triggered when Home Assistant detects
        a Govee BLE device via Bluetooth. The discovery_info contains
        information about the discovered device including its address and name.

        Args:
            self: Configuration flow instance
            discovery_info: Bluetooth discovery information containing:
                - address: Device MAC address
                - name: Device name from BLE advertisement
                - RSSI: Signal strength
                - and other Bluetooth characteristics

        Returns:
            FlowResult: Continues to bluetooth_confirm step

        The flow then calls async_step_bluetooth_confirm for user interaction.
        """
        # Set this device as the unique ID for this config entry
        # This ensures one config entry per discovered device
        await self.async_set_unique_id(discovery_info.address)

        # Abort if this device is already configured
        # Prevents duplicate config entries for the same device
        self._abort_if_unique_id_configured()

        # Store discovery info for later use in confirmation step
        self._discovery_info = discovery_info

        # Move to the confirmation step
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """
        Confirm Bluetooth device discovery and get user input.

        This step presents the user with a confirmation dialog showing:
        - The discovered device name
        - A dropdown to select the light model

        Args:
            self: Configuration flow instance
            user_input: Optional user input dict with model selection
                None means user hasn't submitted the form yet

        Returns:
            FlowResult: Either creates entry or shows form

        If user_input is provided (form submitted):
            - Extracts the selected model
            - Creates a new config entry with the model

        If no user_input (initial display):
            - Shows the confirmation form
            - Loads available models if not already done
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
            return self.async_create_entry(title=title, data={
                CONF_MODEL: model
            })

        # Prepare to show the confirmation form
        self._set_confirm_only()

        # Define placeholders for the confirmation dialog
        placeholders = {
            "name": title,
            "model": "Device model"
        }

        # TODO: We could potentially infer the light model based on BLE advertisement name
        # This would require reverse-engineering the BLE name format for each Govee model

        # Set title placeholders for the confirmation dialog
        self.context["title_placeholders"] = placeholders

        # Show the confirmation form with model selection dropdown
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=placeholders,
            # Schema defines what input fields to show
            data_schema=vol.Schema({
                # Dropdown of available Govee models
                vol.Required(CONF_MODEL): vol.In(self._available_models)
            }),
        )

    async def async_step_ble(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """
        Handle manual Bluetooth address configuration step.

        This step is used when users want to manually enter a device address
        for a Govee light that isn't currently discoverable via Bluetooth.

        Args:
            self: Configuration flow instance
            user_input: Optional user input dict with address and model
                None means user hasn't submitted the form yet

        Returns:
            FlowResult: Either creates entry or shows form

        If user_input is provided (form submitted):
            - Validates the address and model
            - Creates a new config entry
            - Sets the unique ID to the device address

        If no user_input (initial display):
            - Scans for available BLE devices
            - Shows form with discovered devices and models
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
            self._discovered_devices[address] = (discovery_info.name)

        # Handle form submission
        if (user_input is not None and CONF_ADDRESS in user_input and user_input[CONF_ADDRESS] is not None
                and CONF_MODEL in user_input and user_input[CONF_MODEL] is not None):
            address = user_input[CONF_ADDRESS]
            model = user_input[CONF_MODEL]

            # Set unique ID to the device address (for manual entry)
            await self.async_set_unique_id(address, raise_on_progress=False)

            # Check if device is already configured at this address
            self._abort_if_unique_id_configured()

            # Create configuration entry with the device info
            return self.async_create_entry(
                title=self._discovered_devices[address], data={
                    CONF_MODEL: model
                }
            )

        # No form submitted - show the form
        return self.async_show_form(
            step_id="ble",
            # Schema defines address dropdown and model dropdown
            data_schema=vol.Schema({
                # Dropdown of all currently discovered BLE devices
                vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices),
                # Dropdown of available Govee models
                vol.Required(CONF_MODEL): vol.In(self._available_models)
            }),
            # Error dict - empty unless validation fails
            errors=errors
        )

    async def async_step_user(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """
        Handle initial user-triggered configuration step.

        This is the first step when a user manually initiates adding a Govee
        light via Home Assistant's integrations menu. The user can select
        the configuration type (currently only BLE is supported).

        Args:
            self: Configuration flow instance
            user_input: Optional user input dict with configuration type
                None means user hasn't submitted the form yet

        Returns:
            FlowResult: Continues to next step or shows form

        If user selects BLE configuration type:
            - Moves to async_step_ble for manual address entry

        If no user_input (initial display):
            - Shows initial form with configuration type dropdown
        """
        # Handle form submission
        if user_input is not None and user_input[CONF_TYPE] == CONF_TYPE_BLE:
            # User selected BLE - continue to BLE configuration step
            return await self.async_step_ble(user_input)

        # No form submitted - show the initial user step form
        return self.async_show_form(
            step_id="user",
            # Schema defines configuration type dropdown
            data_schema=vol.Schema({
                # Dropdown of available configuration types
                vol.Required(CONF_TYPE): vol.In(self._available_config_types),
            }),
        )
