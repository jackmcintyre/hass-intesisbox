"""Support for IntesisBox Smart AC Controllers.

For more details about this platform, please refer to the documentation at
https://github.com/jnimmo/hass-intesisbox
"""

from __future__ import annotations

from datetime import timedelta
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
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.exceptions import PlatformNotReady
import homeassistant.helpers.config_validation as cv

from . import DOMAIN, SETUP_TIMEOUT
from .intesisbox import IntesisBox

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Intesisbox"

# All commands funnel through one TCP socket.
PARALLEL_UPDATES = 1

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
    }
)

# Return cached results if last scan time was less than this value.
# If a persistent connection is established for the controller, changes to
# values are in realtime.
SCAN_INTERVAL = timedelta(seconds=300)

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

# The device speaks AUTO, 1-9 and SWING on each vane axis. Present those as
# readable labels rather than raw protocol tokens.
VANE_I_TO_E = {"AUTO": "auto", "SWING": "swing"}


def vane_to_ha(value: str | None) -> str | None:
    """Map a device vane value to the mode shown in Home Assistant."""
    if value is None:
        return None
    return VANE_I_TO_E.get(value.upper(), value)


def vane_to_device(mode: str) -> str:
    """Map a Home Assistant swing mode back to the device value."""
    return {v: k for k, v in VANE_I_TO_E.items()}.get(mode, mode).upper()


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Create the Intesisbox climate devices."""
    from . import intesisbox

    controller = intesisbox.IntesisBox(config[CONF_HOST], loop=hass.loop)
    if not await controller.async_connect(timeout=SETUP_TIMEOUT):
        controller.stop()
        raise PlatformNotReady(
            f"Timed out connecting to IntesisBox at {config[CONF_HOST]}"
        )

    name = config.get(CONF_NAME)
    unique_id = config.get(CONF_UNIQUE_ID)
    async_add_entities([IntesisBoxAC(controller, name, unique_id)], True)


async def async_setup_entry(hass, entry, async_add_entities):
    """Add entries from config."""
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([IntesisBoxAC(controller)], True)


class IntesisBoxAC(ClimateEntity):
    """Represents an Intesisbox air conditioning device."""

    def __init__(
        self,
        controller: IntesisBox,
        name: str | None = None,
        unique_id: str | None = None,
    ):
        """Initialize the thermostat."""
        _LOGGER.debug("Setting up climate device.")
        self._controller = controller

        self._deviceid = controller.device_mac_address
        self._devicename = name or controller.device_mac_address
        self._unique_id = unique_id or controller.device_mac_address
        self._connected = controller.is_connected
        # Disable compatibility mode until 2025.1 as per https://developers.home-assistant.io/blog/2024/01/24/climate-climateentityfeatures-expanded/
        self._enable_turn_on_off_backwards_compatibility = False

        self._max_temp = controller.max_setpoint
        self._min_temp = controller.min_setpoint
        self._target_temperature = None
        self._current_temp = None
        self._rssi = None
        self._vane_vertical = None
        self._vane_horizontal = None
        self._power = False
        self._current_operation = STATE_UNKNOWN

        # Setup fan list
        self._fan_list = [x.title() for x in self._controller.fan_speed_list]
        if len(self._fan_list) < 1:
            raise PlatformNotReady("Controller hasn't finished initializing device")
        self._fan_speed = None

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
                    controller.device_mac_address,
                )
                continue
            self._operation_list.append(hvac_mode)
        if len(self._operation_list) == 1:
            raise PlatformNotReady("Controller reported no usable operation modes")

        # Setup feature support
        self._base_features = ClimateEntityFeature.TARGET_TEMPERATURE

        self._base_features |= ClimateEntityFeature.TURN_ON
        self._base_features |= ClimateEntityFeature.TURN_OFF

        if len(self._fan_list) > 0:
            self._base_features |= ClimateEntityFeature.FAN_MODE

        # Setup swing control. Home Assistant models the two vane axes
        # separately, so each is offered only if the unit actually has it -
        # many units report VANEUD and no VANELR at all.
        self._swing_list = [vane_to_ha(v) for v in self._controller.vane_vertical_list]
        self._swing_horizontal_list = [
            vane_to_ha(v) for v in self._controller.vane_horizontal_list
        ]
        # Swing features are not folded into _base_features: a unit that
        # reports a vane position but refuses to be commanded to one is only
        # discovered when it rejects a write, so the feature has to be able to
        # disappear at runtime. See supported_features.

        _LOGGER.debug("Finished setting up climate entity!")
        self._controller.add_update_callback(self.update_callback)

    @property
    def name(self):
        """Return the name of the AC device."""
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
            "name": self.name,
            "manufacturer": "Intesis",
            "model": self._controller.device_model,
            "sw_version": self._controller.firmware_version,
        }

    @property
    def extra_state_attributes(self):
        """Return the device specific state attributes."""
        # Position is still reported even where the vane cannot be commanded -
        # reading it works on hardware where writing does not.
        attrs = {}
        if self._vane_vertical is not None:
            attrs["vertical_swing"] = self._vane_vertical
        if self._vane_horizontal is not None:
            attrs["horizontal_swing"] = self._vane_horizontal

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
        """Set operation mode."""
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
        """Set the up/down vane position."""
        await self._controller.async_set_vertical_vane(vane_to_device(swing_mode))

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode):
        """Set the left/right vane position."""
        await self._controller.async_set_horizontal_vane(
            vane_to_device(swing_horizontal_mode)
        )

    async def async_update(self):
        """Copy values from controller dictionary to climate device."""
        # Reconnection is owned by the controller's own backoff loop; this only
        # mirrors the current state onto the entity.
        self._power = self._controller.is_on
        self._current_temp = self._controller.ambient_temperature
        self._min_temp = self._controller.min_setpoint
        self._max_temp = self._controller.max_setpoint
        self._target_temperature = self._controller.setpoint

        if self._controller.fan_speed:
            self._fan_speed = self._controller.fan_speed.title()

        # Operation mode
        ib_mode = self._controller.mode
        self._current_operation = MAP_OPERATION_MODE_TO_HA.get(ib_mode, STATE_UNKNOWN)

        # Vane positions, one per axis.
        self._vane_vertical = vane_to_ha(self._controller.vertical_swing)
        self._vane_horizontal = vane_to_ha(self._controller.horizontal_swing)

        # Track connection lost/restored.
        if self._connected != self._controller.is_connected:
            self._connected = self._controller.is_connected
            if self._connected:
                _LOGGER.info("Connection to IntesisBox was restored.")
            else:
                _LOGGER.warning("Lost connection to IntesisBox.")

    async def async_will_remove_from_hass(self):
        """Shutdown the controller when the device is being removed."""
        self._controller.stop()

    @property
    def icon(self):
        """Return the icon for the current state."""
        icon = None
        if self._power:
            icon = MAP_STATE_ICONS.get(self._current_operation)
        return icon

    def update_callback(self):
        """Let HA know there has been an update from the controller."""
        _LOGGER.debug("IntesisBox sent a status update.")
        if self.hass:
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
    def should_poll(self):
        """No polling: the controller holds a socket open and pushes changes."""
        return False

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
        """Return the current up/down vane position."""
        return self._vane_vertical

    @property
    def swing_horizontal_mode(self):
        """Return the current left/right vane position."""
        return self._vane_horizontal

    @property
    def fan_modes(self):
        """List of available fan modes."""
        return [FAN_MODE_I_TO_E.get(mode.upper(), mode) for mode in self._fan_list]

    @property
    def swing_modes(self):
        """Available up/down vane positions."""
        if not self._controller.has_vertical_vane:
            return []
        return self._swing_list

    @property
    def swing_horizontal_modes(self):
        """Available left/right vane positions."""
        if not self._controller.has_horizontal_vane:
            return []
        return self._swing_horizontal_list

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

        No need to second-guess this by power state: the device reports a null
        set point (32768, mapped to None) in modes where one does not apply, so
        FAN mode already yields None on its own. Suppressing it while the unit
        is merely off just loses the value from the card and from history.
        """
        return self._target_temperature

    @property
    def supported_features(self):
        """Return the currently supported features.

        Swing is evaluated per read rather than cached, because a vane the
        device refuses to set is only discovered from an ERR after the entity
        already exists.
        """
        features = self._base_features
        if self._controller.has_vertical_vane:
            features |= ClimateEntityFeature.SWING_MODE
        if self._controller.has_horizontal_vane:
            features |= ClimateEntityFeature.SWING_HORIZONTAL_MODE
        return features
