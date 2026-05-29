"""
This class represents Govee light entities.
It only contains the basic methods, and uses govee_ble to talk to govee devices.

This module implements Home Assistant light entities that control Govee BLE lights.
It provides:

1. Device connection management via BLE
2. Light state control (on/off, brightness, color)
3. Scene/effect playback
4. State monitoring via BLE notifications
5. Model-specific handling (segmented vs non-segmented, percentage vs absolute brightness)

The entity uses the GoveeBLE class for all protocol operations and maintains its
own BLE connection with keepalive background tasks.

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
    ColorMode,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo

# from homeassistant.helpers.storage import Store
from homeassistant.core import HomeAssistant

from .govee_ble import GoveeBLE
from .const import DOMAIN
from . import Hub

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities
):
    """
    Set up Govee BLE light entities from a config entry.

    This function creates the GoveeBluetoothLight entity for each configured
    device and adds it to Home Assistant's entity registry.

    Args:
        hass: HomeAssistant instance
        config_entry: Configuration entry for the Govee device
        async_add_entities: Home Assistant callback for adding entities

    Returns:
        None

    The function:
    1. Gets the Hub instance from hass.data (or returns if not found)
    2. Converts the BLE device address to a BleakBluetoothDevice
    3. Creates and adds a GoveeBluetoothLight entity

    If the Hub instance doesn't exist in hass.data, the function returns early.
    """
    # Get the hub instance from hass.data
    if config_entry.entry_id in hass.data[DOMAIN]:
        hub: Hub = hass.data[DOMAIN][config_entry.entry_id]
    else:
        # Hub doesn't exist - integration not properly set up
        return

    # Convert the BLE device address to a device object
    if hub.address is not None:
        ble_device = bluetooth.async_ble_device_from_address(
            hass, hub.address.upper(), False
        )
        # Create and add the light entity
        async_add_entities([GoveeBluetoothLight(hub, ble_device, config_entry)])


class GoveeBluetoothLight(LightEntity):
    """
    Home Assistant light entity for Govee BLE devices.

    This class implements the Home Assistant LightEntity interface to provide
    control over Govee BLE lights. It supports:

    - Power control (on/off)
    - Brightness control (0-255 or percentage depending on model)
    - RGB color control
    - Scene/effect playback
    - State monitoring via BLE notifications

    The entity maintains its own BLE connection with a background keepalive task
    to ensure responsive control. The connection is re-established automatically
    if lost.

    Attributes:
        _client: BleakClient instance for BLE communication
        _mac: Device MAC address
        _model: Govee light model identifier
        _is_segmented: Whether device uses segmented LED control
        _use_percent: Whether device uses percentage brightness
        _ble_device: BleakBluetoothDevice object
        _brightness: Current brightness level
        _state: Current power state
        _rgb_color: Current RGB color
        _current_effect: Currently active effect name
        _effect_list: List of available effects
        _effect_map: Mapping of effect names to internal indexes
        _model_data: Loaded JSON data for this device model
    """

    # Supported features: effects can be played
    _attr_supported_features = LightEntityFeature(LightEntityFeature.EFFECT)

    # Supported color mode is RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    # Current color mode (changes when an effect is active)
    _attr_color_mode = ColorMode.RGB

    _client = None  # BleakClient instance for BLE communication

    def __init__(self, hub: Hub, ble_device, config_entry: ConfigEntry) -> None:
        """
        Initialize a bluetooth light entity.

        Args:
            hub: Hub instance containing device address
            ble_device: BleakBluetoothDevice object for BLE communication
            config_entry: Home Assistant configuration entry for this device
        """

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

        # Create device info for Home Assistant
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            name=self._model,
            manufacturer="Govee",
            model=self._model,
        )

    def _load_effect_list(self) -> list[str]:
        """
        Build the effect list from the JSON file.
        Runs in an executor thread to avoid blocking the main thread.

        This method loads the device model's effect definitions from a JSON file.
        The JSON files are located in the jsons/ directory and contain:
        - Category names (e.g., "Relaxing", "Party")
        - Scene names within each category
        - Light effect configurations
        - Scene parameters encoded in base64

        The method builds an effect_map that maps effect names to internal indexes
        needed for constructing the multi-packet commands.

        Args:
            self: Light entity instance

        Returns:
            list[str]: Sorted list of effect names

        The parsing process:
        1. Read the JSON file for this device model
        2. Parse categories and scenes
        3. For each light effect, check if the device model is supported
        4. Build a unique effect name (with counter for duplicates)
        5. Store the internal indexes in effect_map
        6. Return sorted list of effect names

        Raises:
            Exception: If JSON parsing fails or file not found

        Example:
            >>> effects = light._load_effect_list()
            >>> ['Relaxing - Sunset', 'Party - Confetti', ...]
        """
        # Load the JSON file for this model
        self._model_data = json.loads(
            Path(Path(__file__).parent, "jsons", self._model + ".json").read_text()
        )

        # Initialize the effect mapping
        self._effect_map = {}
        effect_list = []

        # Parse each category
        for categoryIdx, category in enumerate(self._model_data["data"]["categories"]):
            # Parse each scene in the category
            for sceneIdx, scene in enumerate(category["scenes"]):
                # Parse each light effect in the scene
                for leffectIdx, lightEffect in enumerate(scene["lightEffects"]):
                    # Parse special effect configurations
                    for seffectIdx, specialEffect in enumerate(
                        lightEffect["specialEffect"]
                    ):
                        # Skip effects not supported by this device model
                        if (
                            "supportSku" in specialEffect
                            and self._model not in specialEffect["supportSku"]
                        ):
                            continue

                        # Build effect name from category, scene, and optional sub-scene
                        name = category["categoryName"] + " - " + scene["sceneName"]
                        if lightEffect["scenceName"]:
                            name += " - " + lightEffect["scenceName"]

                        # Handle duplicate effect names with counters
                        unique_name = name
                        counter = 2
                        while unique_name in self._effect_map:
                            unique_name = f"{name} ({counter})"
                            counter += 1

                        # Store the effect name and its internal indexes
                        self._effect_map[unique_name] = (
                            categoryIdx,
                            sceneIdx,
                            leffectIdx,
                            seffectIdx,
                        )
                        effect_list.append(unique_name)

        # Log the number of effects loaded
        _LOGGER.debug("Loaded %d effects for model %s", len(effect_list), self._model)

        return effect_list

    async def async_added_to_hass(self) -> None:
        """
        Callback when this entity is added to Home Assistant.

        This method is called automatically by Home Assistant when the entity
        is created. It performs initialization tasks:

        1. Loads the effect list from JSON
        2. Queries initial device state (power, brightness, color)
        3. Starts the background keepalive task

        All tasks run asynchronously to avoid blocking the main thread.
        """
        _LOGGER.debug("Loading effect list for model %s", self._model)

        try:
            # Load the effect list asynchronously
            self._effect_list = await self.hass.async_add_executor_job(
                self._load_effect_list
            )
            _LOGGER.debug("Effect list loaded: %d effects", len(self._effect_list))
        except Exception as err:
            # Log error but continue - effects are optional
            _LOGGER.error(
                "Failed to load effect list for model %s: %s", self._model, err
            )

        # Create a background task to connect to the device
        self.hass.async_create_background_task(
            self.try_connect(), "govee_ble_initialize"
        )

    @property
    def effect_list(self) -> list[str] | None:
        """
        Return the list of available effects.

        Returns:
            list[str]: Sorted list of effect names, or None if not loaded
        """
        return self._effect_list

    @property
    def effect(self) -> str | None:
        """
        Return the currently active effect name.

        Returns:
            str: Effect name, or None if no effect is active
        """
        return self._current_effect

    @property
    def name(self) -> str:
        """
        Return the name of the light entity.

        Returns:
            str: "GOVEE Light" (default name for all entities)
        """
        return "GOVEE Light"

    @property
    def color_mode(self) -> ColorMode:
        """
        Return current color mode.

        Returns:
            ColorMode: BRIGHTNESS when an effect is active,
                       RGB when controlling directly

        When an effect is playing, the color mode temporarily changes to
        BRIGHTNESS because the effect controls the color automatically.
        """
        if self._current_effect and self._current_effect != EFFECT_OFF:
            return ColorMode.BRIGHTNESS
        return ColorMode.RGB

    @property
    def unique_id(self) -> str:
        """
        Return a unique, Home Assistant friendly identifier for this entity.

        Returns:
            str: MAC address with colons removed (e.g., "aa:bb:cc:dd:ee:ff" -> "aabbccddeeff")
        """
        return self._mac.replace(":", "")

    @property
    def brightness(self):
        """
        Return current brightness level.

        Returns:
            int: Brightness value (0-255 or 0-100 depending on model)
        """
        return self._brightness

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """
        Return current RGB color.

        Returns:
            tuple[int, int, int]: (red, green, blue) tuple, or None if unknown
        """
        return self._rgb_color

    @property
    def is_on(self) -> bool | None:
        """
        Return true if light is on.

        Returns:
            bool: True if power state is on, False otherwise
        """
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        """
        Turn the light on and optionally set brightness, color, or effect.

        Args:
            **kwargs:
                ATTR_BRIGHTNESS: Brightness value (0-255 or 0-100)
                ATTR_RGB_COLOR: RGB color tuple
                ATTR_EFFECT: Effect name to play

        Raises:
            ConnectionError: If device hasn't connected yet

        The method:
        1. Sends power-on command if no effect is specified
        2. Sets brightness if requested
        3. Sets RGB color if requested
        4. Plays effect if requested

        Note: Effect is always sent before power-on so the device
        activates with the effect already loaded.
        """
        # Ensure device is connected
        if self._client is None:
            raise ConnectionError(
                "This device has not been connected yet. Is it in range?"
            )

        # Send power-on first, unless we're setting an effect
        # Effect data should be loaded before activation
        if ATTR_EFFECT not in kwargs:
            await GoveeBLE.send_single_packet(
                self._client, GoveeBLE.LEDCommand.POWER, [0x1]
            )
            self._state = True

        # Handle brightness setting
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs.get(ATTR_BRIGHTNESS, 255)

            # Some models require a percentage instead of the raw value of a byte.
            await GoveeBLE.send_single_packet(
                self._client,
                GoveeBLE.LEDCommand.BRIGHTNESS,  # Command
                [  # Data
                    (
                        round(self._brightness * 100 / 255)
                        if self._use_percent
                        else self._brightness
                    )
                ],
            )

        # Handle RGB color setting
        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)

            if self._is_segmented:
                # Send segment-specific color command
                await GoveeBLE.send_single_packet(
                    self._client,
                    GoveeBLE.LEDCommand.COLOR,  # Command
                    [  # Data for segmented device
                        GoveeBLE.LEDMode.SEGMENTS,
                        0x01,  # Segment index
                        red,
                        green,
                        blue,  # RGB values
                        0x00,
                        0x00,
                        0x00,
                        0x00,
                        0x00,  # Reserved
                        0xFF,  # Full intensity
                        0x7F,  # Segment count (default to all)
                    ],
                )  # Data
            else:
                # Send standard RGB color command
                await GoveeBLE.send_single_packet(
                    self._client,
                    GoveeBLE.LEDCommand.COLOR,  # Command
                    [  # Data for non-segmented device
                        GoveeBLE.LEDMode.MANUAL,  # Mode
                        red,
                        green,
                        blue,  # RGB values
                    ],
                )  # Data

            # Update entity state
            self._rgb_color = (red, green, blue)
            self._current_effect = EFFECT_OFF

        # Handle effect setting
        if ATTR_EFFECT in kwargs:
            effect = kwargs.get(ATTR_EFFECT)
            _LOGGER.debug("Effect requested: %r", effect)
            _LOGGER.debug(
                "Effect map loaded: %s, size: %d",
                self._effect_map is not None,
                len(self._effect_map) if self._effect_map else 0,
            )

            if not effect:
                _LOGGER.warning("Effect name is empty, skipping")
            elif not self._effect_map:
                _LOGGER.warning(
                    "Effect map is not loaded yet, skipping effect %r", effect
                )
            elif effect not in self._effect_map:
                _LOGGER.warning(
                    "Effect %r not found in effect map. Available: %s",
                    effect,
                    list(self._effect_map.keys())[:5],
                )
            else:
                # Get internal indexes for the effect
                categoryIndex, sceneIndex, lightEffectIndex, specialEffectIndex = (
                    self._effect_map[effect]
                )
                _LOGGER.debug(
                    "Effect %r maps to indexes: cat=%d scene=%d leffect=%d seffect=%d",
                    effect,
                    categoryIndex,
                    sceneIndex,
                    lightEffectIndex,
                    specialEffectIndex,
                )
                category = self._model_data["data"]["categories"][categoryIndex]
                scene = category["scenes"][sceneIndex]
                lightEffect = scene["lightEffects"][lightEffectIndex]
                specialEffect = lightEffect["specialEffect"][specialEffectIndex]

                _LOGGER.debug(
                    "Sending effect sceneParam length: %d",
                    len(specialEffect.get("scenceParam", "")),
                )

                try:
                    # Send multi-packet command with effect data
                    await GoveeBLE.send_multi_packet(
                        self._client,
                        0xA3,  # Protocol type for scene commands
                        array.array("B", [0x02]),  # Header
                        array.array(
                            "B", base64.b64decode(specialEffect["scenceParam"])
                        ),
                    )

                    _LOGGER.debug(
                        "Effect %r sent successfully, sending power-on", effect
                    )

                    # Update current effect
                    self._current_effect = effect

                    # Power-on after effect data so the device activates
                    # with the effect already loaded
                    await GoveeBLE.send_single_packet(
                        self._client, GoveeBLE.LEDCommand.POWER, [0x1]
                    )
                except Exception as err:
                    _LOGGER.error("Failed to send effect %r: %s", effect, err)

    async def async_turn_off(self, **kwargs) -> None:
        """
        Turn the light off.

        Args:
            **kwargs: Ignored (for Home Assistant compatibility)

        Raises:
            ConnectionError: If device hasn't connected yet

        The method sends a power-off command to turn the device off.
        It clears the current effect and state.
        """
        # Ensure device is connected
        if self._client is None:
            raise ConnectionError(
                "This device has not been connected yet. Is it in range?"
            )

        # Send power-off command
        await GoveeBLE.send_single_packet(
            self._client, GoveeBLE.LEDCommand.POWER, [0x0]  # 0x00 = off
        )

        # Clear current effect and state
        self._current_effect = EFFECT_OFF
        self._state = False

    async def _handle_notification(self, sender, data):
        """
        Schedule processing of a received BLE notification without blocking.

        Home Assistant calls this method when a BLE notification is received
        from the device. Notifications indicate state changes (power, brightness, color)
        that need to be reflected in Home Assistant.

        Args:
            sender: The BLE sender (unused)
            data: Notification data bytes

        Returns:
            None

        The method creates an async task to process the notification,
        allowing notifications to be handled asynchronously.
        """
        # Create async task to process notification
        self.hass.async_create_task(self._process_notification(bytes(data)))

    async def _process_notification(self, frame: bytes) -> None:
        """
        Parse a device status frame and update entity state accordingly.

        This method is called asynchronously when BLE notifications are received.
        It validates and parses the frame, then updates the appropriate state
        variable based on the command type.

        Args:
            frame: Complete frame bytes received from the device

        Returns:
            None

        The method:
        1. Validates the frame checksum and format
        2. Checks if it's a response frame (not a command)
        3. Parses the command and payload
        4. Updates the appropriate state variable
        5. Calls async_write_ha_state() to update Home Assistant
        """
        try:
            # Parse the frame and extract header, command, payload
            head, cmd, payload = GoveeBLE.parse_frame(frame)
            # Checks if frame is valid and extracts header, command and payload
        except Exception:
            # Invalid frame - skip processing
            return

        # Only process responses to state requests (not commands we sent)
        if (head != GoveeBLE.LEDFrameType.REQUEST):
            return

        # Handle power state change
        if cmd == GoveeBLE.LEDCommand.POWER:  # Update power state of device
            self._state = payload[0] == 0x01
            if not self._state:
                self._current_effect = EFFECT_OFF

        # Handle brightness change
        elif cmd == GoveeBLE.LEDCommand.BRIGHTNESS:  # Update brightness of device
            # Depending on model type, convert percentage/absolute value
            self._brightness = (
                round(payload[0] * 255 / 100) if self._use_percent else int(payload[0])
            )

        # Handle color change on non-segmented device
        elif cmd == GoveeBLE.LEDCommand.COLOR:  # Update color of non-segmented device
            if len(payload) >= 4:
                self._rgb_color = (payload[1], payload[2], payload[3])
                self._current_effect = EFFECT_OFF

        # Handle color change on segmented device
        elif cmd == GoveeBLE.LEDCommand.SEGMENT:  # Update color of segmented device
            if len(payload) >= 5:
                self._rgb_color = (payload[2], payload[3], payload[4])
                self._current_effect = EFFECT_OFF

        # Update Home Assistant state
        self.async_write_ha_state()

    async def _register_notifications(self) -> None:
        """
        Subscribe to device status updates.

        This method enables BLE notifications on the status characteristic.
        When the device state changes, Home Assistant will receive notifications
        and call _handle_notification to process them.

        Returns:
            None

        Raises:
            Exception: If notifications cannot be enabled (logged as warning)
        """
        try:
            # Enable notifications on the status characteristic
            await self._client.start_notify(
                GoveeBLE.BLE_UUID_STATUS_CHARACTERISTIC, self._handle_notification
            )
        except Exception as err:
            # Log warning but continue - notifications are optional
            _LOGGER.warning(
                "Could not enable notifications for %s: %s", self.unique_id, err
            )

    async def _request_device_state(self) -> None:
        """
        Request the current state of the device.

        This method sends request frames to query the device's current state
        for power, brightness, and color. The device responds with the current
        values which are used to initialize the entity state.

        Returns:
            None

        Raises:
            Exception: If state requests fail (logged as debug)

        The method:
        1. Sends request for power state
        2. Waits 50ms (interframe delay)
        3. Sends request for brightness
        4. Waits 50ms
        5. Sends request for color (format depends on device type)

        The device responds to each request with its current value.
        """
        try:
            # Request power state of device
            await GoveeBLE.send_single_packet(
                self._client,
                GoveeBLE.LEDCommand.POWER,
                [],  # Empty payload for request
                GoveeBLE.LEDFrameType.REQUEST,
            )  # Request power state of device
            await asyncio.sleep(0.05)

            # Request brightness of device
            await GoveeBLE.send_single_packet(
                self._client,
                GoveeBLE.LEDCommand.BRIGHTNESS,
                [],  # Empty payload for request
                GoveeBLE.LEDFrameType.REQUEST,
            )  # Request brightness of device
            await asyncio.sleep(0.05)

            # Request color based on device type
            if self._is_segmented:  # Request color of device
                # Segmented device uses SEGMENT command for color request
                await GoveeBLE.send_single_packet(
                    self._client,
                    GoveeBLE.LEDCommand.SEGMENT,
                    [0x01],  # Segment index
                    GoveeBLE.LEDFrameType.REQUEST,
                )  # Request color of non segmented device
            else:
                # Non-segmented device uses COLOR command for color request
                await GoveeBLE.send_single_packet(
                    self._client,
                    GoveeBLE.LEDCommand.COLOR,
                    [],  # Empty payload for request
                    GoveeBLE.LEDFrameType.REQUEST,
                )  # Request color of segmented device
        except Exception as err:
            # Log as debug - state initialization is not critical
            _LOGGER.debug("Failed to request initial device state: %s", err)

    async def try_connect(self) -> None:
        """
        Attempt to connect to the device.

        This method is called as a background task to establish and maintain
        the BLE connection. It keeps retrying until successful.

        Returns:
            None

        The method:
        1. Establishes BLE connection (retries on failure)
        2. Registers for BLE notifications
        3. Requests initial device state
        4. Starts background keepalive task

        Note: The keepalive task created here ensures connection stability.
        """

        # Keep trying to connect until successful
        while self._client is None:
            try:
                # Create connection with the device
                self._client = await GoveeBLE.create_connection(
                    self._ble_device, self.unique_id, self.hass
                )
            except Exception:
                # Wait before retrying
                await asyncio.sleep(1)

        # Register for BLE notifications
        await self._register_notifications()  # Register notifications which handles response of request device state

        # Request the current device state to initialize entity
        await self._request_device_state()

        # Create a background task to keep the BLE connection active
        # This helps remove the delay when turning on/off lights
        self.hass.async_create_background_task(
            # We pass client here separately because it would be bad
            # to encourage accessing it directly. Thus we pass it explicitly.
            GoveeBLE.ensure_connection(self._client),
            "govee_ble_keepalive",
        )
