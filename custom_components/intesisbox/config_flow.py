"""Config flow to configure the Intesisbox integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import DOMAIN, SETUP_TIMEOUT
from .intesisbox import IntesisBox

_LOGGER = logging.getLogger(__name__)

STEP_HOST_SCHEMA = vol.Schema({vol.Required(CONF_HOST): str})


async def _async_identify(hass: HomeAssistant, host: str) -> str | None:
    """Connect to a device, complete the handshake, and return its MAC.

    Returns None if the device cannot be reached or does not finish the
    handshake in time. The connection is always closed afterwards; the entry's
    own controller opens its own.
    """
    controller = IntesisBox(host, loop=hass.loop)
    try:
        if not await controller.async_connect(timeout=SETUP_TIMEOUT):
            return None
        return controller.device_mac_address
    finally:
        controller.stop()


class IntesisboxFlowHandler(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow."""

    VERSION = 1

    async def _async_try_host(self, host: str, errors: dict[str, str]) -> str | None:
        """Identify the device at host, recording a form error on failure."""
        try:
            mac = await _async_identify(self.hass, host)
        except Exception:  # noqa: BLE001 - anything else is a bug worth seeing
            _LOGGER.exception("Unexpected error connecting to IntesisBox at %s", host)
            errors["base"] = "unknown"
            return None
        if mac is None:
            errors["base"] = "cannot_connect"
        return mac

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by the user.

        The device is contacted before an entry is created, so a typo or a
        powered-off box is reported here rather than as an entry stuck
        retrying. The MAC from the ID reply is the entry's identity: the same
        box added again under a different hostname updates the existing entry
        instead of creating a second one, which matters because a WMP device
        allows only two TCP connections at once.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            mac = await self._async_try_host(host, errors)
            if mac is not None:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                return self.async_create_entry(title=host, data={CONF_HOST: host})

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_HOST_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the host of an existing entry in place.

        Deleting and re-adding an entry destroys its registry records - entity
        ids, names, areas, history. This keeps them. The MAC is checked so the
        entry cannot silently be pointed at a different device.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            mac = await self._async_try_host(host, errors)
            if mac is not None:
                await self.async_set_unique_id(mac)
                if entry.unique_id is None:
                    # An entry that never completed a handshake has no
                    # identity yet; adopt this one unless another entry
                    # already owns it.
                    other = self.hass.config_entries.async_entry_for_domain_unique_id(
                        DOMAIN, mac
                    )
                    if other is not None and other.entry_id != entry.entry_id:
                        return self.async_abort(reason="already_configured")
                else:
                    self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry, unique_id=mac, data_updates={CONF_HOST: host}
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_HOST_SCHEMA, user_input or entry.data
            ),
            errors=errors,
        )
