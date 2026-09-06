"""IntesisBox Climate Platform."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .intesisbox import IntesisBox

DOMAIN = "intesisbox"
# climate first: it is the entity people look for. The diagnostics attach to
# the same device.
PLATFORMS = ["climate", "binary_sensor", "sensor"]

# Seconds to wait for the device to answer ID and every LIMITS query before
# giving up and letting Home Assistant retry the entry.
SETUP_TIMEOUT = 30

# The controller lives on the entry for the entry's lifetime. Every platform
# reads it from here, so there is no hand-maintained dict in hass.data to keep
# in step with setup and unload.
type IntesisBoxConfigEntry = ConfigEntry[IntesisBox]


async def async_setup_entry(hass: HomeAssistant, entry: IntesisBoxConfigEntry) -> bool:
    """Connect to the device and load its platforms."""
    host = entry.data[CONF_HOST]
    controller = IntesisBox(host, loop=hass.loop)

    # Wait for the full handshake, not just the TCP connection: the climate
    # entity needs the LIMITS replies to build its mode and fan lists.
    if not await controller.async_connect(timeout=SETUP_TIMEOUT):
        controller.stop()
        raise ConfigEntryNotReady(f"Timed out connecting to IntesisBox at {host}")

    entry.runtime_data = controller

    if entry.unique_id is None:
        hass.config_entries.async_update_entry(
            entry, unique_id=controller.device_mac_address
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IntesisBoxConfigEntry) -> bool:
    """Unload a config entry and close its connection."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry.runtime_data.stop()
    return unload_ok
