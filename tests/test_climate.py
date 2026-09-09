"""Tests for the climate entity layer.

These exercise IntesisBoxAC directly against a fake controller. They need
Home Assistant importable, unlike the transport tests, because the entity
subclasses ClimateEntity and the bugs worth catching live in how Home
Assistant reads its properties.
"""

from __future__ import annotations

import logging
from typing import Any

from custom_components.intesisbox.climate import IntesisBoxAC
from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.components.climate.const import ATTR_HVAC_MODE
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.util.unit_system import METRIC_SYSTEM


class FakeHass:
    """Just enough of HomeAssistant for ClimateEntity.state_attributes."""

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
        self._update_callbacks: list = []
        self.connect_timeouts: list[float] = []
        self.connect_result = True
        self.stopped = False

    @property
    def has_swing_control(self) -> bool:
        return len(self.vane_vertical_list) > 1 or len(self.vane_horizontal_list) > 1

    def add_update_callback(self, method):
        self._update_callbacks.append(method)

    def remove_update_callback(self, method):
        self._update_callbacks.remove(method)

    # -- lifecycle, for tests that set up a config entry end-to-end --------

    async def async_connect(self, timeout: float = 30) -> bool:
        self.connect_timeouts.append(timeout)
        return self.connect_result

    def stop(self) -> None:
        self.stopped = True

    def push(self) -> None:
        """Fire every registered update callback, as the device would."""
        for method in list(self._update_callbacks):
            method()

    async def async_set_temperature(self, value):
        self.calls.append(("temperature", value))

    async def async_set_mode(self, mode):
        self.calls.append(("mode", mode))
        self.mode = mode

    async def async_set_fan_speed(self, speed):
        self.calls.append(("fan_speed", speed))

    async def async_set_power_on(self):
        self.calls.append(("power", "ON"))
        self.is_on = True

    async def async_set_power_off(self):
        self.calls.append(("power", "OFF"))
        self.is_on = False

    async def async_set_vertical_vane(self, value):
        self.calls.append(("vane_ud", value))

    async def async_set_horizontal_vane(self, value):
        self.calls.append(("vane_lr", value))


def make_entity(controller: Any = None, **kwargs) -> IntesisBoxAC:
    """Build an entity with a stub hass and inert state writes."""
    fake: Any = controller if controller is not None else FakeController()
    entity = IntesisBoxAC(fake, **kwargs)
    entity.hass = FakeHass()
    entity.async_write_ha_state = lambda: None
    return entity


# --------------------------------------------------------------------------
# Entity added before its first update
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
# Modes the device reports
# --------------------------------------------------------------------------


def test_unknown_operation_mode_is_skipped(caplog):
    """One unrecognised mode must not cost the user every other mode."""
    controller = FakeController(operation_list=["HEAT", "WIBBLE", "COOL"])
    with caplog.at_level(logging.WARNING):
        entity = make_entity(controller)

    assert entity.hvac_modes == [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
    assert "WIBBLE" in caplog.text


def test_no_usable_modes_falls_back_to_the_standard_set(caplog):
    """A device with no mappable modes degrades instead of failing setup.

    The controller deliberately becomes ready without LIMITS replies, so a
    permanently empty mode list would otherwise retry setup forever against
    a device that will never answer differently.
    """
    controller = FakeController(operation_list=["WIBBLE"])
    with caplog.at_level(logging.WARNING):
        entity = make_entity(controller)

    assert HVACMode.OFF in entity.hvac_modes
    assert HVACMode.HEAT in entity.hvac_modes
    assert HVACMode.COOL in entity.hvac_modes


def test_empty_fan_list_degrades_instead_of_raising():
    """A device that ignores LIMITS:FANSP still gets an entity, minus fan."""
    controller = FakeController(fan_speed_list=[])
    entity = make_entity(controller)
    assert not entity.supported_features & ClimateEntityFeature.FAN_MODE
    assert entity.fan_mode is None


async def test_an_unmappable_reported_mode_is_never_a_bare_string():
    """hvac_mode must be an HVACMode or None; a raw token breaks state."""
    controller = FakeController()
    controller.mode = "WIBBLE"
    controller.is_on = True
    entity = make_entity(controller)
    await entity.async_update()
    assert entity.hvac_mode is None
    assert entity.state is None


# --------------------------------------------------------------------------
# Set point
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
    entity._target_temperature = None
    assert entity.target_temperature is None


async def test_mode_change_does_not_resend_setpoint():
    """Changing mode must not write a cached set point back to the device."""
    controller = FakeController()
    entity = make_entity(controller)
    entity._target_temperature = 21.0

    await entity.async_set_hvac_mode(HVACMode.HEAT)

    assert ("mode", "HEAT") in controller.calls
    assert not [c for c in controller.calls if c[0] == "temperature"]


async def test_explicit_set_temperature_still_writes():
    controller = FakeController()
    entity = make_entity(controller)
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 22.5})
    assert ("temperature", 22.5) in controller.calls


async def test_set_temperature_with_a_mode_sets_the_mode_first():
    controller = FakeController()
    entity = make_entity(controller)
    await entity.async_set_temperature(
        **{ATTR_HVAC_MODE: HVACMode.COOL, ATTR_TEMPERATURE: 21.0}
    )
    assert controller.calls == [("mode", "COOL"), ("temperature", 21.0)]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def test_turn_off_sends_power_off_not_mode():
    controller = FakeController()
    entity = make_entity(controller)
    await entity.async_set_hvac_mode(HVACMode.OFF)
    assert controller.calls == [("power", "OFF")]


async def test_turn_on_and_turn_off():
    controller = FakeController()
    entity = make_entity(controller)
    await entity.async_turn_on()
    await entity.async_turn_off()
    assert controller.calls == [("power", "ON"), ("power", "OFF")]


async def test_set_fan_mode_maps_to_the_device_speed():
    controller = FakeController()
    entity = make_entity(controller)
    await entity.async_set_fan_mode("medium")
    await entity.async_set_fan_mode("auto")
    assert controller.calls == [("fan_speed", "2"), ("fan_speed", "AUTO")]


async def test_swing_modes_drive_both_vanes():
    controller = FakeController(
        vertical_vane_list=["AUTO", "1", "SWING"],
        horizontal_vane_list=["AUTO", "1", "SWING"],
    )
    entity = make_entity(controller)
    assert entity.swing_modes == ["Auto", "Horizontal", "Vertical", "Both"]

    await entity.async_set_swing_mode("Both")
    await entity.async_set_swing_mode("Vertical")
    await entity.async_set_swing_mode("Auto")

    assert controller.calls == [
        ("vane_ud", "SWING"),
        ("vane_lr", "SWING"),
        ("vane_ud", "SWING"),
        ("vane_lr", "AUTO"),
        ("vane_ud", "AUTO"),
        ("vane_lr", "AUTO"),
    ]


def test_turn_on_off_features_advertised():
    entity = make_entity()
    assert entity.supported_features & ClimateEntityFeature.TURN_ON
    assert entity.supported_features & ClimateEntityFeature.TURN_OFF
    assert entity.supported_features & ClimateEntityFeature.FAN_MODE


# --------------------------------------------------------------------------
# Availability and lifecycle
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


async def test_climate_subscribes_on_add_and_unsubscribes_on_remove():
    """The controller is shared and entry-owned; the entity only listens."""
    controller = FakeController()
    entity = make_entity(controller)
    assert controller._update_callbacks == []

    await entity.async_added_to_hass()
    assert controller._update_callbacks == [entity.update_callback]

    entity._call_on_remove_callbacks()
    assert controller._update_callbacks == []
    assert controller.stopped is False


def test_update_callback_writes_only_once_the_entity_exists():
    entity = make_entity()
    forced: list[bool] = []
    entity.schedule_update_ha_state = lambda force=False: forced.append(force)  # type: ignore[method-assign]
    entity.entity_id = None
    entity.update_callback()
    assert forced == []
    entity.entity_id = "climate.study"
    entity.update_callback()
    assert forced == [True]


async def test_update_logs_connection_changes(caplog):
    controller = FakeController(connected=True)
    entity = make_entity(controller)
    controller.is_connected = False
    with caplog.at_level(logging.INFO):
        await entity.async_update()
        controller.is_connected = True
        await entity.async_update()
    assert "Lost connection" in caplog.text
    assert "restored" in caplog.text


# --------------------------------------------------------------------------
# The rest of the entity surface
# --------------------------------------------------------------------------


def test_icon_and_hvac_mode_follow_power_and_mode():
    entity = make_entity()
    entity._current_operation = HVACMode.HEAT
    entity._power = False
    assert entity.hvac_mode == HVACMode.OFF
    assert entity.icon is None
    entity._power = True
    assert entity.hvac_mode == HVACMode.HEAT
    assert entity.icon == "mdi:white-balance-sunny"


def test_limits_and_assumed_state():
    entity = make_entity(FakeController(connected=False))
    assert entity.min_temp == 18.0
    assert entity.max_temp == 29.0
    assert entity.assumed_state is True


def test_attributes_report_the_update_type():
    controller = FakeController()
    entity = make_entity(controller)
    assert entity.extra_state_attributes["ha_update_type"] == "push"
    controller.is_connected = False
    assert entity.extra_state_attributes["ha_update_type"] == "poll"


async def test_a_late_limits_reply_reaches_the_entity():
    """A LIMITS reply after readiness must still show up in the entity's lists.

    The controller becomes ready once the grace period expires; a slow unit
    can answer LIMITS:FANSP a moment after the entity was built.
    """
    controller = FakeController(fan_speed_list=[])
    entity = make_entity(controller)
    assert not entity.supported_features & ClimateEntityFeature.FAN_MODE

    controller.fan_speed_list = ["AUTO", "1", "2"]
    await entity.async_update()

    assert entity.supported_features & ClimateEntityFeature.FAN_MODE
    assert entity.fan_modes == ["auto", "low", "medium"]


async def test_a_yaml_entity_stops_the_controller_it_owns():
    """Nothing else owns a YAML platform's controller, so removal stops it."""
    controller = FakeController()
    entity = make_entity(controller, name="Lounge", owns_controller=True)
    await entity.async_added_to_hass()
    entity._call_on_remove_callbacks()
    assert controller.stopped is True
