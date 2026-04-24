"""
This class represents Govee light entities.
It only contains the basic methods, and uses govee_ble to talk to govee devices.
"""

from __future__ import annotations

from pathlib import Path
import asyncio
import logging
import base64
import array
import json

from homeassistant.components import bluetooth
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_EFFECT,
    EFFECT_OFF,
    LightEntityFeature,
    LightEntity,
    ColorMode)

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
#from homeassistant.helpers.storage import Store
from homeassistant.core import HomeAssistant

from .govee_ble import GoveeBLE
from .const import DOMAIN
from . import Hub

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    if config_entry.entry_id in hass.data[DOMAIN]:
        hub: Hub = hass.data[DOMAIN][config_entry.entry_id]
    else:
        return

    if hub.address is not None:
        ble_device = bluetooth.async_ble_device_from_address(hass, hub.address.upper(), False)
        async_add_entities([GoveeBluetoothLight(hub, ble_device, config_entry)])

class GoveeBluetoothLight(LightEntity):
    _attr_supported_features = LightEntityFeature(LightEntityFeature.EFFECT)
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB

    _client = None

    def __init__(self, hub: Hub, ble_device, config_entry: ConfigEntry) -> None:
        """Initialize a bluetooth light."""

        # Initialize variables.
        self._mac = hub.address
        self._model = config_entry.data["model"]
        self._is_segmented = self._model in GoveeBLE.BLE_SEGMENTED_MODELS
        self._use_percent = self._model in GoveeBLE.BLE_PERCENT_MODELS
        self._ble_device = ble_device
        self._brightness = 255
        self._state = False
        self._rgb_color: tuple[int, int, int] | None = None
        self._current_effect: str | None = None
        self._effect_list: list[str] | None = None
        self._effect_map: dict[str, tuple] | None = None
        self._model_data: dict | None = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            name=self._model,
            manufacturer="Govee",
            model=self._model,
        )

    def _load_effect_list(self) -> list[str]:
        """Build the effect list from the JSON file. Runs in an executor thread."""
        self._model_data = json.loads(
            Path(Path(__file__).parent, "jsons", self._model + ".json").read_text()
        )
        self._effect_map = {}
        effect_list = []
        for categoryIdx, category in enumerate(self._model_data['data']['categories']):
            for sceneIdx, scene in enumerate(category['scenes']):
                for leffectIdx, lightEffect in enumerate(scene['lightEffects']):
                    for seffectIdx, specialEffect in enumerate(lightEffect['specialEffect']):
                        if 'supportSku' in specialEffect and self._model not in specialEffect['supportSku']:
                            continue
                        name = category['categoryName'] + " - " + scene['sceneName']
                        if lightEffect['scenceName']:
                            name += ' - ' + lightEffect['scenceName']
                        # Disambiguate duplicate names
                        unique_name = name
                        counter = 2
                        while unique_name in self._effect_map:
                            unique_name = f"{name} ({counter})"
                            counter += 1
                        self._effect_map[unique_name] = (categoryIdx, sceneIdx, leffectIdx, seffectIdx)
                        effect_list.append(unique_name)
        _LOGGER.debug("Loaded %d effects for model %s", len(effect_list), self._model)
        return effect_list

    async def async_added_to_hass(self) -> None:
        """Load effect list, query initial device state, and start background keepalive task."""
        _LOGGER.debug("Loading effect list for model %s", self._model)
        try:
            self._effect_list = await self.hass.async_add_executor_job(self._load_effect_list)
            _LOGGER.debug("Effect list loaded: %d effects", len(self._effect_list))
        except Exception as err:
            _LOGGER.error("Failed to load effect list for model %s: %s", self._model, err)

        # Create a background task to connect to the device.
        self.hass.async_create_background_task(
            self.try_connect(), "govee_ble_initialize"
        )

    @property
    def effect_list(self) -> list[str] | None:
        return self._effect_list

    @property
    def effect(self) -> str | None:
        """Return the current effect."""
        return self._current_effect

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return "GOVEE Light"

    @property
    def color_mode(self) -> ColorMode:
        """Return current color mode. BRIGHTNESS when an effect is active."""
        if self._current_effect and self._current_effect != EFFECT_OFF:
            return ColorMode.BRIGHTNESS
        return ColorMode.RGB

    @property
    def unique_id(self) -> str:
        """Return a unique, Home Assistant friendly identifier for this entity."""
        return self._mac.replace(":", "")

    @property
    def brightness(self):
        return self._brightness

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._rgb_color

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        if self._client is None:
            raise ConnectionError("This device has not been connected yet. Is it in range?")

        # Send power-on first, unless we're setting an effect (effect data should be loaded before activation)
        if ATTR_EFFECT not in kwargs:
            await GoveeBLE.send_single_packet(self._client, GoveeBLE.LEDCommand.POWER, [0x1])
            self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs.get(ATTR_BRIGHTNESS, 255)

            # Some models require a percentage instead of the raw value of a byte.
            await GoveeBLE.send_single_packet(
                self._client,
                GoveeBLE.LEDCommand.BRIGHTNESS, # Command
                [round(self._brightness * 100 / 255) if self._use_percent else self._brightness]) # Data

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)

            if self._is_segmented:
                await GoveeBLE.send_single_packet(
                    self._client,
                    GoveeBLE.LEDCommand.COLOR, # Command
                    [GoveeBLE.LEDMode.SEGMENTS, 0x01, red, green, blue, 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x7F]) # Data
            else:
                await GoveeBLE.send_single_packet(
                    self._client,
                    GoveeBLE.LEDCommand.COLOR, # Command
                    [GoveeBLE.LEDMode.MANUAL, red, green, blue]) # Data

            self._rgb_color = (red, green, blue)
            self._current_effect = EFFECT_OFF

        if ATTR_EFFECT in kwargs:
            effect = kwargs.get(ATTR_EFFECT)
            _LOGGER.debug("Effect requested: %r", effect)
            _LOGGER.debug("Effect map loaded: %s, size: %d", self._effect_map is not None, len(self._effect_map) if self._effect_map else 0)

            if not effect:
                _LOGGER.warning("Effect name is empty, skipping")
            elif not self._effect_map:
                _LOGGER.warning("Effect map is not loaded yet, skipping effect %r", effect)
            elif effect not in self._effect_map:
                _LOGGER.warning("Effect %r not found in effect map. Available: %s", effect, list(self._effect_map.keys())[:5])
            else:
                categoryIndex, sceneIndex, lightEffectIndex, specialEffectIndex = self._effect_map[effect]
                _LOGGER.debug("Effect %r maps to indexes: cat=%d scene=%d leffect=%d seffect=%d", effect, categoryIndex, sceneIndex, lightEffectIndex, specialEffectIndex)
                category = self._model_data['data']['categories'][categoryIndex]
                scene = category['scenes'][sceneIndex]
                lightEffect = scene['lightEffects'][lightEffectIndex]
                specialEffect = lightEffect['specialEffect'][specialEffectIndex]

                _LOGGER.debug("Sending effect scenceParam length: %d", len(specialEffect.get('scenceParam', '')))

                try:
                    await GoveeBLE.send_multi_packet(self._client, 0xa3,
                        array.array('B', [0x02]),
                        array.array('B', base64.b64decode(specialEffect['scenceParam'])))
                    _LOGGER.debug("Effect %r sent successfully, sending power-on", effect)
                    self._current_effect = effect
                    # Power-on after effect data so the device activates with the effect already loaded
                    await GoveeBLE.send_single_packet(self._client, GoveeBLE.LEDCommand.POWER, [0x1])
                except Exception as err:
                    _LOGGER.error("Failed to send effect %r: %s", effect, err)

    async def async_turn_off(self, **kwargs) -> None:
        if self._client is None:
            raise ConnectionError("This device has not been connected yet. Is it in range?")

        await GoveeBLE.send_single_packet(self._client, GoveeBLE.LEDCommand.POWER, [0x0])

        self._current_effect = EFFECT_OFF
        self._state = False

    async def _handle_notification(self, sender, data):
        self.hass.async_create_task(self._process_notification(bytes(data)))

    async def _process_notification(self, frame: bytes) -> None:
        try:
            head, cmd, payload = GoveeBLE.parse_frame(frame)
        except Exception:
            return

        if head != GoveeBLE.LEDFrameType.REQUEST: # Only process responses to state requests
            return

        if cmd == GoveeBLE.LEDCommand.POWER: # Update power state of device
            self._state = payload[0] == 0x01
            if not self._state:
                self._current_effect = EFFECT_OFF
        elif cmd == GoveeBLE.LEDCommand.BRIGHTNESS: # Update brightness of device (depending on segmented/percent model)
            self._brightness = round(payload[0] * 255 / 100) if self._use_percent else int(payload[0])
        elif cmd == GoveeBLE.LEDCommand.COLOR: # Update color of non-segmented device
            if len(payload) >= 4:
                self._rgb_color = (payload[1], payload[2], payload[3])
                self._current_effect = EFFECT_OFF
        elif cmd == GoveeBLE.LEDCommand.SEGMENT: # Update color of segmented device (only first segment, which is the one we control)
            if len(payload) >= 5:
                self._rgb_color = (payload[2], payload[3], payload[4])
                self._current_effect = EFFECT_OFF

        self.async_write_ha_state()

    async def _register_notifications(self) -> None:
        try:
            await self._client.start_notify(GoveeBLE.BLE_UUID_STATUS_CHARACTERISTIC, self._handle_notification) # Register response handler for when device responds to state requests or sends updates
        except Exception as err:
            _LOGGER.warning("Could not enable notifications for %s: %s", self.unique_id, err)

    async def _request_device_state(self) -> None:        
        try:
            await GoveeBLE.send_single_packet(self._client, GoveeBLE.LEDCommand.POWER, [], GoveeBLE.LEDFrameType.REQUEST) # Request power state of device
            await asyncio.sleep(0.05)

            await GoveeBLE.send_single_packet(self._client, GoveeBLE.LEDCommand.BRIGHTNESS, [], GoveeBLE.LEDFrameType.REQUEST) # Request brightness of device
            await asyncio.sleep(0.05)

            if self._is_segmented: # Request color of device
                await GoveeBLE.send_single_packet(self._client, GoveeBLE.LEDCommand.SEGMENT, [0x01], GoveeBLE.LEDFrameType.REQUEST)
            else:
                await GoveeBLE.send_single_packet(self._client, GoveeBLE.LEDCommand.COLOR, [], GoveeBLE.LEDFrameType.REQUEST)
        except Exception as err:
            _LOGGER.debug("Failed to request initial device state: %s", err)

    async def try_connect(self) -> None:
        """ Tries to start a connection to the device. """

        # Try connection to the device.
        while self._client is None:
            try:
                self._client = await GoveeBLE.establish_connection(
                    self._ble_device,
                    self.unique_id,
                    self.hass)
            except Exception:
                await asyncio.sleep(1)

        await self._register_notifications() # Register notifications which handles response of request device state
        await self._request_device_state() # Request the current device state

        # Create a background task to keep the BLE connection active
        # This helps remove the delay when turning on/off lights
        self.hass.async_create_background_task(
            # We pass client here sperately because it would be bad
            # to encourage accessing it directly. Thus we pass it explicitly.
            GoveeBLE.ensure_connection(self._client), "govee_ble_keepalive"
        )
