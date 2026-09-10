"""
Home Assistant light entities for Govee BLE lights.

Controls power, brightness, color, and effects (defined in config.json, see
models.py) over BLE, and tracks device state via notifications. Protocol and
connection handling live in govee_ble.py.
"""

from __future__ import annotations

from typing import Any
import asyncio
import logging
import time

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
from .effects import effect_target, run_fade, segments_to_writes
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
# color changes are interpolated and re-sent at this rate. 30 fps keeps the
# BLE write load reasonable (a multi-color frame sends one write per color).
_FADE_INTERVAL = 1 / 30


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities
):
    """
    Set up light entities for a config entry (adapter to the HA callback).

    Args:
        hass: HomeAssistant instance
        config_entry: Configuration entry for the device
        async_add_entities: Home Assistant callback for adding entities
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
        async_add_entities([GoveeBluetoothLight(hub, ble_device, config_entry)])


class GoveeBluetoothLight(LightEntity):
    """
    Home Assistant light entity for one Govee BLE device.

    Supports on/off, brightness, RGB color, effects on segmented models, and
    state monitoring via notifications over a keepalive-maintained BLE
    connection.
    """

    # Supported color mode is RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    _attr_color_mode = ColorMode.RGB

    _client = None  # BleakClient instance for BLE communication

    def __init__(self, hub: Hub, ble_device, config_entry: ConfigEntry) -> None:
        """Create the entity for *hub*, looking up model behavior from config."""
        self._mac = hub.address
        self._model = config_entry.data["model"]
        self._is_segmented = is_segmented_model(self._model)
        self._use_percent = uses_percent_brightness(self._model)
        self._ble_device = ble_device
        self._brightness = 255
        self._state = False
        self._rgb_color: tuple[int, int, int] | None = None
        # Current effect, or EFFECT_OFF when in manual color mode; selecting
        # EFFECT_OFF restores the last solid color from _rgb_color.
        self._current_effect: str = EFFECT_OFF

        # Effect playing when the light was powered off, so an off/on cycle
        # resumes the animation instead of leaving a static pattern frame.
        self._effect_before_off: str | None = None

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
        """Start the background connection task when the entity is added."""
        self.hass.async_create_background_task(
            self.try_connect(), "govee_ble_initialize"
        )

    @property
    def name(self) -> str:
        """Return the entity name ("GOVEE Light")."""
        return "GOVEE Light"

    @property
    def unique_id(self) -> str:
        """Return the unique entity ID (MAC without colons)."""
        return self._mac.replace(":", "")

    @property
    def brightness(self):
        """Return the current brightness (0-255)."""
        return self._brightness

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the current RGB color, or None if unknown."""
        return self._rgb_color

    @property
    def effect_list(self) -> list[str]:
        """Return effect names (prefixed with EFFECT_OFF), or [] if unsupported."""
        if not self._effects:
            return []
        return [EFFECT_OFF, *sorted(self._effects)]

    @property
    def effect(self) -> str:
        """Return the currently active effect name (EFFECT_OFF in manual mode)."""
        return self._current_effect

    @property
    def is_on(self) -> bool | None:
        """Return True if the light is on."""
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Diagnostic attributes for automations and support."""
        attrs: dict[str, Any] = {"govee_model": self._model}

        if self._is_segmented:
            attrs["govee_segments"] = get_segment_count(self._model)

        rssi = self._rssi()
        if rssi is not None:
            attrs["govee_rssi"] = rssi

        if self._client is not None:
            write_ms = GoveeBLE.last_write_ms(self._client)
            if write_ms is not None:
                attrs["govee_last_write_ms"] = round(write_ms, 1)

        return attrs

    def _rssi(self) -> int | None:
        """Return the last reported RSSI for the device, if known."""
        try:
            info = bluetooth.async_last_service_info(self.hass, self._mac.upper(), True)
        except Exception:
            return None
        return info.rssi if info is not None else None

    async def async_turn_on(self, **kwargs) -> None:
        """
        Turn the light on, optionally with an effect, brightness, color, or
        transition (fade duration in seconds). Selecting a color stops any
        effect.

        Args:
            **kwargs: HA light service attributes.

        Raises:
            ConnectionError: If the device has not connected yet.
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

        # HA automations can pass a transition (seconds); it overrides the
        # configured fades for this call.
        transition = kwargs.get(ATTR_TRANSITION)

        await GoveeBLE.send_single_packet(
            self._client, GoveeBLE.LEDCommand.POWER, [0x1]
        )
        self._state = True

        # Handle effect setting
        if ATTR_EFFECT in kwargs:
            effect = kwargs.get(ATTR_EFFECT)
            if not effect or effect == EFFECT_OFF:
                # Leave effect mode, repainting the last solid color so the
                # pattern is actually cleared.
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

            # Some models expect a percentage (0-100) instead of a raw byte.
            if self._use_percent:
                brightness = round(self._brightness * 100 / 255)
            else:
                brightness = self._brightness

            await GoveeBLE.send_single_packet(
                self._client,
                GoveeBLE.LEDCommand.BRIGHTNESS,
                [brightness],
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

            self._rgb_color = (red, green, blue)
            self._current_effect = EFFECT_OFF

        # Power-on with no explicit target: resume the effect that was playing
        # when the light was turned off, or re-apply the tracked pattern with
        # a fade-in from black. Only when the light was off - a brightness-
        # only change while on (e.g. the slider) must not repaint from black.
        if (
            was_off
            and ATTR_EFFECT not in kwargs
            and ATTR_RGB_COLOR not in kwargs
            and self._segment_state is not None
        ):
            if self._effect_before_off in self._effects:
                await self._async_apply_effect(
                    self._effect_before_off,
                    start_from_black=True,
                    fade_override=transition,
                )
                self._effect_before_off = None
            else:
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
        Set the whole strip to a solid color, fading from the current state
        (or from black on power-on) unless overridden.

        Args:
            red/green/blue: RGB channels (0-255).
            fade: Crossfade duration override in seconds.
            start_from_black: Fade from black instead of the current state.
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
        """Convert per-segment colors into BLE writes (mask packets for
        segmented models, a single manual color packet otherwise)."""
        if self._is_segmented:
            return segments_to_writes(target)
        return [{"color": target[0]}]

    async def _async_send_writes(self, writes: list[dict]) -> None:
        """Send computed writes to the device as one atomic frame (no other
        traffic can interleave between the packets)."""
        if self._is_segmented:
            packets = [
                GoveeBLE.build_segment_packet(
                    write["color"], write["mask_lo"], write["mask_hi"]
                )
                for write in writes
            ]
        else:
            red, green, blue = writes[0]["color"]
            packets = [GoveeBLE.build_color_packet(red, green, blue)]
        await GoveeBLE.send_writes(self._client, packets)

    async def _async_render_target(
        self, target: list[list[int]], fade: float, start_from_black: bool = False
    ) -> None:
        """
        Move the strip toward *target* over *fade* seconds (instant when 0 or
        the start state is unknown). Only the most recent render survives, so
        frames from two fades never interleave.

        Args:
            target: One color per segment.
            fade: Crossfade duration in seconds (0 = instant).
            start_from_black: Interpolate from black (power-on).
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

        # run_fade paces against the wall clock so write latency cannot
        # stretch the fade or slip animation steps.
        async def send_frame(frame):
            await self._async_send_writes(self._writes_for_target(frame))
            # Track the last frame sent so an interrupted fade resumes from
            # the real state.
            self._segment_state = frame

        await run_fade(
            start,
            target,
            fade,
            _FADE_INTERVAL,
            send_frame,
            lambda: self._render_epoch == my_epoch,
        )

    async def _async_apply_effect(
        self,
        name: str,
        start_from_black: bool = False,
        fade_override: float | None = None,
    ) -> None:
        """
        Paint the initial frame of *name* and start its animation task if the
        effect defines ``step``.

        Args:
            name: Effect to apply.
            start_from_black: Fade in from black (power-on).
            fade_override: Duration override in seconds (HA transition).
        """
        await self._async_cancel_effect_task()

        # Capture the operation so a newer turn request arriving during the
        # initial fade can supersede us (and skip spawning the loop).
        my_op_serial = self._op_serial

        effect_def = self._effects[name]
        if fade_override is not None:
            fade = fade_override
        else:
            # Power-on ramps use fade_on; otherwise the effect's own fade (or
            # the step interval, or the top-level default) applies.
            fade = (
                get_fade_on()
                if start_from_black and get_fade_on() > 0
                else self._effect_fade(effect_def)
            )
        try:
            target = effect_target(effect_def, get_segment_count(self._model))
        except ValueError as err:
            _LOGGER.error("Effect %r is invalid: %s", name, err)
            return

        # Declare before rendering (so the fade-in's color echoes are ignored)
        # and before spawning the loop - HA's eager task factory runs its
        # first check at task creation.
        self._current_effect = name
        await self._async_render_target(target, fade, start_from_black=start_from_black)

        # Superseded during the fade: revert unless a newer request claimed it.
        if self._op_serial != my_op_serial:
            if self._current_effect == name:
                self._current_effect = EFFECT_OFF
            return
        if effect_def.get("step"):
            self._effect_task = self.hass.async_create_background_task(
                self._effect_loop(name), f"govee_ble_effect_{name}"
            )

    def _effect_fade(self, effect_def: dict) -> float:
        """
        Fade duration for an effect's frame transitions.

        An explicit ``fade`` wins; otherwise animated effects default to the
        ``step`` interval and static effects to the top-level color fade.

        Keep ``fade`` shorter than ``step`` so the strip settles between
        shifts; ``fade == step`` never settles (continuous pulse breathing is
        the intended exception).
        """
        if "fade" in effect_def:
            return float(effect_def["fade"])
        step = float(effect_def.get("step") or 0)
        if step > 0:
            return step
        return get_default_fade()

    async def _effect_loop(self, name: str) -> None:
        """Advance an animated effect one step every ``step`` seconds.

        Each step crossfades toward the new target over ``fade`` seconds
        (keep it below ``step`` so the strip settles; ``fade: 0`` steps
        crisply). Deadline-based pacing stops a slow render from
        busy-spinning the loop.
        """
        effect_def = self._effects[name]
        step = float(effect_def.get("step") or 0)
        if step <= 0:
            return

        fade = self._effect_fade(effect_def)
        direction = -1 if effect_def.get("direction") == "reverse" else 1
        offset = 0
        while not self.hass.is_stopping:
            # Stop when the effect changed or the light was turned off.
            if self._current_effect != name:
                return

            offset += 1
            start = time.monotonic()
            try:
                target = effect_target(
                    effect_def,
                    get_segment_count(self._model),
                    offset=offset,
                    direction=direction,
                )
                await self._async_render_target(target, fade)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                # Transient BLE failures should not kill the animation; keep
                # trying on the next step.
                _LOGGER.debug("Failed to advance effect %r: %s", name, err)

            # Hold for the rest of the step (the render may itself take up
            # to ``fade`` seconds).
            remaining = step - (time.monotonic() - start)
            if remaining > 0:
                await asyncio.sleep(remaining)

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
        """Turn the light off, fading to black first when a fade-off duration
        is configured (transition or fade_off)."""
        # Ensure device is connected
        if self._client is None:
            raise ConnectionError(
                "This device has not been connected yet. Is it in range?"
            )

        # Bump the operation serial: a newer turn-on/off supersedes this one.
        self._op_serial += 1
        my_op_serial = self._op_serial

        # Remember an active effect so a later power-on resumes it instead of
        # showing a static snapshot of the last animation frame.
        self._effect_before_off = (
            self._current_effect if self._current_effect in self._effects else None
        )

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
        """Dispatch a received BLE notification to _process_notification."""
        self.hass.async_create_task(self._process_notification(bytes(data)))

    async def _process_notification(self, frame: bytes) -> None:
        """
        Update entity state from a device status frame (only REQUEST
        responses; our own commands are ignored).

        Color/segment notifications are ignored while an effect is active:
        they only describe the transient pattern and would overwrite the last
        solid color and spam HA state writes every frame.
        """
        try:
            head, cmd, payload = GoveeBLE.parse_frame(frame)
        except Exception:
            # Invalid frame - skip processing
            return

        # Only process responses to state requests (not commands we sent)
        if head != GoveeBLE.LEDFrameType.REQUEST:
            return

        # Color reported for the whole strip (COLOR) or one segment (SEGMENT).
        if cmd in (GoveeBLE.LEDCommand.COLOR, GoveeBLE.LEDCommand.SEGMENT):
            if self._current_effect != EFFECT_OFF:
                return

            if cmd == GoveeBLE.LEDCommand.COLOR and len(payload) >= 4:
                red, green, blue = payload[1], payload[2], payload[3]
                self._rgb_color = (red, green, blue)

                # Seed the fade state from the reported color so fades work
                # before any explicit color has been set.
                if self._segment_state is None:
                    self._segment_state = [[red, green, blue]]

            elif cmd == GoveeBLE.LEDCommand.SEGMENT and len(payload) >= 5:
                red, green, blue = payload[2], payload[3], payload[4]
                self._rgb_color = (red, green, blue)

                # Seed the fade state from the reported color (assume the
                # strip is solid until proven otherwise).
                if self._segment_state is None:
                    color = [red, green, blue]
                    self._segment_state = [color] * get_segment_count(self._model)

            self.async_write_ha_state()
            return

        # Power and brightness only broadcast state when they actually changed
        # (the device can repeat status frames, including keepalive echoes).
        changed = False
        if cmd == GoveeBLE.LEDCommand.POWER:
            state = payload[0] == 0x01
            if state != self._state:
                self._state = state
                changed = True

        elif cmd == GoveeBLE.LEDCommand.BRIGHTNESS:
            # Convert percentage/absolute depending on the model
            brightness = (
                round(payload[0] * 255 / 100) if self._use_percent else int(payload[0])
            )
            if brightness != self._brightness:
                self._brightness = brightness
                changed = True

        if changed:
            self.async_write_ha_state()

    async def _register_notifications(self) -> None:
        """Subscribe to status notifications, replacing any existing one."""
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
        """Query the device for its current power, brightness, and color."""
        try:
            # Power
            await GoveeBLE.send_single_packet(
                self._client,
                GoveeBLE.LEDCommand.POWER,
                [],
                GoveeBLE.LEDFrameType.REQUEST,
            )
            await asyncio.sleep(0.05)

            # Brightness
            await GoveeBLE.send_single_packet(
                self._client,
                GoveeBLE.LEDCommand.BRIGHTNESS,
                [],
                GoveeBLE.LEDFrameType.REQUEST,
            )
            await asyncio.sleep(0.05)

            # Color (SEGMENT for segmented models, COLOR otherwise)
            request_cmd = (
                GoveeBLE.LEDCommand.SEGMENT
                if self._is_segmented
                else GoveeBLE.LEDCommand.COLOR
            )
            await GoveeBLE.send_single_packet(
                self._client,
                request_cmd,
                [0x01] if self._is_segmented else [],
                GoveeBLE.LEDFrameType.REQUEST,
            )
        except Exception as err:
            # State initialization is not critical
            _LOGGER.debug("Failed to request initial device state: %s", err)

    async def _reconnect_handler(self) -> None:
        """Re-subscribe notifications and re-request state after a reconnect.

        GATT subscriptions are destroyed on disconnect, so this restores the
        notification channel _process_notification depends on.
        """
        try:
            await self._register_notifications()
            await self._request_device_state()
        except Exception as err:
            _LOGGER.debug("Reconnect handler failed: %s", err)

    async def try_connect(self) -> None:
        """Connect to the device (retrying until successful), then register
        notifications, request initial state, and start the keepalive task."""
        # Keep trying to connect until successful
        while self._client is None:
            try:
                self._client = await GoveeBLE.create_connection(
                    self._ble_device, self.unique_id
                )
            except Exception:
                # Wait before retrying
                await asyncio.sleep(1)

        # Register for BLE notifications (handles state request responses)
        await self._register_notifications()

        # Request the current device state to initialize the entity
        await self._request_device_state()

        # Background task keeping the connection alive for responsive control
        self.hass.async_create_background_task(
            GoveeBLE.ensure_connection(self._client, self._reconnect_handler),
            "govee_ble_keepalive",
        )
