"""Set up a config entry end-to-end through Home Assistant's loader.

The controller is faked at the single point __init__ creates it; everything
downstream - platform forwarding, runtime_data, the device registry, entity
naming, availability, unload - is Home Assistant's own code running against
this integration's real modules.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intesisbox import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .test_climate import FakeController

MAC = "001DC9A2C911"


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let the harness load custom_components/intesisbox."""


async def _set_up(hass, fake):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        title="Study Air Con",
        data={CONF_HOST: "ac-study.internal"},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.intesisbox.IntesisBox", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_entry_sets_up_every_platform_on_one_device(hass):
    """Climate plus the three diagnostics, all attached to one device."""
    fake = FakeController(vertical_vane_list=["AUTO", "1", "2", "SWING"])
    entry = await _set_up(hass, fake)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is fake
    assert fake.connect_timeouts == [30]

    # One device, identified by MAC, named from the entry.
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    assert device is not None
    assert device.name == "Study Air Con"
    assert device.model == "TO-RC-WMP-1"
    assert device.sw_version == "v1.3.3"

    # Entities take the device's name, so ids and friendly names follow it.
    climate = hass.states.get("climate.study_air_con")
    assert climate is not None
    assert climate.attributes["friendly_name"] == "Study Air Con"

    fault = hass.states.get("binary_sensor.study_air_con_fault")
    assert fault is not None
    assert fault.state == "off"
    assert fault.attributes["friendly_name"] == "Study Air Con Fault"

    code = hass.states.get("sensor.study_air_con_fault_code")
    assert code is not None
    assert code.state == "0"

    # Signal strength is registered but disabled by default.
    registry = er.async_get(hass)
    rssi = registry.async_get("sensor.study_air_con_signal_strength")
    assert rssi is not None
    assert rssi.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get("sensor.study_air_con_signal_strength") is None

    for entity_id in (
        "climate.study_air_con",
        "binary_sensor.study_air_con_fault",
        "sensor.study_air_con_fault_code",
        "sensor.study_air_con_signal_strength",
    ):
        assert registry.async_get(entity_id).device_id == device.id


async def test_a_push_from_the_device_reaches_the_entities(hass):
    """State written on the controller's callback, not on a poll."""
    fake = FakeController()
    await _set_up(hass, fake)

    fake.error_status = "ERR"
    fake.error_code = "E7"
    fake.push()
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.study_air_con_fault").state == "on"
    assert hass.states.get("sensor.study_air_con_fault_code").state == "E7"


async def test_losing_the_socket_makes_the_entities_unavailable(hass):
    fake = FakeController()
    await _set_up(hass, fake)

    fake.is_connected = False
    fake.push()
    await hass.async_block_till_done()

    assert hass.states.get("climate.study_air_con").state == STATE_UNAVAILABLE
    assert (
        hass.states.get("binary_sensor.study_air_con_fault").state == STATE_UNAVAILABLE
    )


async def test_unload_stops_the_controller(hass):
    fake = FakeController()
    entry = await _set_up(hass, fake)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert fake.stopped is True
    # Registered entities keep a restored placeholder after unload; what
    # matters is that nothing is live behind it.
    assert hass.states.get("climate.study_air_con").state == STATE_UNAVAILABLE


async def test_a_failed_handshake_leaves_the_entry_retrying(hass):
    """ConfigEntryNotReady, and the controller it opened is stopped."""
    fake = FakeController()
    fake.connect_result = False
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data={CONF_HOST: "192.0.2.1"})
    entry.add_to_hass(hass)

    with patch("custom_components.intesisbox.IntesisBox", return_value=fake):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert fake.stopped is True


async def test_a_failed_platform_unload_keeps_the_controller_running(hass):
    fake = FakeController()
    entry = await _set_up(hass, fake)
    with patch.object(
        hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=False)
    ):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    assert fake.stopped is False
