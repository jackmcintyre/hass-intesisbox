"""Tests for the diagnostics download.

#20 - a bug report used to need the device model, firmware, negotiated
limits and refused functions pulled out of debug logs by hand.
"""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intesisbox import DOMAIN
from custom_components.intesisbox.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.intesisbox.intesisbox import IntesisBox
from homeassistant.const import CONF_HOST

MAC = "001DC9A2C911"


async def test_diagnostics_describe_the_device_and_redact_its_identity(hass):
    """The controller's real snapshot, with address and MAC removed."""
    box = IntesisBox("ac-study.internal", loop=None)
    box.data_received(
        b"ID:TO-RC-WMP-1,001DC9A2C911,192.168.1.50,ASCII,v1.3.3,-54\r\n"
        b"LIMITS:FANSP,[AUTO,1,2,3]\r\n"
        b"CHN,1:ONOFF,OFF\r\nCHN,1:ERRSTATUS,OK\r\nCHN,1:ERRCODE,0\r\n"
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        title="Study Air Con",
        data={CONF_HOST: "ac-study.internal"},
    )
    entry.runtime_data = box

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["title"] == "Study Air Con"
    assert diag["entry"]["unique_id"] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_HOST] == "**REDACTED**"

    device = diag["device"]
    assert device["mac"] == "**REDACTED**"
    assert device["model"] == "TO-RC-WMP-1"
    assert device["firmware"] == "v1.3.3"
    assert device["rssi"] == "-54"
    assert device["limits"]["fan_speeds"] == ["AUTO", "1", "2", "3"]
    assert device["state"]["ERRSTATUS"] == "OK"
    assert device["vane_settable"] == {"VANEUD": True, "VANELR": True}
    assert device["ready"] is False
    assert device["tasks"] == []
