# Ultimate Govee BLE Lighting Control Integration for Home Assistant
![Home Assistant](https://img.shields.io/badge/home%20assistant-%2341BDF5.svg?style=for-the-badge&logo=home-assistant&logoColor=white)
[![hacs](https://img.shields.io/badge/HACS-Integration-blue.svg?style=for-the-badge)](https://github.com/hacs/integration)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
<img src="assets/govee-logo.png" alt="Govee Logo" width="125">

A powerful and seamless integration to control your Govee BLE lighting devices
directly from Home Assistant. No bridges or cloud accounts required.
This repository includes the source from the original BLE control repository, as well as patches from [cralex96](https://github.com/cralex96/govee_ble_lights) and [Rombond](https://github.com/Rombond/h617a_govee_ble_lights), credit to them for their work.

Here is a compatibility table of different light models.

| Model | Change Color | Change Brightness | On/Off |
|-------|--------------|-------------------|--------|
| H617A | ✅           | ✅                | ✅     |
| H617C | ✅           | ✅                | ✅     |
| more..| ✅           | ✅                | ✅     |

---

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Support & Contribution](#support--contribution)
- [License](#license)

---

## Features

- 🚀 **Direct BLE Control**: No need for middlewares or bridges. Connect and control your Govee devices directly through Bluetooth Low Energy.

- 💡 **Comprehensive Lighting Control**: Adjust brightness, change colors, or switch on/off with ease.

- 🌈 **Effects**: Animated, segment-aware patterns on segmented models.

- ⏱️ **Transitions & Fades**: Smooth crossfades on color changes, plus a per-call `transition` option from automations.

---

## Installation

### HACS (recommended)

This integration is **not in the default HACS list yet**, so you need to add it as a custom repository:

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu (top right) and choose **Custom repositories**.
3. Paste `https://github.com/Laserology/govee_ble_lights` as the repository, choose **Integration** as the category, and click **Add**.
4. Search HACS for **Ultimate Govee BLE Lights** and download it.
5. Restart Home Assistant.

### Manual

Copy the `custom_components/govee-ble-lights` folder into your Home Assistant
`config/custom_components/` directory and restart Home Assistant.

---

## Configuration

### What is needed

For Direct BLE Control:

- Before you begin, make certain Home Assistant can access BLE on your platform. Ensure your Home Assistant instance is granted permissions to utilize the Bluetooth Low Energy of your host machine.

---

### A note on newly added models

The wire protocol (BLE characteristic UUIDs, 20-byte frame layout, command
bytes) is identical for every model in `config.json` — the per-model entry
only changes how the UI treats the device (segmented or not, brightness
scale). Most entries have been confirmed against real hardware over time by
contributors and issue reports.

A batch of models was added based on community documentation (other
reverse-engineering projects, Homebridge's Govee device list) rather than
direct hardware testing. They are very likely correct, since they use the
same documented protocol family, but haven't been verified against real
devices yet. Known limitation: models with a warm-white/CCT channel (e.g.
H6005, H6113) only get RGB control this way — color temperature isn't
encoded by this protocol.

If you own one of these and it works (or doesn't), please open a
[device compatibility report](https://github.com/Laserology/govee_ble_lights/issues/new?template=compatibility_report.md)
so the list can be corrected/confirmed over time.

## Usage

With the integration installed and Home Assistant restarted, add your light:

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for and select **Ultimate Govee BLE Lights**.
3. Choose **BLE** as the configuration type.
4. Select your device and its model.

Devices are discovered over Bluetooth automatically. When a device is added this way, the model is pre-selected from the advertisement name whenever it matches a supported model (e.g. `Govee_H617C_2482`), so you usually only have to confirm.

Each light then appears as an entity you can control (on/off, brightness, color) and use in automations:

- **Effects**: segmented models expose an **Effects** dropdown. Selecting `None` leaves effect mode and repaints the light with the last solid color used, so the pattern is actually cleared.

- **Transitions**: `light.turn_on` / `light.turn_off` accept a `transition` (seconds), e.g.:

  ```yaml
  service: light.turn_on
  target:
    entity_id: light.bedroom
  data:
    rgb_color: [255, 0, 0]
    transition: 3
  ```

Each light also reports diagnostic attributes for automations and support:

- `govee_model`, `govee_segments` (segmented models only)
- `govee_rssi` — last seen signal strength
- `govee_last_write_ms` — BLE write latency

The **Download diagnostics** button on the config entry exports the same info.

---

## Troubleshooting for BLE

If you're facing issues with the integration, consider the following steps:

1. **Check BLE Connection**:

   Ensure that the Govee device is within the Bluetooth range of your Home Assistant host machine.

2. **Model Check**:

   Check that you selected the correct device model.

3. **Logs**:

   Home Assistant logs can provide insights into any issues. Navigate to `Settings > System > Logs` to review any error messages related to the Govee integration.

---

## Support & Contribution

- **Found an Issue?**

   Raise it in the [Issues section](https://github.com/Laserology/govee_ble_lights/issues) of this repository.

- **Device support**:

   Almost every Govee device has its own BLE message protocol. If you find a model that doesn't work or has bugs, please report an issue here.

- **Contributions**:

   We welcome community contributions! If you'd like to improve the integration or add new features, please fork the repository and submit a pull request.

---

## License

This project is under the MIT License. For full license details, please refer to the [LICENSE file](https://github.com/Beshelmek/govee_ble_lights/blob/main/LICENSE) in this repository.
