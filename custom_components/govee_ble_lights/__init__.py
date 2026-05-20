from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import (CONF_MODEL, MAJOR_VERSION, MINOR_VERSION)
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["light"]

class Hub:
    def __init__(self, address: str = None, devices: list = None) -> None:
        """Init Govee dummy hub."""
        self.devices = devices
        self.address = address

UNIQUE_DEVICES = {}

def internal_unique_devices(uid: str, devices: list) -> list:
    """For support multiple integrations - bind each device to one integraion.
    To avoid duplicates.
    """
    return [
        device
        for device in devices
        if UNIQUE_DEVICES.setdefault(device["device"], uid) == uid
    ]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee BLE device from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    address = entry.unique_id
    assert address is not None
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Govee BLE device with address {address}"
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = Hub(address=address)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

async def async_setup(hass: HomeAssistant, _: dict) -> bool:
    if (MAJOR_VERSION, MINOR_VERSION) < (2026, 1):
        raise EnvironmentError("unsupported hass version, need at least 2026.1")

    # init storage for registries
    hass.data[DOMAIN] = {}
    return True
