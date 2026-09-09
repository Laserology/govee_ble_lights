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
    _install_deps()
    if PACKAGE_NAME in sys.modules:
        return

    component = COMPONENT_DIR.resolve()
    sys.path.insert(0, str(component.parent))

    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(component)]
    package.__file__ = str(component / "__init__.py")
    sys.modules[PACKAGE_NAME] = package


def _install_deps() -> None:
    """Stub third-party modules the component imports when they are not
    installed, so tests can run without a Home Assistant / BLE stack."""
    for name, attrs in (
        ("bleak", {"BleakClient": type("BleakClient", (), {})}),
        ("bleak_retry_connector", {"establish_connection": lambda *a, **k: None}),
    ):
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except ImportError:
            module = types.ModuleType(name)
            for attr, value in attrs.items():
                setattr(module, attr, value)
            sys.modules[name] = module
