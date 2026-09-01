"""Communication with an Intesisbox device."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

_LOGGER = logging.getLogger(__name__)

API_DISCONNECTED = "Disconnected"
API_CONNECTING = "Connecting"
API_AUTHENTICATED = "Connected"

POWER_ON = "ON"
POWER_OFF = "OFF"
POWER_STATES = [POWER_ON, POWER_OFF]

MODE_AUTO = "AUTO"
MODE_DRY = "DRY"
MODE_FAN = "FAN"
MODE_COOL = "COOL"
MODE_HEAT = "HEAT"
MODES = [MODE_AUTO, MODE_DRY, MODE_FAN, MODE_COOL, MODE_HEAT]

FUNCTION_ONOFF = "ONOFF"
FUNCTION_MODE = "MODE"
FUNCTION_SETPOINT = "SETPTEMP"
FUNCTION_FANSP = "FANSP"
FUNCTION_VANEUD = "VANEUD"
FUNCTION_VANELR = "VANELR"
FUNCTION_AMBTEMP = "AMBTEMP"
FUNCTION_ERRSTATUS = "ERRSTATUS"
FUNCTION_ERRCODE = "ERRCODE"

NULL_VALUES = ["-32768", "32768"]

# The WMP spec (v1.11 §Overview) states the device closes the TCP connection
# after 1 minute without traffic, and recommends a keepalive every 30-60s.
KEEPALIVE_INTERVAL = 45
# Full status refresh. Changes arrive spontaneously as CHN messages, so this is
# only a backstop against missed pushes.
STATUS_POLL_INTERVAL = 60 * 5
# Ambient temperature is pushed on change, but poll it as well so a missed push
# cannot leave the reported temperature stale indefinitely.
AMBTEMP_POLL_INTERVAL = 60

# The spec (§Considerations iii) requires more than 1 second between opening and
# closing TCP sockets. Backoff starts there and grows to avoid hammering a device
# that is powered off.
RECONNECT_MIN_DELAY = 1.5
RECONNECT_MAX_DELAY = 60

# Gap between successive commands. The spec (§FAQs) says commands cannot be
# batched, but imposes no minimum spacing - the 1 second rule applies to socket
# open/close cycles, not to commands on an established connection.
COMMAND_INTERVAL = 0.2

# How long to wait for the device to confirm a mode change before giving up.
CONFIRM_TIMEOUT = 10

# Commands sent on connect. The device is not considered ready until every one
# of these has been answered.
INIT_COMMANDS = [
    "ID",
    f"LIMITS:{FUNCTION_SETPOINT}",
    f"LIMITS:{FUNCTION_FANSP}",
    f"LIMITS:{FUNCTION_MODE}",
    f"LIMITS:{FUNCTION_VANEUD}",
    f"LIMITS:{FUNCTION_VANELR}",
]

background_tasks = set()


def clean_background_task(task):
    """Handle background task completion, logging any unexpected failure."""
    background_tasks.discard(task)
    if task.cancelled():
        return
    if exc := task.exception():
        _LOGGER.error("Background task failed: %r", exc)


def ensure_background_task(coro, loop):
    """Schedule a coroutine on the given loop and keep a reference to it."""
    task = asyncio.ensure_future(coro, loop=loop)
    background_tasks.add(task)
    task.add_done_callback(clean_background_task)
    return task


class IntesisBox(asyncio.Protocol):
    """Handles communication with an intesisbox device via WMP."""

    def __init__(self, ip: str, port: int = 3310, loop=None):
        """Set up base state."""
        self._ip = ip
        self._port = port
        self._mac = None
        self._device: dict[str, str] = {}
        self._connectionStatus = API_DISCONNECTED
        self._transport: asyncio.Transport | None = None
        self._updateCallbacks: list[Callable[[], None]] = []
        self._errorCallbacks: list[Callable[[str], None]] = []
        self._errorMessage: str | None = None
        self._controllerType = None
        self._model: str | None = None
        self._firmversion: str | None = None
        self._rssi: str | None = None
        self._eventLoop = loop

        # Receive buffer. TCP is a byte stream, so a single data_received() may
        # carry a partial line, several lines, or both.
        self._buffer = b""

        # Outbound commands are serialised through a queue drained by a single
        # writer task, so every write happens on the event loop thread and no
        # two callers can interleave.
        self._write_queue: asyncio.Queue[str] = asyncio.Queue()

        # Set once ID and every LIMITS reply have been received.
        self._ready = asyncio.Event()
        self._pending_init: set[str] = set()

        # Waiters for a specific function reaching a specific value, used to
        # confirm a change instead of polling for it.
        self._change_waiters: list[tuple[str, str, asyncio.Future]] = []

        # Owned tasks, cancelled and replaced on every (re)connect.
        self._tasks: dict[str, asyncio.Task] = {}
        self._stopped = False

        # Limits
        self._operation_list: list[str] = []
        self._fan_speed_list: list[str] = []
        self._vertical_vane_list: list[str] = []
        self._horizontal_vane_list: list[str] = []
        self._setpoint_minimum: float | None = None
        self._setpoint_maximum: float | None = None

    # ------------------------------------------------------------------
    # Task ownership
    # ------------------------------------------------------------------

    def _start_task(self, name: str, coro) -> None:
        """Start a named task, cancelling any previous instance first.

        Without this, a reconnect that lands inside a poller's sleep leaves the
        old task alive alongside the new one and the traffic doubles each time.
        """
        self._cancel_task(name)
        self._tasks[name] = ensure_background_task(coro, self._eventLoop)

    def _cancel_task(self, name: str) -> None:
        """Cancel a named task if it is running."""
        task = self._tasks.pop(name, None)
        if task and not task.done():
            task.cancel()

    def _cancel_all_tasks(self) -> None:
        """Cancel every owned task."""
        for name in list(self._tasks):
            self._cancel_task(name)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport):
        """Asyncio callback for a successful connection."""
        _LOGGER.debug("Connected to IntesisBox %s:%s", self._ip, self._port)
        self._transport = transport  # type: ignore[assignment]
        self._buffer = b""
        self._connectionStatus = API_CONNECTING
        self._ready.clear()
        self._pending_init = set(INIT_COMMANDS)

        # Drain anything queued while disconnected; it refers to a dead socket.
        while not self._write_queue.empty():
            self._write_queue.get_nowait()

        self._start_task("writer", self._writer())
        self._start_task("init", self.query_initial_state())

    def connection_lost(self, exc):
        """Asyncio callback for a lost TCP connection."""
        if exc:
            _LOGGER.warning("Connection to IntesisBox %s lost: %r", self._ip, exc)
        else:
            _LOGGER.info("IntesisBox %s closed the connection", self._ip)

        self._connectionStatus = API_DISCONNECTED
        self._transport = None
        self._ready.clear()
        self._cancel_all_tasks()
        self._fail_change_waiters(ConnectionResetError("Connection lost"))
        self._send_update_callback()

        if not self._stopped:
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Start the reconnect loop if it is not already running."""
        task = self._tasks.get("reconnect")
        if task and not task.done():
            return
        self._tasks["reconnect"] = ensure_background_task(
            self._reconnect_loop(), self._eventLoop
        )

    async def _reconnect_loop(self) -> None:
        """Reconnect with exponential backoff until the device answers."""
        delay = RECONNECT_MIN_DELAY
        while not self._stopped and not self.is_connected:
            await asyncio.sleep(delay)
            if self._stopped:
                return
            try:
                await self._open_connection()
            except OSError as exc:
                _LOGGER.debug("Reconnect to %s failed: %r", self._ip, exc)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
                continue
            # Wait for the handshake to complete before declaring victory; a
            # socket that opens but never answers ID is not a usable device.
            try:
                async with asyncio.timeout(CONFIRM_TIMEOUT):
                    await self._ready.wait()
            except TimeoutError:
                _LOGGER.debug("IntesisBox %s connected but did not answer", self._ip)
                self._close_transport()
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
                continue
            _LOGGER.info("Reconnected to IntesisBox %s", self._ip)
            return

    async def _open_connection(self) -> None:
        """Open the TCP connection. Raises OSError on failure."""
        if not self._ip or not self._port:
            raise OSError("Missing IP address or port")
        self._connectionStatus = API_CONNECTING
        _LOGGER.debug("Opening connection to IntesisBox %s:%s", self._ip, self._port)
        try:
            await self._eventLoop.create_connection(lambda: self, self._ip, self._port)
        except OSError:
            # Reset the status, otherwise every later attempt sees CONNECTING
            # and refuses to try again.
            self._connectionStatus = API_DISCONNECTED
            raise

    async def async_connect(self, timeout: float = 30) -> bool:
        """Connect and wait until the device has reported ID and all limits.

        Returns True once ready, False if the device could not be reached or
        did not finish the handshake within the timeout.
        """
        self._stopped = False
        if self.is_connected:
            return True
        try:
            await self._open_connection()
        except OSError as exc:
            _LOGGER.debug("Connection to %s failed: %r", self._ip, exc)
            self._schedule_reconnect()
            return False
        try:
            async with asyncio.timeout(timeout):
                await self._ready.wait()
        except TimeoutError:
            _LOGGER.debug("IntesisBox %s did not complete handshake", self._ip)
            self._close_transport()
            self._schedule_reconnect()
            return False
        return True

    def connect(self):
        """Connect to the device, scheduling the work on the event loop."""
        self._stopped = False
        if self.is_connected:
            return
        ensure_background_task(self.async_connect(), self._eventLoop)

    def _close_transport(self) -> None:
        """Close the transport if there is one."""
        if self._transport is not None and not self._transport.is_closing():
            self._transport.close()

    def stop(self):
        """Shut down connectivity with the device and cancel all tasks."""
        self._stopped = True
        self._connectionStatus = API_DISCONNECTED
        self._ready.clear()
        self._cancel_all_tasks()
        self._fail_change_waiters(ConnectionResetError("Stopped"))
        self._close_transport()
        self._transport = None

    # ------------------------------------------------------------------
    # Outbound commands
    # ------------------------------------------------------------------

    async def _writer(self) -> None:
        """Drain the write queue, one command at a time, on the event loop."""
        while True:
            cmd = await self._write_queue.get()
            transport = self._transport
            if transport is None or transport.is_closing():
                _LOGGER.debug("Dropping %r, transport is gone", cmd)
                continue
            try:
                transport.write(f"{cmd}\r".encode("ascii"))
            except Exception as exc:  # noqa: BLE001 - surface, do not crash writer
                _LOGGER.error("Failed to send %r: %r", cmd, exc)
                continue
            _LOGGER.debug("Data sent: %r", cmd)
            await asyncio.sleep(COMMAND_INTERVAL)

    async def _send(self, cmd: str) -> None:
        """Queue a command for transmission."""
        await self._write_queue.put(cmd)

    def send_threadsafe(self, cmd: str) -> None:
        """Queue a command from outside the event loop thread."""
        self._eventLoop.call_soon_threadsafe(self._write_queue.put_nowait, cmd)

    async def query_initial_state(self):
        """Fetch identification and limits from the device upon connection."""
        for cmd in INIT_COMMANDS:
            await self._send(cmd)

    async def keep_alive(self):
        """Send PING periodically to reset the device's watchdog timer."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            _LOGGER.debug("Sending keepalive")
            await self._send("PING")

    async def poll_status(self):
        """Periodically request a full status refresh."""
        while True:
            await self._send("GET,1:*")
            await asyncio.sleep(STATUS_POLL_INTERVAL)

    async def poll_ambtemp(self):
        """Periodically refresh the ambient temperature."""
        while True:
            await asyncio.sleep(AMBTEMP_POLL_INTERVAL)
            await self._send(f"GET,1:{FUNCTION_AMBTEMP}")

    # ------------------------------------------------------------------
    # Inbound data
    # ------------------------------------------------------------------

    def data_received(self, data: bytes):
        """Asyncio callback when data is received on the socket.

        TCP is a byte stream: a chunk may end mid-line, so the tail is buffered
        until the rest of the line arrives.
        """
        self._buffer += data
        # Per the spec (§2) lines end with \r, \n or \r\n.
        self._buffer = self._buffer.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        *lines, self._buffer = self._buffer.split(b"\n")

        statusChanged = False
        for raw in lines:
            if not raw.strip():
                continue
            try:
                line = raw.decode("ascii").strip()
            except UnicodeDecodeError:
                _LOGGER.warning("Discarding non-ASCII data: %r", raw)
                continue
            try:
                statusChanged |= self._process_line(line)
            except Exception:  # noqa: BLE001 - one bad line must not kill the socket
                _LOGGER.exception("Failed to process line: %r", line)

        if statusChanged:
            self._send_update_callback()

    def _process_line(self, line: str) -> bool:
        """Handle one complete line. Returns True if state changed."""
        _LOGGER.debug("Data received: %r", line)

        if line == "ACK":
            return False
        if line == "ERR":
            # The spec (§FAQs) returns a bare ERR for an invalid value or a
            # write to a read-only function, with no indication of which
            # command failed.
            _LOGGER.warning("IntesisBox %s rejected a command", self._ip)
            self._send_error_callback("Device rejected the command")
            return False

        cmdList = line.split(":", 1)
        if len(cmdList) < 2:
            return False
        cmd, args = cmdList[0], cmdList[1]

        if cmd == "ID":
            self._parse_id_received(args)
            self._mark_init_complete("ID")
            return True
        if cmd == "CHN,1":
            self._parse_change_received(args)
            return True
        if cmd == "LIMITS":
            function = self._parse_limits_received(args)
            if function:
                self._mark_init_complete(f"LIMITS:{function}")
            return True
        return False

    def _mark_init_complete(self, cmd: str) -> None:
        """Record an answered init command and signal readiness once complete."""
        if not self._pending_init:
            # Not mid-handshake; this is an unsolicited or late reply.
            return
        self._pending_init.discard(cmd)
        if self._pending_init or self._ready.is_set():
            return

        self._connectionStatus = API_AUTHENTICATED
        self._ready.set()
        _LOGGER.debug("IntesisBox %s ready", self._ip)

        # Start the periodic tasks only once the handshake is done, and only
        # ever one of each.
        self._start_task("keepalive", self.keep_alive())
        self._start_task("poll_status", self.poll_status())
        self._start_task("poll_ambtemp", self.poll_ambtemp())

    def _parse_id_received(self, args):
        """Parse the ID reply.

        Gen 1: Model,MAC,IP,Protocol,Version,RSSI[,Name,Security,Generation]
        V6:    Model,MAC,IP,Version,RSSI,GwName,SecurityLevel,Generation
        The Protocol field is absent on V6, shifting every later field left.
        """
        info = [field.strip() for field in args.split(",")]
        if len(info) < 5:
            _LOGGER.warning("Unexpected ID reply: %r", args)
            return

        # V6 omits Protocol; gen 1 always reports it as a non-numeric token
        # ("ASCII") in position 3.
        is_v6 = len(info) >= 8 and info[3].upper() != "ASCII"
        offset = 3 if is_v6 else 4

        self._model = info[0]
        self._mac = info[1]
        self._firmversion = info[offset]
        self._rssi = info[offset + 1] if len(info) > offset + 1 else None
        self._controllerType = "V6" if is_v6 else "V1"

        _LOGGER.debug(
            "Updated info: model=%s mac=%s version=%s rssi=%s type=%s",
            self._model,
            self._mac,
            self._firmversion,
            self._rssi,
            self._controllerType,
        )

    def _parse_change_received(self, args):
        """Parse a CHN status change message."""
        parts = args.split(",", 1)
        if len(parts) != 2:
            _LOGGER.warning("Malformed change message: %r", args)
            return
        function, value = parts[0].strip(), parts[1].strip()
        if value in NULL_VALUES:
            value = None
        self._device[function] = value

        _LOGGER.debug("Updated state: %r", self._device)
        self._resolve_change_waiters(function, value)

    def _parse_limits_received(self, args) -> str | None:
        """Parse a LIMITS reply. Returns the function name, or None."""
        split_args = args.split(",", 1)
        if len(split_args) != 2:
            _LOGGER.warning("Malformed limits message: %r", args)
            return None

        function = split_args[0].strip()
        values = [v.strip() for v in split_args[1].strip().strip("[]").split(",")]

        if function == FUNCTION_SETPOINT and len(values) == 2:
            try:
                self._setpoint_minimum = int(values[0]) / 10
                self._setpoint_maximum = int(values[1]) / 10
            except ValueError:
                _LOGGER.warning("Non-numeric setpoint limits: %r", values)
                return None
        elif function == FUNCTION_FANSP:
            self._fan_speed_list = values
        elif function == FUNCTION_MODE:
            self._operation_list = values
        elif function == FUNCTION_VANEUD:
            self._vertical_vane_list = values
        elif function == FUNCTION_VANELR:
            self._horizontal_vane_list = values
        else:
            return None

        _LOGGER.debug(
            "Updated limits: setpoint=%s-%s fan=%s mode=%s vaneud=%s vanelr=%s",
            self._setpoint_minimum,
            self._setpoint_maximum,
            self._fan_speed_list,
            self._operation_list,
            self._vertical_vane_list,
            self._horizontal_vane_list,
        )
        return function

    # ------------------------------------------------------------------
    # Change confirmation
    # ------------------------------------------------------------------

    def _resolve_change_waiters(self, function: str, value: str | None) -> None:
        """Wake anyone waiting for this function to reach this value."""
        for waiter in list(self._change_waiters):
            wanted_function, wanted_value, future = waiter
            if wanted_function == function and wanted_value == value:
                self._change_waiters.remove(waiter)
                if not future.done():
                    future.set_result(True)

    def _fail_change_waiters(self, exc: Exception) -> None:
        """Fail every outstanding waiter, e.g. because the socket dropped."""
        for _, _, future in self._change_waiters:
            if not future.done():
                future.set_exception(exc)
        self._change_waiters.clear()

    async def _wait_for_value(
        self, function: str, value: str, timeout: float = CONFIRM_TIMEOUT
    ) -> bool:
        """Wait for a function to report a value, using the device's own push.

        Returns True on confirmation, False on timeout. This replaces polling
        the device repeatedly; the WMP spec (§CHN) guarantees a spontaneous
        change message whenever a value actually changes.
        """
        if self._device.get(function) == value:
            return True
        future: asyncio.Future = self._eventLoop.create_future()
        waiter = (function, value, future)
        self._change_waiters.append(waiter)
        try:
            async with asyncio.timeout(timeout):
                await future
        except TimeoutError:
            if waiter in self._change_waiters:
                self._change_waiters.remove(waiter)
            return False
        except ConnectionResetError:
            return False
        return True

    # ------------------------------------------------------------------
    # Public control surface
    # ------------------------------------------------------------------

    async def async_set_temperature(self, setpoint: float) -> None:
        """Set the target temperature."""
        await self._set_value(FUNCTION_SETPOINT, int(setpoint * 10))

    async def async_set_fan_speed(self, fan_speed: str) -> None:
        """Set the fan speed."""
        await self._set_value(FUNCTION_FANSP, fan_speed)

    async def async_set_vertical_vane(self, vane: str) -> None:
        """Set the vertical vane."""
        await self._set_value(FUNCTION_VANEUD, vane)

    async def async_set_horizontal_vane(self, vane: str) -> None:
        """Set the horizontal vane."""
        await self._set_value(FUNCTION_VANELR, vane)

    async def async_set_power_off(self) -> None:
        """Turn the device off."""
        await self._set_value(FUNCTION_ONOFF, POWER_OFF)

    async def async_set_power_on(self) -> None:
        """Turn the device on."""
        await self._set_value(FUNCTION_ONOFF, POWER_ON)

    async def async_set_mode(self, mode: str) -> None:
        """Set the mode, confirming the change before turning the unit on.

        Some units apply ONOFF and MODE out of order, so when the unit is off
        we wait for the device to confirm the new mode before powering on.
        """
        if mode not in MODES:
            _LOGGER.warning("Ignoring unsupported mode %r", mode)
            return

        _LOGGER.debug("Setting MODE to %s", mode)
        await self._set_value(FUNCTION_MODE, mode)

        if self.is_on:
            return

        if await self._wait_for_value(FUNCTION_MODE, mode):
            _LOGGER.debug("MODE confirmed as %s, powering on", mode)
            await self.async_set_power_on()
        else:
            _LOGGER.error(
                "IntesisBox %s did not confirm mode %s, not powering on",
                self._ip,
                mode,
            )

    async def _set_value(self, uid: str, value: str | int) -> None:
        """Change a setting on the thermostat."""
        await self._send(f"SET,1:{uid},{value}")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def operation_list(self) -> list[str]:
        """Supported modes."""
        return self._operation_list

    @property
    def vane_horizontal_list(self) -> list[str]:
        """Supported Horizontal Vane settings."""
        return self._horizontal_vane_list

    @property
    def vane_vertical_list(self) -> list[str]:
        """Supported Vertical Vane settings."""
        return self._vertical_vane_list

    @property
    def mode(self) -> str | None:
        """Current mode."""
        return self._device.get(FUNCTION_MODE)

    @property
    def fan_speed(self) -> str | None:
        """Current fan speed."""
        return self._device.get(FUNCTION_FANSP)

    @property
    def fan_speed_list(self) -> list[str]:
        """Supported fan speeds."""
        return self._fan_speed_list

    @property
    def device_mac_address(self) -> str | None:
        """MAC address of the IntesisBox."""
        return self._mac

    @property
    def device_model(self) -> str | None:
        """Model of the IntesisBox."""
        return self._model

    @property
    def firmware_version(self) -> str | None:
        """Firmware version of the IntesisBox."""
        return self._firmversion

    @property
    def is_on(self) -> bool:
        """Return true if the controlled device is turned on."""
        return self._device.get(FUNCTION_ONOFF) == POWER_ON

    @property
    def has_swing_control(self) -> bool:
        """Return true if the device supports swing modes."""
        return len(self._horizontal_vane_list) > 1 or len(self._vertical_vane_list) > 1

    @property
    def setpoint(self) -> float | None:
        """Public method returns the target temperature."""
        setpoint = self._device.get(FUNCTION_SETPOINT)
        return (int(setpoint) / 10) if setpoint else None

    @property
    def ambient_temperature(self) -> float | None:
        """Public method returns the current temperature."""
        temperature = self._device.get(FUNCTION_AMBTEMP)
        return (int(temperature) / 10) if temperature else None

    @property
    def max_setpoint(self) -> float | None:
        """Maximum allowed target temperature."""
        return self._setpoint_maximum

    @property
    def min_setpoint(self) -> float | None:
        """Minimum allowed target temperature."""
        return self._setpoint_minimum

    @property
    def rssi(self) -> str | None:
        """Wireless signal strength of the IntesisBox."""
        return self._rssi

    @property
    def vertical_swing(self) -> str | None:
        """Current vertical vane setting."""
        return self._device.get(FUNCTION_VANEUD)

    @property
    def horizontal_swing(self) -> str | None:
        """Current horizontal vane setting."""
        return self._device.get(FUNCTION_VANELR)

    @property
    def is_connected(self) -> bool:
        """Returns true if the device is connected and has reported its state."""
        return self._connectionStatus == API_AUTHENTICATED

    @property
    def is_initialized(self) -> bool:
        """Returns true once ID and all limits have been received."""
        return self._ready.is_set()

    @property
    def error_message(self) -> str | None:
        """Returns the last error message, or None if there were no errors."""
        return self._errorMessage

    @property
    def is_disconnected(self) -> bool:
        """Returns true when the TCP connection is disconnected and idle."""
        return self._connectionStatus == API_DISCONNECTED

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _send_update_callback(self):
        """Notify all listeners that state of the thermostat has changed."""
        if not self._updateCallbacks:
            _LOGGER.debug("Update callback has not been set by client.")

        for callback in self._updateCallbacks:
            callback()

    def _send_error_callback(self, message: str):
        """Notify all listeners that an error has occurred."""
        self._errorMessage = message

        if not self._errorCallbacks:
            _LOGGER.debug("Error callback has not been set by client.")

        for callback in self._errorCallbacks:
            callback(message)

    def add_update_callback(self, method):
        """Public method to add a callback subscriber."""
        self._updateCallbacks.append(method)

    def add_error_callback(self, method):
        """Public method to add a callback subscriber."""
        self._errorCallbacks.append(method)
