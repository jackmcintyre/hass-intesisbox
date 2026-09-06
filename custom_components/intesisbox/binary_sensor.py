"""Fault reporting for an IntesisBox-connected air conditioner."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory

from . import IntesisBoxConfigEntry
from .entity import IntesisBoxEntity
from .intesisbox import IntesisBox

# Read-only entities; nothing here writes to the device.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass, entry: IntesisBoxConfigEntry, async_add_entities
) -> None:
    """Add the fault sensor for a config entry."""
    async_add_entities([IntesisBoxFault(entry.runtime_data, entry.title)])


class IntesisBoxFault(IntesisBoxEntity, BinarySensorEntity):
    """On while the indoor unit reports a fault.

    Every status refresh carries ERRSTATUS (OK or ERR). Until now it was
    received, stored and read by nothing, so "the aircon stopped working"
    was invisible to Home Assistant.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "fault"

    def __init__(self, controller: IntesisBox, device_name: str) -> None:
        """Set up the fault sensor."""
        super().__init__(controller, device_name)
        self._attr_unique_id = f"{controller.device_mac_address}-fault"

    @property
    def is_on(self) -> bool | None:
        """True on ERR, False on OK, unknown until the device has said."""
        status = self._controller.error_status
        if status is None:
            return None
        return status.upper() != "OK"
