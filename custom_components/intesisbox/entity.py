"""Shared base for the entities of one IntesisBox device."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import DOMAIN
from .intesisbox import IntesisBox


class IntesisBoxEntity(Entity):
    """An entity that reads its state live from the device's controller.

    The controller pushes a callback on every change; the entity writes its
    state then. Nothing is cached here, so the properties always reflect the
    controller, and availability follows the socket.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: IntesisBox, device_name: str) -> None:
        """Bind to the controller and describe the device it belongs to."""
        self._controller = controller
        # The same identifier the climate entity uses, so every entity of
        # one box lands on one device in the registry.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, controller.device_mac_address)},
            name=device_name,
            manufacturer="Intesis",
            model=controller.device_model,
            sw_version=controller.firmware_version,
        )

    @property
    def available(self) -> bool:
        """Unavailable while the controller has no working connection."""
        return self._controller.is_connected

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller pushes, and unsubscribe on removal."""
        self._controller.add_update_callback(self._on_controller_update)
        self.async_on_remove(
            lambda: self._controller.remove_update_callback(self._on_controller_update)
        )

    def _on_controller_update(self) -> None:
        """Write state on a push. Runs on the event loop, from the protocol."""
        if self.hass and self.entity_id:
            self.async_write_ha_state()
