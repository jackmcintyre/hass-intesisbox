"""Tests for the climate entity layer.

These exercise IntesisBoxAC directly against a fake controller. They need
Home Assistant importable (unlike the transport tests, which deliberately do
not), because the entity subclasses ClimateEntity and the bugs worth catching
live in how Home Assistant reads its properties.
"""

from __future__ import annotations

import logging

import pytest

from custom_components.intesisbox.climate import IntesisBoxAC
from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.util.unit_system import METRIC_SYSTEM


class FakeHass:
    """Just enough of HomeAssistant for ClimateEntity.state_attributes.

    state_attributes reads self.hass.config.units for precision and unit
    conversion. Attaching this lets the tests exercise the exact code path
    Home Assistant runs when it adds an entity.
    """

    class _Config:
        units = METRIC_SYSTEM

    config = _Config()


class FakeController:
    """Stands in for IntesisBox, recording the commands it is asked to send."""

    def __init__(
        self,
        *,
        operation_list=None,
        fan_speed_list=None,
        vertical_vane_list=None,
        horizontal_vane_list=None,
        connected=True,
    ):
        """Build a controller that has already completed its handshake."""
        self.operation_list = (
            operation_list
            if operation_list is not None
            else ["AUTO", "HEAT", "DRY", "COOL", "FAN"]
        )
        self.fan_speed_list = (
            fan_speed_list if fan_speed_list is not None else ["AUTO", "1", "2", "3"]
        )
        self.vane_vertical_list = vertical_vane_list or []
        self.vane_horizontal_list = horizontal_vane_list or []
        self.is_connected = connected

        self.device_mac_address = "001DC9A2C911"
        self.device_model = "TO-RC-WMP-1"
        self.firmware_version = "v1.3.3"
        self.min_setpoint = 18.0
        self.max_setpoint = 29.0

        self.mode = None
        self.fan_speed = None
        self.setpoint = None
        self.ambient_temperature = None
        self.vertical_swing = None
        self.horizontal_swing = None
        self.is_on = False

        self.calls: list[tuple[str, object]] = []
        self._update_callbacks = []

    @property
    def has_swing_control(self) -> bool:
        """Mirror the real controller's capability test."""
        return len(self.vane_horizontal_list) > 1 or len(self.vane_vertical_list) > 1

    def add_update_callback(self, method):
        """Record the entity's callback."""
        self._update_callbacks.append(method)

    async def async_set_temperature(self, value):
        """Record a set point write."""
        self.calls.append(("temperature", value))

    async def async_set_mode(self, mode):
        """Record a mode write."""
        self.calls.append(("mode", mode))
        self.mode = mode

    async def async_set_fan_speed(self, speed):
        """Record a fan speed write."""
        self.calls.append(("fan_speed", speed))

    async def async_set_power_on(self):
        """Record a power on."""
        self.calls.append(("power", "ON"))
        self.is_on = True

    async def async_set_power_off(self):
        """Record a power off."""
        self.calls.append(("power", "OFF"))
        self.is_on = False

    async def async_set_vertical_vane(self, value):
        """Record a vertical vane write."""
        self.calls.append(("vane_ud", value))

    async def async_set_horizontal_vane(self, value):
        """Record a horizontal vane write."""
        self.calls.append(("vane_lr", value))


def make_entity(controller=None, **kwargs) -> IntesisBoxAC:
    """Build an entity with a stub hass and inert state writes."""
    entity = IntesisBoxAC(controller or FakeController(), **kwargs)
    entity.hass = FakeHass()
    entity.async_write_ha_state = lambda: None
    return entity


# --------------------------------------------------------------------------
# Regression: entity added before its first update
# --------------------------------------------------------------------------


def test_fan_mode_before_first_update_does_not_raise():
    """fan_mode must not blow up while the speed is still unknown.

    Home Assistant writes an entity's state as soon as it is added, before any
    update has run. This previously did None.lower() and killed the add for
    every entity on the platform.
    """
    entity = make_entity()
    assert entity._fan_speed is None
    assert entity.fan_mode is None


def test_state_attributes_before_first_update():
    """The full attribute dict Home Assistant builds on add must not raise."""
    entity = make_entity()
    attrs = entity.state_attributes
    assert attrs["fan_mode"] is None
    assert attrs["temperature"] is None


def test_fan_mode_maps_numeric_speeds():
    """Numeric device speeds map to Home Assistant's named modes."""
    entity = make_entity()
    for device_value, expected in [
        ("Auto", "auto"),
        ("1", "low"),
        ("2", "medium"),
        ("3", "high"),
        ("4", "ultra high"),
    ]:
        entity._fan_speed = device_value
        assert entity.fan_mode == expected


# --------------------------------------------------------------------------
# #15 - set point must survive the unit being off
# --------------------------------------------------------------------------


def test_target_temperature_reported_while_off():
    """A powered-off unit still has a set point, and should report it."""
    entity = make_entity()
    entity._power = False
    entity._target_temperature = 21.0
    assert entity.target_temperature == 21.0


def test_target_temperature_none_when_device_reports_null():
    """In FAN mode the device sends the null set point, which becomes None."""
    entity = make_entity()
    entity._power = True
    entity._current_operation = HVACMode.FAN_ONLY
    # The controller maps 32768 to None, so the entity mirrors None.
    entity._target_temperature = None
    assert entity.target_temperature is None


# --------------------------------------------------------------------------
# #16 - do not override the device's set point
# --------------------------------------------------------------------------


async def test_mode_change_does_not_resend_setpoint():
    """Changing mode must not write a cached set point back to the device."""
    controller = FakeController()
    entity = make_entity(controller)
    entity._target_temperature = 21.0

    await entity.async_set_hvac_mode(HVACMode.HEAT)

    assert ("mode", "HEAT") in controller.calls
    assert not [c for c in controller.calls if c[0] == "temperature"]


async def test_explicit_set_temperature_still_writes():
    """An explicit set point request must still reach the device."""
    controller = FakeController()
    entity = make_entity(controller)

    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 22.5})

    assert ("temperature", 22.5) in controller.calls


async def test_turn_off_sends_power_off_not_mode():
    """Turning off must not cycle the mode."""
    controller = FakeController()
    entity = make_entity(controller)

    await entity.async_set_hvac_mode(HVACMode.OFF)

    assert controller.calls == [("power", "OFF")]


# --------------------------------------------------------------------------
# #10 - unmappable modes are skipped, not fatal
# --------------------------------------------------------------------------


def test_unknown_operation_mode_is_skipped(caplog):
    """One unrecognised mode must not cost the user every other mode."""
    controller = FakeController(operation_list=["HEAT", "WIBBLE", "COOL"])
    with caplog.at_level(logging.WARNING):
        entity = make_entity(controller)

    assert entity.hvac_modes == [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
    assert "WIBBLE" in caplog.text


def test_no_usable_modes_raises_not_ready():
    """If nothing maps, the platform should retry rather than half-load."""
    from homeassistant.exceptions import PlatformNotReady

    controller = FakeController(operation_list=["WIBBLE"])
    with pytest.raises(PlatformNotReady):
        make_entity(controller)


# --------------------------------------------------------------------------
# Capability reporting
# --------------------------------------------------------------------------


def test_no_swing_feature_when_device_ignores_vane_limits():
    """Real units never answer LIMITS:VANEUD, leaving the lists empty."""
    entity = make_entity(FakeController())
    assert not entity.supported_features & ClimateEntityFeature.SWING_MODE
    assert entity.swing_modes == []
    assert "swing_mode" not in entity.state_attributes


def test_swing_feature_when_vane_limits_are_reported():
    """A device that does answer gets swing control."""
    controller = FakeController(
        vertical_vane_list=["AUTO", "1", "2", "SWING"],
        horizontal_vane_list=["AUTO", "1", "2", "SWING"],
    )
    entity = make_entity(controller)
    assert entity.supported_features & ClimateEntityFeature.SWING_MODE
    assert "Both" in entity.swing_modes


def test_turn_on_off_features_advertised():
    """Home Assistant needs these declared for the turn_on/turn_off actions."""
    entity = make_entity()
    assert entity.supported_features & ClimateEntityFeature.TURN_ON
    assert entity.supported_features & ClimateEntityFeature.TURN_OFF
    assert entity.supported_features & ClimateEntityFeature.FAN_MODE


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


def test_unavailable_when_controller_disconnected():
    """Availability must track the socket, not a slow retry counter."""
    controller = FakeController(connected=False)
    entity = make_entity(controller)
    assert entity.available is False

    controller.is_connected = True
    assert entity.available is True


def test_entity_does_not_poll():
    """The controller pushes changes, so Home Assistant should not poll."""
    assert make_entity().should_poll is False
