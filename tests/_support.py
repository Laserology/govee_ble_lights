"""Test support: expose the component as an importable package.

The component directory is named ``govee-ble-lights`` (hyphens), which cannot
be imported as a normal Python name. These tests register it under an alias so
``from govee_ble_lights import models`` works.
"""

import sys
import types
from pathlib import Path

PACKAGE_NAME = "govee_ble_lights"
COMPONENT_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "govee-ble-lights"
)


def ensure() -> None:
    """Register the package alias and add the component path (idempotent)."""
    if PACKAGE_NAME in sys.modules:
        return

    component = COMPONENT_DIR.resolve()
    sys.path.insert(0, str(component.parent))

    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(component)]
    package.__file__ = str(component / "__init__.py")
    sys.modules[PACKAGE_NAME] = package
