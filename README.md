# Govee BLE Lights for Home Assistant

[![CI](https://github.com/teh-hippo/govee_ble_lights/actions/workflows/ci.yml/badge.svg)](https://github.com/teh-hippo/govee_ble_lights/actions/workflows/ci.yml)

A Home Assistant custom integration for controlling Govee LED strips via Bluetooth Low Energy (BLE).

## Supported Devices

| Model | Name |
|-------|------|
| H617A | LED Strip |

## Features

- **On/Off** control
- **Brightness** adjustment (0-100%)
- **RGB Color** — full color control via segmented BLE mode
- **Color Temperature** — 2000K–9000K (converted to RGB)
- **Scenes** — sunrise, sunset, movie, dating, romantic, blinking, candlelight, snowflake, rainbow
- **BLE Auto-Discovery** — automatically detects Govee devices nearby

## Installation (HACS)

1. Open HACS in Home Assistant
2. Click the three dots menu → **Custom repositories**
3. Add `https://github.com/teh-hippo/govee_ble_lights` as an **Integration**
4. Search for "Govee BLE Lights" and install
5. Restart Home Assistant

## Configuration

Once installed, the integration will automatically discover nearby Govee BLE devices. You can also add devices manually:

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for "Govee BLE Lights"
3. Enter the BLE address and select the device model

## BLE Troubleshooting

- Ensure your Home Assistant host has a Bluetooth adapter
- The device must be within BLE range (~10m line of sight)
- Only one controller can be connected to a BLE device at a time — close the Govee app before using HA
- If the device is unresponsive, power cycle it and restart HA
- Check **Settings → System → Logs** for BLE connection errors

## License

MIT
