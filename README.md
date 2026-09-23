# Ultimate BLE Lighting Control Integration for HomeAssistant
![Home Assistant](https://img.shields.io/badge/home%20assistant-%2341BDF5.svg?style=for-the-badge&logo=home-assistant&logoColor=white)
[![hacs](https://img.shields.io/badge/HACS-Integration-blue.svg?style=for-the-badge)](https://github.com/hacs/integration)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
<img src="assets/govee-logo.png" alt="Govee Logo" width="125">

A powerful and seamless integration to control your Govee BLE lighting devices
directly from HomeAssistant.
This repository includes the source from the orignal BLE control reposityory, as well as patches from [cralex96](https://github.com/cralex96/govee_ble_lights) and [Rombond](https://github.com/Rombond/h617a_govee_ble_lights), credit to them for their work.

Here is a compatability table of different light models.

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

- 🌈 **Effects**: Animated, segment-aware patterns on segmented models, defined in `config.json`.

---

## Installation

- 1: (Install HACS (Home assistant comunity repository))[https://hacs.xyz/docs/use/]
- 2: Find the "Ultimate gove BLE lights control" plugin from the HACS side menu
- 3: Enjoy.

## Configuration

### What is needed

For Direct BLE Control:
- Before you begin, make certain HomeAssistant can access BLE on your platform. Ensure your HomeAssistant instance is granted permissions to utilize the Bluetooth Low Energy of your host machine.

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
    "Warm Christmas": {
      "colors": [[255, 0, 0], [255, 132, 43]],
      "step": 1.0,
      "fade": 0.9
    }
  }
}
```

Segment `n` of the device is painted `colors[(n - 1) % len(colors)]` — the
Warm Christmas palette above alternates red and warm white on any segment
count. Adding an effect is just adding another entry here; no code changes
needed.

Bundled effects: `Warm Christmas`, `Halloween`, `New Year`,
`Rainbow`, `Sunset`, `Ocean`, `Valentine`, `America`.

Effects can be **animated** by adding a `step` value in seconds: every step the
pattern shifts one segment along the strip, so the example above makes red and
warm white bands move. Omit `step` for a static pattern. Set `fade` to roughly
`step - 0.1` — the longest transition with a safe margin so it completes
before the next shift; `fade: 0` gives crisp one-segment jumps.

Fades are done in software (most Govee firmware has no native crossfade):
colors are interpolated and re-sent ~30 times per second. Set `fade` on an
effect (seconds) to crossfade between its frames instead of stepping — the
bundled animations use `step - 0.1`, the longest transition with a safe
margin so it completes before the next shift (`fade == step` runs
back-to-back without any settle and reads as choppy on a write-bound BLE
link; the exception is *directional* flows such as a multi-color rainbow,
where the pattern keeps moving one way and `fade == step` is the classic
smooth sweep. Rainbow frames are also heavy — one write per color — so give
it a `fade` window long enough to fit a frame, i.e. roughly one second for a
five-to-seven-color palette.)
Top-level values control general transitions:

```json
{
  "fade": 0.5,
  "fade_on": 1.0,
  "fade_off": 1.0
}
```

- `fade` — default crossfade for plain color changes (0.5 s).
- `fade_on` — ramp-up duration when the light turns on (fades in from black).
  A bare `light.turn_on` repaints the last known color, falling back to white
  if none was ever learned, so the light never powers on dark.
- `fade_off` — fade-to-black before powering off.

All three default to 0 (instant) when absent.

**Motion types:** animated effects advance one segment per step by default
(*shift*). Set `motion` to change how the pattern moves:

```json
"Pulse": {
  "colors": [[255, 105, 180]],
  "step": 0.8,
  "motion": "pulse"
}
```

- `shift` (default) — the pattern travels along the strip;
  `direction: "reverse"` travels the other way.
- `pulse` — the pattern stays put and breathes: brightness alternates
  between full and `pulse_low` (default 0.25) each step. Leave `fade` out
  (or equal `step`) for a seamless breathe.
- `wipe` — the pattern fills in from one end, one segment per step, resets
  to the full strip, and repeats; `direction: "reverse"` fills from the far
  end. The strip starts fully filled.

A bundled `Pulse` effect demonstrates the breathe.

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

The effect dropdown also lists a `None` entry: selecting it leaves effect mode
and repaints the whole light with the last solid color chosen before the
effect, so the pattern is actually cleared (not just deselected).

To confirm a model's segment count or add a model-specific effect/override,
edit its entry under `devices`.

> **Note:** effects paint segments directly, so they only appear on segmented
> models. If the strip pattern looks off on your model, the `segments` count
> is probably wrong — adjust it in `config.json` and restart.

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

With the integration setup, your Govee devices will appear as entities within HomeAssistant. All you need to do is select your device model when adding it.

When a device is added over Bluetooth, the model is pre-selected automatically from the advertisement name whenever it matches a bundled model (e.g. `Govee_H617C_2482`).

Each light also reports diagnostic attributes for automations and support:

- `govee_model`, `govee_segments` (segmented models only)
- `govee_rssi` — last seen signal strength
- `govee_last_write_ms` — BLE write latency; handy for judging how heavy an effect is

The "Download diagnostics" button on the config entry exports the same info.

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

The pure logic modules (`models`, `effects`) have unit tests — no
Home Assistant runtime or hardware needed:

```
python3 -m unittest discover -s tests
```

---

## License

This project is under the MIT License. For full license details, please refer to the [LICENSE file](https://github.com/Beshelmek/govee_ble_lights/blob/main/LICENSE) in this repository.
