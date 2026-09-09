# Ultimate BLE Lighting Control Integration for HomeAssistant
![Home Assistant](https://img.shields.io/badge/home%20assistant-%2341BDF5.svg?style=for-the-badge&logo=home-assistant&logoColor=white)
[![hacs](https://img.shields.io/badge/HACS-Integration-blue.svg?style=for-the-badge)](https://github.com/hacs/integration)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
<img src="assets/govee-logo.png" alt="Govee Logo" width="125">

A powerful and seamless integration to control your Govee lighting devices via Govee API or BLE directly from HomeAssistant.
This repository includes the source from the orignal BLE control reposityory, as well as patches from [cralex96](https://github.com/cralex96/govee_ble_lights) and [Rombond](https://github.com/Rombond/h617a_govee_ble_lights), credit to them for their work.

Here is a compatability table of different light models.

| Model | Change Color | Change Brightness | On/Off |
|-------|--------------|-------------------|--------|
| H617A | ✅           | ✅                | ✅     |
| H617C | ✅           | ✅                | ✅     |
| more..| ✅           | ✅                | ✅     |

Segmented lighting is currently not supported.

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

- ☁️ **API Control**: Supported all light devices with full features support including scenes!

- 🌈 **Scene Selection**: Leverage the full potential of your Govee lights by choosing from all available scenes, transforming the ambiance of your room instantly.
  
- 💡 **Comprehensive Lighting Control**: Adjust brightness, change colors, or switch on/off with ease.

---

## Installation

- 1: (Install HACS (Home assistant comunity repository))[https://hacs.xyz/docs/use/]
- 2: Find the "Ultimate gove BLE lights control" plugin from the HACS side menu
- 3: Enjoy.

## Configuration

### What is needed

For Direct BLE Control:
- Before you begin, make certain HomeAssistant can access BLE on your platform. Ensure your HomeAssistant instance is granted permissions to utilize the Bluetooth Low Energy of your host machine.

For Govee API Control:
- Retrieve Govee-API-Key as described [here](https://developer.govee.com/reference/apply-you-govee-api-key), setup integration with API type ad fill your API key.

### Device data (`config.json`)

All bundled, model-specific data for the integration lives in a single file,
`custom_components/govee-ble-lights/config.json`. It is the only place device
models are listed or described — the code never hard-codes models:

```json
{
  "devices": {
    "H6006": {},
    "H6053": { "segmented": true },
    "H613A": { "brightness_percent": true },
    "H6199": { "segmented": true, "brightness_percent": true }
  }
}
```

Per-model options:

- `segmented` — the device has individually addressable LED segments and needs
  segment-aware color commands.
- `segments` — how many addressable segments the device has (segmented models
  only). Defaults to 15, the protocol limit; set it when a model differs (e.g.
  H6053 is 12).
- `layout` — how addresses map to visible bands (segmented models only):
  `sequential` (each address is one visible band) or `blended` (each address
  shows two bands, the second a blend with its neighbour — e.g. H617C).
  Defaults to `sequential`.
- `brightness_percent` — the device expects brightness as a percentage (0-100)
  instead of a raw byte (0-255).
- `effects` / `effects_file` — optional *model-specific* effects, merged on top
  of the shared effects below. `effects_file` is a path relative to the
  component directory, for definitions too large for `config.json`.

### Effects

Effects are defined once under the top-level `effects` key and are shared by
all models that can play them (segmented models). Definitions are *patterns*,
not per-device pixel maps, so one effect works across models with different
segment counts:

```json
{
  "effects": {
    "Christmas": {
      "colors": [[255, 0, 0], [255, 255, 255]],
      "step": 1.0
    }
  }
}
```

Segment `n` of the device is painted `colors[(n - 1) % len(colors)]` — the
Christmas palette above alternates red and white on any segment count. Adding
an effect is just adding another entry here; no code changes needed.

Effects can be **animated** by adding a `step` value in seconds: every step the
pattern shifts one segment along the strip, so the example above makes red and
white bands move, updating once per second. Omit `step` for a static pattern.

Fades are done in software (most Govee firmware has no native crossfade):
colors are interpolated and re-sent ~30 times per second. Set `fade` on an
effect (seconds) to crossfade between its frames instead of stepping —
equal `step` and `fade` makes the pattern morph continuously. Top-level
values control general transitions:

```json
{
  "fade": 0.5,
  "fade_on": 1.0,
  "fade_off": 1.0
}
```

- `fade` — default crossfade for plain color changes (0.5 s).
- `fade_on` — ramp-up duration when the light turns on (fades in from black).
- `fade_off` — fade-to-black before powering off.

All three default to 0 (instant) when absent.

**Automations:** the integration advertises the `transition` feature, so
`light.turn_on` / `light.turn_off` accept a `transition` (seconds) and it
overrides the configured fades for that call, e.g.:

```yaml
service: light.turn_on
target:
  entity_id: light.bedroom
data:
  rgb_color: [255, 0, 0]
  transition: 3
```

> **Strip blend quirk:** on some models (e.g. H617C) each addressable segment
> visually spans *two* bands, the second being a blend with the neighboring
> segment — a red/white palette reads as red, pink, white, pink. Set that
> model's `layout` to `blended` and the integration stretches each palette
> color across two addresses automatically, producing clean wide stripes;
> effect definitions stay simple. The transition band between different
> colors is physical and can't be blanked — pick palette colors whose
> transitions look intentional (warm white blends with red to a soft orange
> instead of pink).

The effect dropdown also lists a `None` entry: selecting it leaves effect mode
and repaints the whole light with the last solid color chosen before the
effect, so the pattern is actually cleared (not just deselected).

To confirm a model's segment count or add a model-specific effect/override,
edit its entry under `devices`.

> Note: effects paint segments directly, so they only appear on segmented
> models. If the strip layout looks wrong on your model, the `segments` count
> is probably off — adjust it in `config.json` and restart.

## Usage

With the integration setup, your Govee devices will appear as entities within HomeAssistant. All you need to do is select your device model when adding it.

---

## Troubleshooting for BLE

If you're facing issues with the integration, consider the following steps:

1. **Check BLE Connection**: 
   
   Ensure that the Govee device is within the Bluetooth range of your HomeAssistant host machine.

2. **Model Check**:

   Check that you selected correct device model.

3. **Logs**:

   HomeAssistant logs can provide insights into any issues. Navigate to `Configuration > Logs` to review any error messages related to the Govee integration.

---

## Support & Contribution

- **Found an Issue?** 
   
   Raise it in the [Issues section](https://github.com/Laserology/govee_ble_lights/issues) of this repository.

- **Device support**:

   Almost every Govee device has its own BLE message protocol. If you find a model that doesn't work or has bugs, please report an issue here.

- **Contributions**:

   We welcome community contributions! If you'd like to improve the integration or add new features, please fork the repository and submit a pull request.

---

## Future Plans

We aim to continuously improve this integration by:

- Supporting more Govee device models for BLE
- Enhancing the overall user experience and stability

## Development (tests)

The pure logic modules (`models`, `effects`, `layouts`) have unit tests — no
Home Assistant runtime or hardware needed:

```
python3 -m unittest discover -s tests
```

---

## License

This project is under the MIT License. For full license details, please refer to the [LICENSE file](https://github.com/Beshelmek/govee_ble_lights/blob/main/LICENSE) in this repository.
