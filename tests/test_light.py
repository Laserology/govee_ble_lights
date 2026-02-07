"""Tests for the Govee BLE light entity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.govee_ble_lights.coordinator import GoveeBLECoordinator
from custom_components.govee_ble_lights.light import GoveeBLELight
from custom_components.govee_ble_lights.protocol import (
    SCENE_IDS,
    build_brightness,
    build_color_rgb,
    build_power,
    build_scene,
)


@pytest.fixture
def mock_coordinator():
    """Create a mock coordinator."""
    coordinator = MagicMock(spec=GoveeBLECoordinator)
    coordinator.address = "AA:BB:CC:DD:EE:FF"
    coordinator.model = "H617A"
    coordinator.is_on = False
    coordinator.brightness_pct = 100
    coordinator.rgb_color = (255, 255, 255)
    coordinator.color_temp_kelvin = None
    coordinator.effect = None
    coordinator.send_command = AsyncMock()
    return coordinator


@pytest.fixture
def mock_config_entry():
    """Create a mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


@pytest.fixture
def light(mock_coordinator, mock_config_entry):
    """Create a light entity for testing."""
    entity = GoveeBLELight(mock_coordinator, mock_config_entry)
    entity.async_write_ha_state = MagicMock()
    return entity


def test_unique_id(light):
    """Test unique ID is derived from address."""
    assert light.unique_id == "aabbccddeeff"


def test_effect_list(light):
    """Test effect list matches scene IDs."""
    assert light.effect_list == list(SCENE_IDS.keys())


def test_is_off_initially(light):
    """Test light starts off."""
    assert light.is_on is False


@pytest.mark.asyncio
async def test_turn_on(light, mock_coordinator):
    """Test turning the light on."""
    await light.async_turn_on()
    mock_coordinator.send_command.assert_called_with(build_power(True))
    assert mock_coordinator.is_on is True


@pytest.mark.asyncio
async def test_turn_off(light, mock_coordinator):
    """Test turning the light off."""
    await light.async_turn_off()
    mock_coordinator.send_command.assert_called_with(build_power(False))
    assert mock_coordinator.is_on is False


@pytest.mark.asyncio
async def test_turn_on_with_brightness(light, mock_coordinator):
    """Test turning on with brightness."""
    await light.async_turn_on(brightness=128)
    calls = mock_coordinator.send_command.call_args_list
    # Should have power on + brightness
    assert len(calls) == 2
    assert calls[0].args[0] == build_power(True)
    assert calls[1].args[0] == build_brightness(50)  # 128/255 * 100 ≈ 50


@pytest.mark.asyncio
async def test_turn_on_with_rgb(light, mock_coordinator):
    """Test turning on with RGB color."""
    await light.async_turn_on(rgb_color=(255, 0, 128))
    calls = mock_coordinator.send_command.call_args_list
    assert len(calls) == 2
    assert calls[1].args[0] == build_color_rgb(255, 0, 128)
    assert mock_coordinator.rgb_color == (255, 0, 128)


@pytest.mark.asyncio
async def test_turn_on_with_effect(light, mock_coordinator):
    """Test turning on with an effect."""
    await light.async_turn_on(effect="rainbow")
    calls = mock_coordinator.send_command.call_args_list
    assert len(calls) == 2
    assert calls[1].args[0] == build_scene(SCENE_IDS["rainbow"])
    assert mock_coordinator.effect == "rainbow"


def test_brightness_conversion(light, mock_coordinator):
    """Test brightness converts from pct to 0-255."""
    mock_coordinator.brightness_pct = 50
    assert light.brightness == 127
