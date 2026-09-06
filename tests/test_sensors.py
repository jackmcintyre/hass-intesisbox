"""Tests for the diagnostic entities: fault, fault code, signal strength.

#14 - the device reports ERRSTATUS, ERRCODE and RSSI on every refresh, and
the integration used to receive all three and discard them.
"""

from __future__ import annotations

from custom_components.intesisbox import DOMAIN, binary_sensor, sensor
from custom_components.intesisbox.binary_sensor import IntesisBoxFault
from custom_components.intesisbox.climate import IntesisBoxAC
from custom_components.intesisbox.sensor import (
    IntesisBoxFaultCode,
    IntesisBoxSignalStrength,
)
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory

from .test_climate import FakeController, FakeHass


def _attach(entity):
    entity.hass = FakeHass()
    return entity


def test_fault_is_off_while_the_unit_reports_ok():
    controller = FakeController()
    fault = _attach(IntesisBoxFault(controller, "Study Air Con"))
    assert fault.is_on is False
    assert fault.device_class is BinarySensorDeviceClass.PROBLEM
    assert fault.entity_category is EntityCategory.DIAGNOSTIC


def test_fault_turns_on_when_the_unit_reports_err():
    controller = FakeController()
    fault = _attach(IntesisBoxFault(controller, "Study Air Con"))
    controller.error_status = "ERR"
    assert fault.is_on is True


def test_fault_is_unknown_until_the_device_has_said():
    controller = FakeController()
    controller.error_status = None
    fault = _attach(IntesisBoxFault(controller, "Study Air Con"))
    assert fault.is_on is None


def test_fault_code_is_reported_verbatim():
    controller = FakeController()
    controller.error_code = "E7"
    code = _attach(IntesisBoxFaultCode(controller, "Study Air Con"))
    assert code.native_value == "E7"
    assert code.entity_category is EntityCategory.DIAGNOSTIC


def test_signal_strength_is_an_integer_in_dbm():
    controller = FakeController()
    rssi = _attach(IntesisBoxSignalStrength(controller, "Study Air Con"))
    assert rssi.native_value == -54
    assert rssi.native_unit_of_measurement == "dBm"
    assert rssi.device_class is SensorDeviceClass.SIGNAL_STRENGTH
    # Chatty diagnostics are disabled by default, as elsewhere in HA.
    assert rssi.entity_registry_enabled_default is False


def test_signal_strength_tolerates_garbage():
    controller = FakeController()
    controller.rssi = "n/a"
    rssi = _attach(IntesisBoxSignalStrength(controller, "Study Air Con"))
    assert rssi.native_value is None
    controller.rssi = None
    assert rssi.native_value is None


def test_diagnostics_are_unavailable_when_the_socket_is_down():
    controller = FakeController(connected=False)
    fault = _attach(IntesisBoxFault(controller, "Study Air Con"))
    assert fault.available is False
    controller.is_connected = True
    assert fault.available is True


def test_all_entities_land_on_the_same_device():
    """The climate entity and the diagnostics must share one device."""
    controller = FakeController()
    climate = IntesisBoxAC(controller, name="Study Air Con")
    fault = IntesisBoxFault(controller, "Study Air Con")
    code = IntesisBoxFaultCode(controller, "Study Air Con")

    expected = {(DOMAIN, controller.device_mac_address)}
    assert climate.device_info["identifiers"] == expected
    assert fault.device_info["identifiers"] == expected
    assert code.device_info["identifiers"] == expected
    assert {climate.unique_id, fault.unique_id, code.unique_id} == {
        controller.device_mac_address,
        f"{controller.device_mac_address}-fault",
        f"{controller.device_mac_address}-fault_code",
    }


def test_read_only_platforms_declare_no_parallel_updates():
    assert sensor.PARALLEL_UPDATES == 0
    assert binary_sensor.PARALLEL_UPDATES == 0


def test_diagnostic_entities_use_translation_keys():
    """Names come from translations/en.json, not hard-coded strings."""
    controller = FakeController()
    assert IntesisBoxFault(controller, "x").translation_key == "fault"
    assert IntesisBoxFaultCode(controller, "x").translation_key == "fault_code"
    assert (
        IntesisBoxSignalStrength(controller, "x").translation_key == "signal_strength"
    )


async def test_diagnostic_entity_unsubscribes_on_remove():
    controller = FakeController()
    fault = _attach(IntesisBoxFault(controller, "x"))
    await fault.async_added_to_hass()
    assert len(controller._update_callbacks) == 1
    fault._call_on_remove_callbacks()
    assert controller._update_callbacks == []
    assert controller.stopped is False
