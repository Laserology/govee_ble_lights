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
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_EFFECT,
    EFFECT_OFF,
    LightEntityFeature,
    LightEntity,
    ColorMode)

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.storage import Store
import homeassistant.util.color as color_util
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

    if hub.devices is not None:
        devices = hub.devices
        for device in devices:
            if device['type'] == 'devices.types.light':
                _LOGGER.info("Adding device: %s", device)
                async_add_entities([GoveeAPILight(hub, device)])
    elif hub.address is not None:
        ble_device = bluetooth.async_ble_device_from_address(hass, hub.address.upper(), False)
        async_add_entities([GoveeBluetoothLight(hub, ble_device, config_entry)])

class GoveeAPILight(LightEntity, dict):
    _attr_color_mode = ColorMode.RGB

    def __init__(self, hub: Hub, device: dict) -> None:
        """Initialize an API light."""
        super().__init__()

        self.hub = hub

        self._state = None
        self._brightness = None

        self.device_data = device
        self.sku = self.device_data["sku"]
        self.device = self.device_data["device"]

        self._attr_name = device["deviceName"]

        color_modes: set[ColorMode] = set()

        for cap in device["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                color_modes.add(ColorMode.ONOFF)
            if cap['instance'] == 'brightness':
                color_modes.add(ColorMode.BRIGHTNESS)
            if cap['instance'] == 'colorTemperatureK':
                color_modes.add(ColorMode.COLOR_TEMP)
                self._attr_min_color_temp_kelvin = cap['parameters']['range']['min']
                self._attr_max_color_temp_kelvin = cap['parameters']['range']['max']
                self._attr_min_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_min_color_temp_kelvin)
                self._attr_max_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_max_color_temp_kelvin)
            if cap['instance'] == 'colorRgb':
                color_modes.add(ColorMode.RGB)
            if cap['instance'] == 'lightScene':
                self._attr_supported_features = LightEntityFeature(
                    LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
                )

        if ColorMode.ONOFF in color_modes:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
        if ColorMode.BRIGHTNESS in color_modes:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        if ColorMode.COLOR_TEMP in color_modes:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        if ColorMode.RGB in color_modes:
            self._attr_supported_color_modes = {ColorMode.RGB}

        self._state = None
        self._brightness = None
        self._rgb_color = None
        self.update_scenes()

    async def async_update(self):
        """Retrieve latest state."""
        _LOGGER.info("Updating device: %s", self.device_data)

        state = await self.hub.api.get_device_state(self.sku, self.device)
        for cap in state["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                self._state = cap['state']['value'] == 1
            if cap['instance'] == 'brightness':
                self._brightness = cap['state']['value']
            if cap['instance'] == 'colorTemperatureK':
                value = cap['state']['value']
                if value != 0:
                    self._attr_color_temp_kelvin = value
                    self._attr_color_temp = color_util.color_temperature_kelvin_to_mired(value)
            if cap['instance'] == 'colorRgb':
                num = cap['state']['value']
                self._attr_rgb_color = ((num >> 16) & 0xFF, (num >> 8) & 0xFF, num & 0xFF)

    async def update_scenes(self):
        if LightEntityFeature.EFFECT in self.supported_features:
            if self._attr_effect_list is None or len(self._attr_effect_list) == 0:
                _LOGGER.info("Updating device effects: %s", self.device_data)

                store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
                scenes = await self.hub.api.list_scenes(self.sku, self.device)

                await store.async_save(scenes)

                self._attr_effect_list = [scene['name'] for scene in scenes]

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self.device

    @property
    def brightness(self):
        return self._brightness

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._rgb_color

    @property
    def is_on(self) -> bool | None:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            await self.hub.api.set_brightness(self.sku, self.device, (brightness / 255) * 100)
            self._brightness = brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            await self.hub.api.set_color_rgb(self.sku, self.device, red, green, blue)

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            await self.hub.api.set_color_temp(self.sku, self.device, kelvin)

        if ATTR_EFFECT in kwargs:
            effect_name = kwargs.get(ATTR_EFFECT)
            store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
            scenes = (
                scene for scene in await store.async_load()
                if scene['name'] == effect_name
            )
            scene = next(scenes)
            _LOGGER.info("Set scene: %s", scene)
            await self.hub.api.set_scene(self.sku, self.device, scene['value'])

        await self.hub.api.toggle_power(self.sku, self.device, 1)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.api.toggle_power(self.sku, self.device, 0)
        self._state = False

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
        self._state = True
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
                [int(self._brightness * 100 / 255) if self._use_percent else self._brightness]) # Data

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
                asyncio.sleep(1)
                continue

        # Create a background task to keep the BLE conenction active
        # This helps remove the delay when turning on/off lights
        self.hass.async_create_background_task(
            # We pass client here sperately because it would be bad
            # to encourage accessing it directly. Thus we pass it explicitly.
            GoveeBLE.ensure_connection(self._client), "govee_ble_keepalive"
        )
