"""Support for IntesisBox Smart AC Controllers.

For more details about this platform, please refer to the documentation at
https://github.com/jnimmo/hass-intesisbox
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components.climate import (
    PLATFORM_SCHEMA,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.climate.const import ATTR_HVAC_MODE
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_HOST,
    CONF_NAME,
    CONF_UNIQUE_ID,
    UnitOfTemperature,
)
from homeassistant.exceptions import PlatformNotReady
import homeassistant.helpers.config_validation as cv

from . import DOMAIN, IntesisBoxConfigEntry
from .intesisbox import MODES, IntesisBox

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Intesisbox"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
    }
)

# All commands funnel through one TCP socket.
PARALLEL_UPDATES = 1

MAP_OPERATION_MODE_TO_HA = {
    "AUTO": HVACMode.HEAT_COOL,
    "FAN": HVACMode.FAN_ONLY,
    "HEAT": HVACMode.HEAT,
    "DRY": HVACMode.DRY,
    "COOL": HVACMode.COOL,
    "OFF": HVACMode.OFF,
}
MAP_OPERATION_MODE_TO_IB = {v: k for k, v in MAP_OPERATION_MODE_TO_HA.items()}

MAP_STATE_ICONS = {
    HVACMode.HEAT: "mdi:white-balance-sunny",
    HVACMode.HEAT_COOL: "mdi:cached",
    HVACMode.COOL: "mdi:snowflake",
    HVACMode.DRY: "mdi:water-off",
    HVACMode.FAN_ONLY: "mdi:fan",
}

FAN_MODE_I_TO_E = {
    "AUTO": "auto",
    "1": "low",
    "2": "medium",
    "3": "high",
    "4": "ultra high",
}
FAN_MODE_E_TO_I = {v: k for k, v in FAN_MODE_I_TO_E.items()}

SWING_ON = "SWING"
SWING_STOP = "AUTO"
SWING_LIST_HORIZONTAL = "Horizontal"
SWING_LIST_VERTICAL = "Vertical"
SWING_LIST_BOTH = "Both"
SWING_LIST_STOP = "Auto"


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Create the Intesisbox climate devices."""
    controller = IntesisBox(config[CONF_HOST], loop=hass.loop)
    if not await controller.async_connect():
        controller.stop()
        raise PlatformNotReady(
            f"Timed out connecting to IntesisBox at {config[CONF_HOST]}"
        )

    name = config.get(CONF_NAME)
    unique_id = config.get(CONF_UNIQUE_ID)
    async_add_entities(
        [IntesisBoxAC(controller, name, unique_id, owns_controller=True)], True
    )


async def async_setup_entry(
    hass, entry: IntesisBoxConfigEntry, async_add_entities
) -> None:
    """Add entries from config."""
    async_add_entities([IntesisBoxAC(entry.runtime_data)], True)


class IntesisBoxAC(ClimateEntity):
    """Represents an Intesisbox air conditioning device."""

    # The controller holds a socket open and pushes every change.
    _attr_should_poll = False

    def __init__(
        self,
        controller: IntesisBox,
        name: str | None = None,
        unique_id: str | None = None,
        owns_controller: bool = False,
    ):
        """Initialize the thermostat.

        owns_controller is set by the YAML platform, which has nothing else
        to stop the controller when the entity goes; a config entry owns its
        controller and stops it on unload.
        """
        _LOGGER.debug("Setting up climate device.")
        self._controller = controller
        self._owns_controller = owns_controller

        self._deviceid = controller.device_mac_address
        self._devicename = name or controller.device_mac_address
        self._unique_id = unique_id or controller.device_mac_address
        self._connected = controller.is_connected
        # From a config entry the device carries the name: the single climate
        # entity has none of its own, so its friendly name is the device's, as
        # it was, and renaming the device renames the entity with it. The
        # YAML platform registers no device, so there the entity keeps its
        # configured name as before.
        self._attr_has_entity_name = name is None
        # Disable compatibility mode until 2025.1 as per https://developers.home-assistant.io/blog/2024/01/24/climate-climateentityfeatures-expanded/
        self._enable_turn_on_off_backwards_compatibility = False

        self._max_temp = controller.max_setpoint
        self._min_temp = controller.min_setpoint
        self._target_temperature = None
        self._current_temp = None
        self._rssi = None
        self._swing_list: list[str] = []
        self._vswing = False
        self._hswing = False
        self._power = False
        self._current_operation: HVACMode | None = None
        self._fan_speed = None
        self._fan_list: list[str] = []
        self._operation_list: list[HVACMode] = []
        self._has_swing_control = False
        self._base_features = ClimateEntityFeature.TARGET_TEMPERATURE
        self._capabilities_seen: tuple = ()
        self._refresh_capabilities()

        _LOGGER.debug("Finished setting up climate entity!")

    def _capabilities(self) -> tuple:
        """Return the controller's negotiated limits as one comparable value."""
        c = self._controller
        return (
            tuple(c.fan_speed_list),
            tuple(c.operation_list),
            tuple(c.vane_vertical_list),
            tuple(c.vane_horizontal_list),
        )

    def _refresh_capabilities(self) -> None:
        """Build the mode, fan and swing lists from the controller's limits.

        Called on construction and again on every update, because a unit can
        answer a LIMITS query after the controller became ready without it:
        the grace period expires, the entity is built, and the reply lands a
        moment later. Rebuilding on update means it still reaches the entity.
        """
        self._capabilities_seen = self._capabilities()
        self._has_swing_control = self._controller.has_swing_control

        # Setup fan list. The controller deliberately becomes ready even when
        # the device ignores LIMITS:FANSP, so an empty list is a degraded
        # device, not a race: offer the entity without fan control rather
        # than failing setup forever.
        self._fan_list = [x.title() for x in self._controller.fan_speed_list]

        # Setup operation list. A mode the device reports but we cannot map is
        # skipped with a warning rather than raising, so one unrecognised token
        # does not cost the user every other mode on the unit.
        self._operation_list = [HVACMode.OFF]
        for operation in self._controller.operation_list:
            hvac_mode = MAP_OPERATION_MODE_TO_HA.get(operation)
            if hvac_mode is None:
                _LOGGER.warning(
                    "Ignoring unsupported operation mode %r reported by %s",
                    operation,
                    self._controller.device_mac_address,
                )
                continue
            self._operation_list.append(hvac_mode)
        if len(self._operation_list) == 1:
            # No usable modes reported (LIMITS:MODE ignored, or nothing
            # mapped): degrade to the standard WMP set instead of failing
            # setup forever. The device answers ERR to anything unsupported.
            _LOGGER.warning(
                "%s reported no usable operation modes; offering the standard set",
                self._controller.device_mac_address,
            )
            self._operation_list += [MAP_OPERATION_MODE_TO_HA[m] for m in MODES]

        # Setup feature support
        self._base_features = ClimateEntityFeature.TARGET_TEMPERATURE

        self._base_features |= ClimateEntityFeature.TURN_ON
        self._base_features |= ClimateEntityFeature.TURN_OFF

        if len(self._fan_list) > 0:
            self._base_features |= ClimateEntityFeature.FAN_MODE

        # Setup swing control
        self._swing_list = []
        if self._has_swing_control:
            self._base_features |= ClimateEntityFeature.SWING_MODE
            self._swing_list = [SWING_LIST_STOP]
            if SWING_ON in self._controller.vane_horizontal_list:
                self._swing_list.append(SWING_LIST_HORIZONTAL)
            if SWING_ON in self._controller.vane_vertical_list:
                self._swing_list.append(SWING_LIST_VERTICAL)
            if len(self._swing_list) > 2:
                self._swing_list.append(SWING_LIST_BOTH)

    @property
    def name(self) -> str | None:
        """The configured name for a YAML entity; none from a config entry."""
        if self._attr_has_entity_name:
            return None
        return self._devicename

    @property
    def unique_id(self):
        """Return the unique id of the AC device."""
        return self._unique_id

    @property
    def temperature_unit(self):
        """Intesisbox API uses celsius on the backend."""
        return UnitOfTemperature.CELSIUS

    @property
    def device_info(self):
        """Info about the IntesisBox itself."""
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self._devicename,
            "manufacturer": "Intesis",
            "model": self._controller.device_model,
            "sw_version": self._controller.firmware_version,
        }

    @property
    def extra_state_attributes(self):
        """Return the device specific state attributes."""
        attrs = {}
        if self._has_swing_control:
            attrs["vertical_swing"] = self._vswing
            attrs["horizontal_swing"] = self._hswing

        if self._controller.is_connected:
            attrs["ha_update_type"] = "push"
        else:
            attrs["ha_update_type"] = "poll"

        return attrs

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        _LOGGER.debug("async_set_temperature(%r)", kwargs)

        temperature = kwargs.get(ATTR_TEMPERATURE)
        operation_mode = kwargs.get(ATTR_HVAC_MODE)

        if operation_mode:
            await self.async_set_hvac_mode(operation_mode)

        if temperature:
            await self._controller.async_set_temperature(temperature)

    async def async_set_hvac_mode(self, hvac_mode):
        """Set operation mode.

        The set point is not resent on a mode change. The device keeps its own
        per-mode set point, and writing a cached value back overrode it.
        """
        _LOGGER.debug("async_set_hvac_mode(%s)", hvac_mode)
        if hvac_mode == HVACMode.OFF:
            await self._controller.async_set_power_off()
            self._power = False
        else:
            await self._controller.async_set_mode(MAP_OPERATION_MODE_TO_IB[hvac_mode])

        self.async_write_ha_state()

    async def async_turn_on(self):
        """Turn thermostat on."""
        await self._controller.async_set_power_on()
        self.async_write_ha_state()

    async def async_turn_off(self):
        """Turn thermostat off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_fan_mode(self, fan_mode):
        """Set fan mode (from quiet, low, medium, high, auto)."""
        target = FAN_MODE_E_TO_I.get(fan_mode, fan_mode)
        _LOGGER.debug(
            "async_set_fan_mode(%s) -> fan speed %s", fan_mode, target.upper()
        )
        await self._controller.async_set_fan_speed(target.upper())

    async def async_set_swing_mode(self, swing_mode):
        """Set the vanes."""
        if swing_mode == SWING_LIST_BOTH:
            await self._controller.async_set_vertical_vane(SWING_ON)
            await self._controller.async_set_horizontal_vane(SWING_ON)
        elif swing_mode == SWING_LIST_STOP:
            await self._controller.async_set_vertical_vane(SWING_STOP)
            await self._controller.async_set_horizontal_vane(SWING_STOP)
        elif swing_mode == SWING_LIST_HORIZONTAL:
            await self._controller.async_set_vertical_vane(SWING_STOP)
            await self._controller.async_set_horizontal_vane(SWING_ON)
        elif swing_mode == SWING_LIST_VERTICAL:
            await self._controller.async_set_vertical_vane(SWING_ON)
            await self._controller.async_set_horizontal_vane(SWING_STOP)

    async def async_update(self):
        """Copy values from controller dictionary to climate device."""
        # Reconnection is owned by the controller's own backoff loop; this only
        # mirrors the current state onto the entity.
        if self._capabilities() != self._capabilities_seen:
            self._refresh_capabilities()
        self._power = self._controller.is_on
        self._current_temp = self._controller.ambient_temperature
        self._min_temp = self._controller.min_setpoint
        self._max_temp = self._controller.max_setpoint
        self._target_temperature = self._controller.setpoint

        if self._controller.fan_speed:
            self._fan_speed = self._controller.fan_speed.title()

        # Operation mode. None for a mode we cannot map (or none received
        # yet): hvac_mode must only ever return an HVACMode or None, because
        # Home Assistant's state property raises on any other string.
        ib_mode = self._controller.mode
        self._current_operation = MAP_OPERATION_MODE_TO_HA.get(ib_mode)

        # Swing mode
        # Climate module only supports one swing setting.
        if self._has_swing_control:
            self._vswing = self._controller.vertical_swing == SWING_ON
            self._hswing = self._controller.horizontal_swing == SWING_ON

        # Track connection lost/restored.
        if self._connected != self._controller.is_connected:
            self._connected = self._controller.is_connected
            if self._connected:
                _LOGGER.info("Connection to IntesisBox was restored.")
            else:
                _LOGGER.warning("Lost connection to IntesisBox.")

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller pushes once added; unsubscribe on removal.

        The controller is owned by whoever created it, the config entry or
        the YAML platform, and the entry stops it on unload. The entity only
        listens, so disabling or removing it never takes the connection down.
        """
        self._controller.add_update_callback(self.update_callback)
        self.async_on_remove(
            lambda: self._controller.remove_update_callback(self.update_callback)
        )
        if self._owns_controller:
            self.async_on_remove(self._controller.stop)

    @property
    def icon(self):
        """Return the icon for the current state."""
        icon = None
        if self._power:
            icon = MAP_STATE_ICONS.get(self._current_operation)
        return icon

    def update_callback(self):
        """Let HA know there has been an update from the controller.

        Guarded on entity_id as well as hass: during update_before_add the
        entity already has hass but no entity_id yet, and writing state in
        that window raises NoEntitySpecifiedError. The platform writes the
        state itself as soon as the add completes, so nothing is lost.
        """
        _LOGGER.debug("IntesisBox sent a status update.")
        if self.hass and self.entity_id:
            self.schedule_update_ha_state(True)

    @property
    def min_temp(self):
        """Return the minimum temperature for the current mode of operation."""
        return self._min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature for the current mode of operation."""
        return self._max_temp

    @property
    def is_on(self):
        """Return true if on."""
        return self._power

    @property
    def hvac_modes(self):
        """List of available operation modes."""
        return self._operation_list

    @property
    def fan_mode(self):
        """Return the current fan mode, or None before the first update."""
        if self._fan_speed is None:
            return None
        return FAN_MODE_I_TO_E.get(self._fan_speed, self._fan_speed).lower()

    @property
    def swing_mode(self):
        """Return current swing mode."""
        if self._vswing and self._hswing:
            return SWING_LIST_BOTH
        if self._vswing:
            return SWING_LIST_VERTICAL
        if self._hswing:
            return SWING_LIST_HORIZONTAL
        return SWING_LIST_STOP

    @property
    def fan_modes(self):
        """List of available fan modes."""
        return [FAN_MODE_I_TO_E.get(mode.upper(), mode) for mode in self._fan_list]

    @property
    def swing_modes(self):
        """List of available swing positions."""
        return self._swing_list

    @property
    def assumed_state(self) -> bool:
        """If the device is not connected we have to assume state."""
        return not self._connected

    @property
    def available(self) -> bool:
        """Unavailable while the controller has no working connection."""
        return self._controller.is_connected

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._current_temp

    @property
    def hvac_mode(self):
        """Return the current mode of operation if unit is on."""
        if self._power:
            return self._current_operation
        return HVACMode.OFF

    @property
    def target_temperature(self):
        """Return the set point the device is reporting.

        The device reports a null set point (32768, mapped to None) in modes
        where one does not apply, so FAN mode already yields None on its own.
        Suppressing it while the unit is merely off loses the value from the
        card and from history.
        """
        return self._target_temperature

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._base_features
