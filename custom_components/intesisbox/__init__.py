"""IntesisBox Climate Platform."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

DOMAIN = "intesisbox"
PLATFORMS = ["climate"]

# Seconds to wait for the device to answer ID and every LIMITS query before
# giving up and letting Home Assistant retry the entry.
SETUP_TIMEOUT = 30


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the saved entities."""
    host = entry.data[CONF_HOST]

    from . import intesisbox

    controller = intesisbox.IntesisBox(host, loop=hass.loop)

    # Wait for the full handshake, not just the TCP connection: the climate
    # entity needs the LIMITS replies to build its mode and fan lists.
    if not await controller.async_connect(timeout=SETUP_TIMEOUT):
        controller.stop()
        raise ConfigEntryNotReady(f"Timed out connecting to IntesisBox at {host}")

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = controller

    if entry.unique_id is None:
        hass.config_entries.async_update_entry(
            entry, unique_id=controller.device_mac_address
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass, entry):
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        controller = hass.data[DOMAIN].pop(entry.entry_id)
        controller.stop()
    return unload_ok
