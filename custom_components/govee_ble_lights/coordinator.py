"""DataUpdateCoordinator for Govee BLE Lights."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bleak import BleakClient, BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .protocol import WRITE_UUID

_LOGGER = logging.getLogger(__name__)

DISCONNECT_DELAY = 120  # seconds before auto-disconnect


class GoveeBLECoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages BLE connection lifecycle for a Govee device."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        model: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Govee {model} ({address})",
        )
        self.address = address
        self.model = model
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._disconnect_timer: asyncio.TimerHandle | None = None
        # Optimistic state
        self.is_on: bool = False
        self.brightness_pct: int = 100
        self.rgb_color: tuple[int, int, int] = (255, 255, 255)
        self.color_temp_kelvin: int | None = None
        self.effect: str | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Return optimistic state (BLE is write-only for most commands)."""
        return {
            "is_on": self.is_on,
            "brightness_pct": self.brightness_pct,
            "rgb_color": self.rgb_color,
            "color_temp_kelvin": self.color_temp_kelvin,
            "effect": self.effect,
        }

    async def _ensure_connected(self) -> BleakClient:
        """Ensure BLE connection is established."""
        if self._client and self._client.is_connected:
            self._reset_disconnect_timer()
            return self._client

        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if not ble_device:
            raise BleakError(f"Device {self.address} not found")

        self._client = await establish_connection(BleakClient, ble_device, self.address)
        self._reset_disconnect_timer()
        return self._client

    def _reset_disconnect_timer(self) -> None:
        """Reset the auto-disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._disconnect_timer = self.hass.loop.call_later(
            DISCONNECT_DELAY, lambda: self.hass.async_create_task(self.disconnect())
        )

    async def send_command(self, packet: bytes) -> None:
        """Send a BLE command with retry logic."""
        async with self._lock:
            for attempt in range(3):
                try:
                    client = await self._ensure_connected()
                    await client.write_gatt_char(WRITE_UUID, packet, response=False)
                    return
                except BleakError:
                    self._client = None
                    if attempt == 2:
                        _LOGGER.error(
                            "Failed to send command to %s after 3 attempts",
                            self.address,
                        )
                        raise
                    _LOGGER.debug(
                        "BLE command attempt %d failed for %s, retrying",
                        attempt + 1,
                        self.address,
                    )

    async def disconnect(self) -> None:
        """Disconnect from the BLE device."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except BleakError:
                _LOGGER.debug("Error disconnecting from %s", self.address)
            finally:
                self._client = None
