"""Tests for the config flow.

These run against a real Home Assistant instance from the test harness, so
the unique-id, abort and reconfigure machinery is Home Assistant's own rather
than a stand-in. The device itself is replaced by patching _async_identify,
which is the only point the flow touches the network.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intesisbox import DOMAIN
from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.data_entry_flow import FlowResultType

MAC = "001DC9A2C911"
OTHER_MAC = "001DC9FFFFFF"


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let the harness load custom_components/intesisbox."""


@pytest.fixture(autouse=True)
def _skip_entry_setup():
    """Creating an entry must not try to reach a device."""
    with patch("custom_components.intesisbox.async_setup_entry", return_value=True):
        yield


def _device(mac):
    """Stand in for the device the flow would contact."""
    return patch(
        "custom_components.intesisbox.config_flow._async_identify", return_value=mac
    )


async def _start_user_flow(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


# --------------------------------------------------------------------------
# #18 - test the connection before creating an entry
# --------------------------------------------------------------------------


async def test_user_flow_creates_entry_keyed_on_mac(hass):
    """A reachable device becomes an entry whose identity is its MAC."""
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with _device(MAC):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "ac-study.internal"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ac-study.internal"
    assert result["data"] == {CONF_HOST: "ac-study.internal"}
    assert result["result"].unique_id == MAC


async def test_user_flow_reports_an_unreachable_host(hass):
    """A host that never completes the handshake stays on the form."""
    result = await _start_user_flow(hass)

    with _device(None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.0.2.1"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert not hass.config_entries.async_entries(DOMAIN)


# --------------------------------------------------------------------------
# #17 - one device, one entry
# --------------------------------------------------------------------------


async def test_same_device_under_a_new_host_updates_the_entry(hass):
    """Re-adding a known box by another name must not open a second socket."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, data={CONF_HOST: "192.168.1.50"}
    )
    entry.add_to_hass(hass)

    result = await _start_user_flow(hass)
    with _device(MAC):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "ac-study.internal"}
        )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data[CONF_HOST] == "ac-study.internal"


# --------------------------------------------------------------------------
# #19 - change the address without losing the entry
# --------------------------------------------------------------------------


async def test_reconfigure_moves_the_same_device(hass):
    """A new address for the same MAC is accepted in place."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, data={CONF_HOST: "192.168.1.50"}
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with _device(MAC):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.77"}
        )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "192.168.1.77"
    assert entry.unique_id == MAC


async def test_reconfigure_refuses_a_different_device(hass):
    """Pointing an entry at some other box is a mistake, not a move."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, data={CONF_HOST: "192.168.1.50"}
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with _device(OTHER_MAC):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.77"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_HOST] == "192.168.1.50"


async def test_reconfigure_adopts_an_identity_for_a_never_connected_entry(hass):
    """An entry that never finished a handshake has no MAC yet; it gains one."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=None, data={CONF_HOST: "192.0.2.1"}
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with _device(MAC):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.77"}
        )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == MAC
    assert entry.data[CONF_HOST] == "192.168.1.77"


async def test_reconfigure_does_not_steal_another_entry_identity(hass):
    """A never-connected entry cannot be pointed at a box another entry owns."""
    owner = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, data={CONF_HOST: "192.168.1.50"}
    )
    owner.add_to_hass(hass)
    orphan = MockConfigEntry(
        domain=DOMAIN, unique_id=None, data={CONF_HOST: "192.0.2.1"}
    )
    orphan.add_to_hass(hass)

    result = await orphan.start_reconfigure_flow(hass)
    with _device(MAC):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert orphan.unique_id is None


# --------------------------------------------------------------------------
# The one place the flow touches the network, and the unexpected-error branch
# --------------------------------------------------------------------------


async def test_identify_returns_the_mac_and_always_closes_its_socket(hass):
    from custom_components.intesisbox import config_flow

    class FakeBox:
        instances: list = []
        ok = True
        device_mac_address = MAC

        def __init__(self, host, loop=None):
            self.stopped = False
            FakeBox.instances.append(self)

        async def async_connect(self, timeout=30):
            return FakeBox.ok

        def stop(self):
            self.stopped = True

    with patch.object(config_flow, "IntesisBox", FakeBox):
        assert await config_flow._async_identify(hass, "a-host") == MAC
        FakeBox.ok = False
        assert await config_flow._async_identify(hass, "a-host") is None
    assert len(FakeBox.instances) == 2
    assert all(box.stopped for box in FakeBox.instances)


async def test_user_flow_reports_an_unexpected_error(hass):
    result = await _start_user_flow(hass)
    with patch(
        "custom_components.intesisbox.config_flow._async_identify",
        side_effect=RuntimeError("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "x"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
