"""Diagnostic sensors for an IntesisBox device."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory

from . import IntesisBoxConfigEntry
from .entity import IntesisBoxEntity
from .intesisbox import IntesisBox


async def async_setup_entry(
    hass, entry: IntesisBoxConfigEntry, async_add_entities
) -> None:
    """Add the diagnostic sensors for a config entry."""
    controller = entry.runtime_data
    async_add_entities(
        [
            IntesisBoxFaultCode(controller, entry.title),
            IntesisBoxSignalStrength(controller, entry.title),
        ]
    )


class IntesisBoxFaultCode(IntesisBoxEntity, SensorEntity):
    """The indoor unit's fault code, as the device reports it.

    Codes are manufacturer-specific, so this is the raw token: it gives a
    bug report, or a service technician, something concrete to look up.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Fault code"

    def __init__(self, controller: IntesisBox, device_name: str) -> None:
        """Set up the fault code sensor."""
        super().__init__(controller, device_name)
        self._attr_unique_id = f"{controller.device_mac_address}-fault_code"

    @property
    def native_value(self) -> str | None:
        """The current fault code."""
        return self._controller.error_code


class IntesisBoxSignalStrength(IntesisBoxEntity, SensorEntity):
    """Wi-Fi signal strength of the gateway, refreshed by every PONG.

    Disabled by default, as signal-strength diagnostics are throughout Home
    Assistant: it changes on every keepalive and would otherwise write a
    recorder row every 45 seconds for a value most people never look at.
    """

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_name = "Signal strength"

    def __init__(self, controller: IntesisBox, device_name: str) -> None:
        """Set up the signal strength sensor."""
        super().__init__(controller, device_name)
        self._attr_unique_id = f"{controller.device_mac_address}-rssi"

    @property
    def native_value(self) -> int | None:
        """The last reported RSSI, or None if the device has not said."""
        raw = self._controller.rssi
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None
