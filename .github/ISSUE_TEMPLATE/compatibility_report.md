---
name: Device compatibility report
about: Report whether a Govee model works (or doesn't) with this integration
labels: compatibility
---

## Device model

<!-- SKU code, format H**** (e.g. H6127) -->

## Result

<!-- Delete the lines that don't apply -->

- ✅ Works: power, brightness and color all respond correctly.
- ⚠️ Partially works: describe what does and doesn't work (e.g. color OK,
  brightness ignored, warm-white/CCT channel not supported).
- ❌ Does not work: connects but never responds to commands.
- ❌ Does not even connect / not discovered.

## Segmentation

<!-- Only relevant for RGBIC / multi-zone strips -->

- Number of physical LED segments on the device:
- Does `"segmented": true` in `config.json` produce correct per-segment
  effects, or does the whole strip react as one zone?

## Version of the integration

<!-- Check const.py / manifest.json if unsure -->

## Debug log

<!-- Enable debug logging: https://www.home-assistant.io/components/logger/ -->

```text
Add relevant log lines here (connection + a few command writes are enough).
```
