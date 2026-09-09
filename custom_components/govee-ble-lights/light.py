"""
This class represents Govee light entities.
It only contains the basic methods, and uses govee_ble to talk to govee devices.

This module implements Home Assistant light entities that control Govee BLE lights.
It provides:

1. Device connection management via BLE
2. Light state control (on/off, brightness, color)
3. Effect playback (static patterns, segmented models)
4. State monitoring via BLE notifications
5. Model-specific handling (segmented vs non-segmented, percentage vs absolute brightness)

The entity uses the GoveeBLE class for all protocol operations and maintains its
own BLE connection with keepalive background tasks.

Effects are defined in config.json (see models.py) as *patterns* that are
generic across segment counts; the entity renders each one for the model's own
segment count.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components import bluetooth
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_EFFECT,
    ATTR_TRANSITION,
    EFFECT_OFF,
    LightEntity,
    LightEntityFeature,
    ColorMode,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.core import HomeAssistant

from .govee_ble import GoveeBLE
from .const import DOMAIN
from .effects import interpolate_segments, segment_colors, segments_to_writes
from .models import (
    get_default_fade,
    get_fade_off,
    get_fade_on,
    get_model_effects,
    get_segment_count,
    is_segmented_model,
    uses_percent_brightness,
)
from . import Hub

_LOGGER = logging.getLogger(__name__)

# Seconds between software-fade frames. Firmware has no native fading, so
# color changes are interpolated and re-sent at this rate (~30 fps).
_FADE_INTERVAL = 1 / 30


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
    - Effect playback (static patterns, segmented models)
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
        _current_effect: Name of the active effect (None if not using one)
        _effects: Effect definitions available for this model
    """

    # Supported color mode is RGB
    _attr_supported_color_modes = {ColorMode.RGB}

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
        self._is_segmented = is_segmented_model(self._model)
        self._use_percent = uses_percent_brightness(self._model)
        self._ble_device = ble_device
        self._brightness = 255
        self._state = False
        self._rgb_color: tuple[int, int, int] | None = None
        # Current effect, or EFFECT_OFF when the light is in manual color mode.
        # Selecting EFFECT_OFF while an effect is active restores the last
        # solid color (kept in _rgb_color) so the pattern is cleared.
        self._current_effect: str = EFFECT_OFF

        # Segment patterns need an addressable segment controller; only
        # segmented models advertise effects.
        self._effects: dict[str, dict] = (
            get_model_effects(self._model) if self._is_segmented else {}
        )

        # Transitions (fades) are always supported so automations can pass
        # light.turn_on/turn_off transition.
        features = LightEntityFeature.TRANSITION
        if self._effects:
            features |= LightEntityFeature.EFFECT
        self._attr_supported_features = features

        # Tracks whether we currently have an active notification subscription,
        # so _register_notifications can stop the old one before re-subscribing.
        self._notifications_active = False

        # Background task advancing an animated effect, if one is running.
        self._effect_task: asyncio.Task | None = None

        # Monotonic counters so a newer fade (or turn request) invalidates
        # older in-flight ones. Rapid color changes must never interleave
        # frames from two fades at once (that causes flicker).
        self._render_epoch = 0
        self._op_serial = 0

        # Current per-segment color state, used as the starting point when
        # crossfading to a new target. None means the device state is unknown.
        # Seeded from the device's reported color on connect, so fades work
        # even before the user sets a color explicitly.
        self._segment_state: list[list[int]] | None = None

        # Create device info for Home Assistant
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            name=self._model,
            manufacturer="Govee",
            model=self._model,
        )

    async def async_added_to_hass(self) -> None:
        """
        Callback when this entity is added to Home Assistant.

        This method is called automatically by Home Assistant when the entity
        is created. It performs initialization tasks:

        1. Queries initial device state (power, brightness, color)
        2. Starts the background keepalive task

        All tasks run asynchronously to avoid blocking the main thread.
        """
        # Create a background task to connect to the device
        self.hass.async_create_background_task(
            self.try_connect(), "govee_ble_initialize"
        )

    @property
    def name(self) -> str:
        """
        Return the name of the light entity.

        Returns:
            str: "GOVEE Light" (default name for all entities)
        """
        return "GOVEE Light"

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
    def effect_list(self) -> list[str]:
        """
        Return the effect names available for this model.

        Returns:
            list[str]: Sorted effect names prefixed with an EFFECT_OFF entry
                to return to manual color mode, or an empty list when the
                model has no effect support.
        """
        if not self._effects:
            return []
        return [EFFECT_OFF, *sorted(self._effects)]

    @property
    def effect(self) -> str:
        """Return the currently active effect name (EFFECT_OFF in manual mode)."""
        return self._current_effect

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
        Turn the light on and optionally set an effect, brightness, or color.

        Args:
            **kwargs:
                ATTR_EFFECT: Name of an effect to play
                ATTR_BRIGHTNESS: Brightness value (0-255 or 0-100)
                ATTR_RGB_COLOR: RGB color tuple

        Raises:
            ConnectionError: If device hasn't connected yet

        The method:
        1. Sends power-on command
        2. Plays the requested effect, if any
        3. Sets brightness if requested
        4. Sets RGB color if requested (selecting a color stops the effect)
        """
        # Ensure device is connected
        if self._client is None:
            raise ConnectionError(
                "This device has not been connected yet. Is it in range?"
            )

        # Remember whether the light was off: power-on transitions fade in
        # from black instead of crossfading from the previous color.
        was_off = not self._state

        # Bump the operation serial so any older turn request (in particular
        # an in-flight fade-out) knows it has been superseded.
        self._op_serial += 1

        # Home Assistant automations can pass a transition (seconds); it
        # overrides the configured fades for this call.
        transition = kwargs.get(ATTR_TRANSITION)

        # Send power-on
        await GoveeBLE.send_single_packet(
            self._client, GoveeBLE.LEDCommand.POWER, [0x1]
        )
        self._state = True

        # Handle effect setting
        if ATTR_EFFECT in kwargs:
            effect = kwargs.get(ATTR_EFFECT)
            if not effect or effect == EFFECT_OFF:
                # Leave effect mode and go back to a solid color. The pattern
                # is cleared by repainting every segment with the last color
                # chosen before the effect was started.
                await self._async_cancel_effect_task()
                self._current_effect = EFFECT_OFF
                if self._rgb_color is not None:
                    await self._async_set_solid_color(*self._rgb_color)
                else:
                    _LOGGER.debug(
                        "No previous color to restore for model %s", self._model
                    )
            elif effect in self._effects:
                await self._async_apply_effect(
                    effect, start_from_black=was_off, fade_override=transition
                )
                self._current_effect = effect
            else:
                _LOGGER.warning(
                    "Effect %r not available for model %s. Available: %s",
                    effect,
                    self._model,
                    sorted(self._effects),
                )

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
            # Stop any running animation first so it cannot fight the fade.
            await self._async_cancel_effect_task()
            await self._async_set_solid_color(
                red,
                green,
                blue,
                fade=transition,
                start_from_black=was_off,
            )

            # Update entity state
            self._rgb_color = (red, green, blue)
            self._current_effect = EFFECT_OFF

        # Power-on with no explicit target: re-apply the tracked pattern with
        # a fade-in from black, so toggling on fades in even before any color
        # has been set explicitly (the state is seeded from the device's
        # reported color on connect).
        if (
            ATTR_EFFECT not in kwargs
            and ATTR_RGB_COLOR not in kwargs
            and self._segment_state is not None
        ):
            await self._async_render_target(
                self._segment_state,
                transition if transition is not None else get_fade_on(),
                start_from_black=True,
            )

        self.async_write_ha_state()

    async def _async_set_solid_color(
        self,
        red: int,
        green: int,
        blue: int,
        fade: float | None = None,
        start_from_black: bool = False,
    ) -> None:
        """
        Set the whole light to a single solid color.

        Segmented models get one color per segment; non-segmented models get
        the standard manual color command. The change crossfades instead of
        jumping: from the previous color for normal changes, or from black
        when the light is powering on.

        Args:
            red: Red channel (0-255)
            green: Green channel (0-255)
            blue: Blue channel (0-255)
            fade: Override crossfade duration (seconds); defaults to the
                top-level ``fade`` value (or ``fade_on`` when powering on).
            start_from_black: Fade from off/black rather than the current
                state (used when the light was powered off).
        """
        if start_from_black:
            duration = fade if fade is not None else get_fade_on()
        elif fade is not None:
            duration = fade
        else:
            duration = get_default_fade()

        if self._is_segmented:
            target = [[red, green, blue]] * get_segment_count(self._model)
        else:
            target = [[red, green, blue]]
        await self._async_render_target(
            target, duration, start_from_black=start_from_black
        )

    def _writes_for_target(self, target: list[list[int]]) -> list[dict]:
        """Convert a per-segment color state into BLE write dicts.

        Segmented models use one mask packet per distinct color; non-segmented
        models use a single manual color packet.

        Args:
            target: One color per segment.

        Returns:
            list[dict]: write dicts, see segments_to_writes. Non-segmented
            writes carry only the color.
        """
        if self._is_segmented:
            return segments_to_writes(target)
        return [{"color": target[0]}]

    async def _async_send_writes(self, writes: list[dict]) -> None:
        """Send a set of computed writes to the device.

        Args:
            writes: write dicts from _writes_for_target
        """
        for write in writes:
            if self._is_segmented:
                await GoveeBLE.set_segments_color(
                    self._client, write["color"], write["mask_lo"], write["mask_hi"]
                )
            else:
                red, green, blue = write["color"]
                await GoveeBLE.send_single_packet(
                    self._client,
                    GoveeBLE.LEDCommand.COLOR,
                    [GoveeBLE.LEDMode.MANUAL, red, green, blue],
                )

    async def _async_render_target(
        self, target: list[list[int]], fade: float, start_from_black: bool = False
    ) -> None:
        """
        Move the strip from its current state to *target*, optionally fading.

        When the start state is known (or *start_from_black* is set) and
        *fade* > 0, the colors are interpolated channel-wise and intermediate
        frames are sent every ``_FADE_INTERVAL`` seconds. Otherwise the
        target is applied instantly.

        Only the most recent render survives: starting a new render (e.g. a
        rapid color change) invalidates any fade still in progress, which
        then aborts before its next write instead of interleaving frames.

        Args:
            target: One color per segment (see _writes_for_target).
            fade: Crossfade duration in seconds (0 = instant).
            start_from_black: Interpolate from black instead of the current
                tracked state (used when powering on from off).
        """
        self._render_epoch += 1
        my_epoch = self._render_epoch

        start = None
        if start_from_black:
            start = [[0, 0, 0]] * len(target)
        elif self._segment_state is not None and fade > 0:
            start = self._segment_state

        if start is None or fade <= 0:
            if self._render_epoch != my_epoch:
                return
            await self._async_send_writes(self._writes_for_target(target))
            if self._render_epoch == my_epoch:
                self._segment_state = target
            return

        # Nothing to do when the target already matches the start state.
        if start == target:
            self._segment_state = target
            return

        frames = max(1, round(fade / _FADE_INTERVAL))
        for frame_index in range(1, frames + 1):
            if self._render_epoch != my_epoch:
                # A newer render superseded this one; stop before writing so
                # frames from two fades never interleave.
                return
            frame = interpolate_segments(start, target, frame_index / frames)
            await self._async_send_writes(self._writes_for_target(frame))
            # Track the frame actually sent so an interrupted fade (e.g. the
            # effect task being cancelled) resumes from the real state.
            self._segment_state = frame
            await asyncio.sleep(_FADE_INTERVAL)

        if self._render_epoch == my_epoch:
            self._segment_state = target

    async def _async_apply_effect(
        self,
        name: str,
        start_from_black: bool = False,
        fade_override: float | None = None,
    ) -> None:
        """
        Paint the initial frame of an effect and start its animation, if any.

        The effect definition is rendered against this model's segment count
        and each resulting color is written with a single mask packet. When
        the effect defines a ``step`` (seconds), a background task advances
        the pattern by one segment every step.

        Args:
            name: Name of the effect to apply (must exist in ``self._effects``)
            start_from_black: Fade in from off/black (used on power-on).
            fade_override: Explicit fade duration (seconds); overrides the
                configured fades for the initial frame (used for HA
                transition calls).
        """
        await self._async_cancel_effect_task()

        effect_def = self._effects[name]
        if fade_override is not None:
            fade = fade_override
        else:
            # Power-on ramps use fade_on; otherwise the effect's own fade (or
            # the top-level default) applies.
            fade = (
                get_fade_on()
                if start_from_black and get_fade_on() > 0
                else float(effect_def.get("fade", get_default_fade()))
            )
        try:
            target = segment_colors(effect_def, get_segment_count(self._model))
        except ValueError as err:
            _LOGGER.error("Effect %r is invalid: %s", name, err)
            return

        await self._async_render_target(target, fade, start_from_black=start_from_black)

        # Animated effects advance the pattern in a background task.
        if effect_def.get("step"):
            self._effect_task = self.hass.async_create_background_task(
                self._effect_loop(name), f"govee_ble_effect_{name}"
            )

    async def _effect_loop(self, name: str) -> None:
        """
        Advance an animated effect by one segment every ``step`` seconds.

        The loop keeps painting until the effect is switched away, the light
        is turned off, or the task is cancelled.

        Args:
            name: Name of the effect being animated
        """
        effect_def = self._effects[name]
        step = float(effect_def.get("step") or 0)
        if step <= 0:
            return

        offset = 0
        while not self.hass.is_stopping:
            await asyncio.sleep(step)
            if self._current_effect != name:
                return

            offset += 1
            try:
                target = segment_colors(
                    effect_def, get_segment_count(self._model), offset=offset
                )
                await self._async_render_target(
                    target, float(effect_def.get("fade", get_default_fade()))
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:
                # Transient BLE failures should not kill the animation; keep
                # trying on the next step.
                _LOGGER.debug("Failed to advance effect %r: %s", name, err)

    async def _async_cancel_effect_task(self) -> None:
        """Cancel and await the effect animation task, if one is running."""
        if self._effect_task is None:
            return
        task = self._effect_task
        self._effect_task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def async_will_remove_from_hass(self) -> None:
        """Stop the effect animation task when the entity is removed."""
        await self._async_cancel_effect_task()

    async def async_turn_off(self, **kwargs) -> None:
        """
        Turn the light off.

        Args:
            **kwargs: Ignored (for Home Assistant compatibility)

        Raises:
            ConnectionError: If device hasn't connected yet

        The method sends a power-off command to turn the device off.
        """
        # Ensure device is connected
        if self._client is None:
            raise ConnectionError(
                "This device has not been connected yet. Is it in range?"
            )

        # Bump the operation serial: a newer turn-on/off supersedes this one.
        self._op_serial += 1
        my_op_serial = self._op_serial

        # Stop any running animation; the pattern is no longer guaranteed to
        # match the device once it is powered down.
        await self._async_cancel_effect_task()

        # Home Assistant automations can pass a transition (seconds); it
        # overrides the configured fade-off duration for this call.
        transition = kwargs.get(ATTR_TRANSITION)
        prev_state = self._segment_state
        # Fall back to the reported solid color when the per-segment layout is
        # unknown, so fade-off works before any explicit color has been set.
        if prev_state is None and self._rgb_color is not None:
            color = list(self._rgb_color)
            if self._is_segmented:
                prev_state = [color] * get_segment_count(self._model)
            else:
                prev_state = [color]

        fade_off = transition if transition is not None else get_fade_off()
        if prev_state is not None and fade_off > 0:
            black = [[0, 0, 0]] * len(prev_state)
            await self._async_render_target(black, fade_off)

        # A newer turn request (e.g. a turn-on that raced the fade-out) took
        # over; do not switch the power off now.
        if self._op_serial != my_op_serial:
            return
        if prev_state is not None and fade_off > 0:
            self._segment_state = prev_state

        # Send power-off command
        await GoveeBLE.send_single_packet(
            self._client, GoveeBLE.LEDCommand.POWER, [0x0]  # 0x00 = off
        )

        self._current_effect = EFFECT_OFF
        self._state = False
        self.async_write_ha_state()

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
        if head != GoveeBLE.LEDFrameType.REQUEST:
            return

        # Handle power state change
        if cmd == GoveeBLE.LEDCommand.POWER:  # Update power state of device
            self._state = payload[0] == 0x01

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
                # Seed the fade state from the reported color so fades work
                # before any explicit color has been set.
                if self._segment_state is None:
                    self._segment_state = [[payload[1], payload[2], payload[3]]]

        # Handle color change on segmented device
        elif cmd == GoveeBLE.LEDCommand.SEGMENT:  # Update color of segmented device
            if len(payload) >= 5:
                self._rgb_color = (payload[2], payload[3], payload[4])
                # Seed the fade state from the reported color (assume the
                # strip is solid until proven otherwise).
                if self._segment_state is None:
                    color = [payload[2], payload[3], payload[4]]
                    self._segment_state = [color] * get_segment_count(self._model)

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
        # Stop any existing notification before starting a new one.
        # This prevents duplicate handlers after reconnection.
        if self._notifications_active:
            try:
                await self._client.stop_notify(GoveeBLE.BLE_UUID_STATUS_CHARACTERISTIC)
            except Exception:
                pass  # Ignore stop errors; we're about to re-subscribe anyway

        try:
            # Enable notifications on the status characteristic
            await self._client.start_notify(
                GoveeBLE.BLE_UUID_STATUS_CHARACTERISTIC, self._handle_notification
            )
            self._notifications_active = True
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

    async def _reconnect_handler(self) -> None:
        """
        Re-register BLE notifications and re-request device state after a reconnect.

        Called by ensure_connection() after it successfully reconnects the underlying
        transport. GATT subscriptions are destroyed on disconnect so this restores
        the notification channel that _process_notification depends on.
        """
        try:
            await self._register_notifications()
            await self._request_device_state()
        except Exception as err:
            _LOGGER.debug("Reconnect handler failed: %s", err)

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
            GoveeBLE.ensure_connection(self._client, self._reconnect_handler),
            "govee_ble_keepalive",
        )
