"""
Global constants file
Used to store widely used information for the Govee BLE Lights integration.

This module defines all constant values, domain identifiers, and configuration
keys that are used throughout the integration. These constants ensure consistency
and make the codebase easier to maintain and understand.

"""

# Domain identifier for the Govee BLE Lights integration in Home Assistant
# This unique string identifies this custom component in Home Assistant's system
DOMAIN = "govee_ble_lights"

# Configuration key used to identify BLE as the configuration type
# This is used during the config flow to distinguish between different integration types
CONF_TYPE_BLE = 'BLE'

# TODO: Add any additional constants here as the integration grows
# Examples:
# - DOMAIN = Unique identifier for this integration
# - CONF_* = Configuration entry keys
# - BLE_* = BLE-specific timing or protocol constants
