"""Set up a config entry end-to-end through Home Assistant's loader.

The controller is faked at the single point __init__ creates it; everything
downstream, platform forwarding, runtime_data, the device registry, entity
naming and unload, is Home Assistant's own code running against this
integration's real modules.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intesisbox import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

from .test_climate import FakeController

MAC = "001DC9A2C911"


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let the harness load custom_components/intesisbox."""


async def _set_up(hass, fake, **entry_kwargs):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="192.0.2.10",
        data={CONF_HOST: "192.0.2.10"},
        **entry_kwargs,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.intesisbox.IntesisBox", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_entry_stores_the_controller_and_sets_up_the_climate(hass):
    fake = FakeController()
    fake.mode, fake.is_on, fake.ambient_temperature = "COOL", True, 24.5
    entry = await _set_up(hass, fake, unique_id=MAC)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is fake
    assert fake.connect_timeouts == [30]
    assert DOMAIN not in hass.data

    # One device, identified by MAC, and the entity named after it as before.
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    assert device is not None
    assert device.name == MAC
    assert device.model == "TO-RC-WMP-1"
    assert device.sw_version == "v1.3.3"

    state = hass.states.get("climate.001dc9a2c911")
    assert state is not None
    assert state.attributes["friendly_name"] == MAC
    assert state.state == "cool"
    assert state.attributes["current_temperature"] == 24.5
    assert er.async_get(hass).async_get("climate.001dc9a2c911").device_id == device.id


async def test_an_entry_without_a_unique_id_gets_the_mac(hass):
    fake = FakeController()
    entry = await _set_up(hass, fake)
    assert entry.unique_id == MAC


async def test_unload_stops_the_controller(hass):
    fake = FakeController()
    entry = await _set_up(hass, fake, unique_id=MAC)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert fake.stopped is True
    assert hass.states.get("climate.001dc9a2c911").state == STATE_UNAVAILABLE


async def test_a_failed_platform_unload_keeps_the_controller_running(hass):
    fake = FakeController()
    entry = await _set_up(hass, fake, unique_id=MAC)
    with patch.object(
        hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=False)
    ):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    assert fake.stopped is False


async def test_a_yaml_platform_keeps_its_configured_name(hass):
    """The YAML path registers no device, so the entity keeps its own name."""
    fake = FakeController()
    with patch("custom_components.intesisbox.climate.IntesisBox", return_value=fake):
        assert await async_setup_component(
            hass,
            "climate",
            {"climate": {"platform": DOMAIN, "host": "192.0.2.10", "name": "Lounge"}},
        )
        await hass.async_block_till_done()

    state = hass.states.get("climate.lounge")
    assert state is not None
    assert state.attributes["friendly_name"] == "Lounge"


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


async def test_a_push_from_the_device_reaches_the_entity(hass):
    """State written on the controller's callback, not on a poll."""
    fake = FakeController()
    await _set_up(hass, fake, unique_id=MAC)
    assert hass.states.get("climate.001dc9a2c911").state == "off"

    fake.mode, fake.is_on = "HEAT", True
    fake.push()
    await hass.async_block_till_done()

    assert hass.states.get("climate.001dc9a2c911").state == "heat"


async def test_losing_the_socket_makes_the_entity_unavailable(hass):
    fake = FakeController()
    await _set_up(hass, fake, unique_id=MAC)

    fake.is_connected = False
    fake.push()
    await hass.async_block_till_done()

    assert hass.states.get("climate.001dc9a2c911").state == STATE_UNAVAILABLE
