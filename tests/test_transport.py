"""Tests for the IntesisBox transport layer.

The controller is run against the WMP device emulator in tests/emulator.py,
listening on an ephemeral port on 127.0.0.1, so the tests exercise the real
asyncio protocol rather than a mock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import importlib.util
import logging
from pathlib import Path
from typing import Any

import pytest

from .emulator import ID_V6, Emulator, start


@pytest.fixture(autouse=True)
def _real_sockets(socket_enabled):
    """The emulator is a real TCP server on 127.0.0.1.

    The Home Assistant test harness blocks socket construction for every test
    by default; this opts the transport tests back in.
    """


# Load the controller module from its file rather than importing the package,
# so these tests stay independent of Home Assistant's own import machinery.
_SPEC = importlib.util.spec_from_file_location(
    "intesisbox",
    Path(__file__).parent.parent / "custom_components" / "intesisbox" / "intesisbox.py",
)
assert _SPEC is not None and _SPEC.loader is not None
intesisbox = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(intesisbox)


@pytest.fixture
async def server():
    """Run an emulator on an ephemeral port."""
    srv = await start()
    yield srv
    srv.close()
    await srv.wait_closed()


@pytest.fixture
def port(server) -> int:
    """Port the emulator is listening on."""
    return server.sockets[0].getsockname()[1]


async def _wait_until(condition: Callable[[], bool], timeout: float = 15) -> None:
    """Poll a condition until it holds or the timeout expires."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.05)


async def _reap_tasks() -> None:
    """Cancel the controller's background tasks and wait for them.

    The periodic tasks sleep until their next tick and only notice a stop
    then, so cancel them outright: the test harness fails any test that leaves
    a task or timer behind.
    """
    tasks = list(intesisbox.background_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _shutdown(box: Any) -> None:
    """Stop a connected controller and reap its background tasks."""
    box.stop()
    await _reap_tasks()


async def _connect_and_handshake(port: int) -> Any:
    """Connect to the emulator and wait for the handshake to finish."""
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    box.connect()
    # LIMITS:VANELR is the last query the controller sends on connect, and the
    # status dump requested on ID follows it.
    await _wait_until(
        lambda: bool(box.vane_horizontal_list) and box.ambient_temperature is not None
    )
    return box


def _assert_handshake_state(box: Any) -> None:
    """Everything the emulator reports during the handshake, as parsed."""
    assert box.is_connected
    assert box.device_model == "IS-IR-WMP-1"
    assert box.device_mac_address == "001DC9A2C911"
    assert box.firmware_version == "v1.0.2"
    assert box.rssi == "-44"

    assert (box.min_setpoint, box.max_setpoint) == (16.0, 30.0)
    assert box.fan_speed_list == ["AUTO", "1", "2", "3", "4"]
    assert box.operation_list == ["AUTO", "HEAT", "DRY", "COOL", "FAN"]
    assert box.vane_vertical_list == ["AUTO", "1", "2", "3", "SWING"]
    assert box.vane_horizontal_list == ["AUTO", "1", "2", "3", "SWING"]
    assert box.has_swing_control

    # The status dump requested once the ID reply arrived.
    assert box.mode == "AUTO"
    assert box.setpoint == 21.0
    assert box.ambient_temperature == 18.0
    assert not box.is_on


async def test_handshake_reads_identity_limits_and_state(port):
    """Connecting must populate identity, limits and the first status dump."""
    box = await _connect_and_handshake(port)
    try:
        _assert_handshake_state(box)
    finally:
        await _shutdown(box)


def test_status_lines_update_state():
    """Several complete CHN lines in one chunk must all be applied."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box.data_received(b"CHN,1:MODE,HEAT\r\nCHN,1:ONOFF,ON\r\nCHN,1:SETPTEMP,215\r\n")
    assert box.mode == "HEAT"
    assert box.is_on
    assert box.setpoint == 21.5


def test_status_change_notifies_subscribers():
    """A state change must reach every registered update callback."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    calls: list[bool] = []
    box.add_update_callback(lambda: calls.append(True))
    box.data_received(b"CHN,1:AMBTEMP,32768\r\n")  # the device's null value
    assert box.ambient_temperature is None
    assert calls == [True]


async def test_torn_frames_are_reassembled(port):
    """A reply split byte-by-byte must still parse.

    Real devices do this under load. data_received() used to call
    splitlines() on each raw chunk, so a frame cut mid-line raised IndexError
    inside the protocol callback and asyncio closed the connection.
    """
    Emulator.tear_frames = True
    box = await _connect_and_handshake(port)
    try:
        _assert_handshake_state(box)
    finally:
        await _shutdown(box)


def test_partial_line_is_held_until_the_rest_arrives():
    """Nothing is parsed from a fragment; the whole line is, once complete."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box.data_received(b"CHN,1:AMBTE")
    assert box.ambient_temperature is None
    box.data_received(b"MP,220\r\n")
    assert box.ambient_temperature == 22.0


def test_line_endings_are_all_accepted():
    """The spec allows \\r, \\n or \\r\\n; a \\r\\n split across chunks is one end."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box.data_received(b"CHN,1:MODE,HEAT\rCHN,1:FANSP,2\nCHN,1:ONOFF,ON\r")
    box.data_received(b"\nCHN,1:SETPTEMP,215\r\n")
    assert box.mode == "HEAT"
    assert box.fan_speed == "2"
    assert box.is_on
    assert box.setpoint == 21.5


def test_runaway_line_is_discarded(caplog):
    """Bytes that never see a line ending must not accumulate forever."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    with caplog.at_level(logging.WARNING):
        box.data_received(b"x" * (intesisbox.MAX_LINE_LENGTH + 1))
    assert box._buffer == b""
    assert "no line ending" in caplog.text
    # The stream is usable again afterwards.
    box.data_received(b"CHN,1:MODE,DRY\r\n")
    assert box.mode == "DRY"


def test_malformed_lines_are_skipped_not_fatal(caplog):
    """A garbage line must be logged and skipped, not kill the connection."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    pushes: list[bool] = []
    box.add_update_callback(lambda: pushes.append(True))
    with caplog.at_level(logging.WARNING):
        box.data_received(
            b"\r\n\r\n"  # blank lines
            b"CHN,1:NOCOMMA\r\n"  # no value
            b"LIMITS:\r\n"  # nothing after the colon
            b"LIMITS:SETPTEMP,[a,b]\r\n"  # non-numeric limits
            b"CHN,1:MODE,H\xc3\xa9AT\r\n"  # non-ASCII
            b"HELLO\r\n"  # no colon at all
            b"CHN,1:MODE,COOL\r\n"
        )
    assert box.mode == "COOL"
    assert box.min_setpoint is None
    # One real change in that chunk, so exactly one notification.
    assert pushes == [True]
    assert "Malformed change message" in caplog.text
    assert "Non-numeric setpoint limits" in caplog.text
    assert "non-ASCII" in caplog.text


def test_a_parser_exception_does_not_kill_the_socket(caplog):
    """An unexpected failure on one line must not take the connection down."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box._parse_change_received = lambda args: 1 / 0  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        box.data_received(b"CHN,1:MODE,HEAT\r\nCHN,1:ONOFF,ON\r\n")
    assert "Failed to process line" in caplog.text


async def test_v6_id_banner_field_offsets():
    """V6 gateways omit the Protocol field, shifting version and RSSI left."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    try:
        box.data_received(f"{ID_V6}\r\n".encode())
        assert box.device_model == "INWMPUNI001I000"
        assert box.device_mac_address == "001DC9A2C911"
        assert box.firmware_version == "v1.0.1"
        assert box.rssi == "-44"
        assert box.is_connected
    finally:
        # Never connected a socket, so there is nothing to stop; only the
        # pollers the ID reply started.
        await _reap_tasks()


def test_short_id_reply_does_not_count_as_connected(caplog):
    """A reply with no identity in it is logged, and the handshake goes on."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    with caplog.at_level(logging.WARNING):
        box.data_received(b"ID:too,short\r\n")
    assert "Unexpected ID reply" in caplog.text
    assert box.device_mac_address is None
    assert not box.is_connected
    assert not intesisbox.background_tasks


# --------------------------------------------------------------------------
# Keepalive, reconnect and task lifecycle
# --------------------------------------------------------------------------


class FakeTransport:
    """Stands in for asyncio's transport so the periodic tasks can be observed."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


def _live_tasks(box: Any) -> list[str]:
    return sorted(name for name, task in box._tasks.items() if not task.done())


async def test_periodic_tasks_send_keepalive_and_polls(monkeypatch):
    """PING must actually go out, alongside the status and temperature polls."""
    monkeypatch.setattr(intesisbox, "COMMAND_INTERVAL", 0.01)
    monkeypatch.setattr(intesisbox, "KEEPALIVE_INTERVAL", 0.05)
    monkeypatch.setattr(intesisbox, "AMBTEMP_POLL_INTERVAL", 0.05)
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    transport = FakeTransport()
    box.connection_made(transport)
    box.data_received(f"{ID_V6}\r\n".encode())
    await asyncio.sleep(0.3)
    try:
        assert b"PING\r" in transport.written
        assert b"GET,1:AMBTEMP\r" in transport.written
        assert b"GET,1:*\r" in transport.written
    finally:
        await _shutdown(box)


async def test_reconnect_does_not_duplicate_pollers(port):
    """Reconnecting must replace the periodic tasks, not add to them."""
    box = await _connect_and_handshake(port)
    try:
        Emulator.drop_all()
        await _wait_until(lambda: not box.is_connected, timeout=5)

        await _wait_until(lambda: box.is_connected, timeout=30)
        # Let the handshake and the retired reconnect task finish.
        await _wait_until(
            lambda: not {"reconnect", "init"} & set(_live_tasks(box)), timeout=15
        )
        assert _live_tasks(box) == [
            "keepalive",
            "poll_ambtemp",
            "poll_status",
            "writer",
        ]
    finally:
        await _shutdown(box)


async def test_failed_connection_does_not_wedge_reconnect(port):
    """A failed first attempt must reset state instead of raising later.

    Previously the status stayed at CONNECTING and the next connect() hit
    AttributeError on a None transport, permanently breaking reconnection.
    """
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    assert await box.async_connect(timeout=3) is False
    assert box.is_disconnected
    box.stop()

    box._port = port
    assert await box.async_connect(timeout=15)
    await _shutdown(box)


async def test_stop_cancels_all_tasks(port):
    """stop() must leave nothing running, and must not raise without a socket."""
    box = await _connect_and_handshake(port)
    box.stop()
    await asyncio.sleep(0.1)
    assert all(task.done() for task in box._tasks.values())
    assert not box.is_connected
    box.stop()  # a second stop, with no transport, is harmless
    await _reap_tasks()


async def test_silent_device_times_out_the_handshake(monkeypatch, port):
    """A box that accepts TCP and never answers must not count as connected."""
    monkeypatch.setattr(intesisbox, "CONNECT_TIMEOUT", 0.3)
    Emulator.silent = True
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    try:
        assert await box.async_connect(timeout=0.5) is False
        assert not box.is_connected
        # The reconnect loop now owns the retry; let it hit the same wall once.
        box._reconnect_delay = 0.05
        await asyncio.sleep(1.2)
        assert not box.is_connected
        assert box._reconnect_delay > 0.05, "a failed handshake must grow the backoff"
    finally:
        await _shutdown(box)


async def test_reconnect_backs_off_while_the_device_is_unreachable():
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    box._reconnect_delay = 0.05
    box._schedule_reconnect()
    await asyncio.sleep(0.5)
    try:
        assert box._reconnect_delay > 0.05
        assert not box.is_connected
    finally:
        await _shutdown(box)


async def test_connection_lost_after_stop_does_not_reconnect():
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    box.stop()
    box.connection_lost(OSError("reset"))
    assert "reconnect" not in box._tasks
    await _reap_tasks()


async def test_missing_address_is_a_connection_failure():
    box = intesisbox.IntesisBox("", 0, loop=asyncio.get_running_loop())
    assert await box.async_connect(timeout=1) is False
    await _shutdown(box)


async def test_connect_is_idempotent_and_safe_from_another_thread(port):
    """connect() may be called from an executor thread; a second call is a no-op."""
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=loop)
    try:
        await loop.run_in_executor(None, box.connect)
        await _wait_until(lambda: box.is_connected)
        assert await box.async_connect(timeout=1) is True
        box.connect()
        await asyncio.sleep(0.2)
        assert len(Emulator.connections) == 1
    finally:
        await _shutdown(box)


async def test_write_is_dropped_when_the_transport_is_gone(caplog):
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    with caplog.at_level(logging.DEBUG, logger="intesisbox"):
        box._write("PING")
    assert "Dropping" in caplog.text


async def test_background_task_failures_are_logged(caplog):
    async def boom():
        raise RuntimeError("task blew up")

    with caplog.at_level(logging.ERROR):
        task = intesisbox.ensure_background_task(boom(), asyncio.get_running_loop())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert task.done()
    assert "Background task failed" in caplog.text


async def test_two_connect_calls_open_one_socket(port):
    """A second connect() while the first is still opening must join it."""
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=loop)
    try:
        box.connect()
        box.connect()
        await loop.run_in_executor(None, box.connect)
        await _wait_until(lambda: box.is_connected)
        await asyncio.sleep(0.2)
        assert len(Emulator.connections) == 1
    finally:
        await _shutdown(box)


async def test_stop_cancels_a_connect_still_in_flight(monkeypatch, port):
    """stop() during a pending connect must not let a socket appear later."""
    monkeypatch.setattr(intesisbox, "CONNECT_TIMEOUT", 5)
    Emulator.silent = True
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    box.connect()
    await _wait_until(lambda: box._transport is not None)
    box.stop()
    await asyncio.sleep(0.3)
    assert all(task.done() for task in box._tasks.values())
    assert not box.is_connected
    assert Emulator.connections == []
    await _reap_tasks()


# --------------------------------------------------------------------------
# Serialised writes, ACK and ERR
# --------------------------------------------------------------------------


async def test_set_mode_confirms_before_power_on(port):
    """Mode is confirmed from the device's own push, not by polling 30 times."""
    box = await _connect_and_handshake(port)
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await box.async_set_mode("HEAT")
        await asyncio.sleep(1.5)
        device = Emulator.connections[0]
        assert device.state["MODE"] == "HEAT"
        assert device.state["ONOFF"] == "ON"
        assert loop.time() - started < 5
    finally:
        await _shutdown(box)


async def test_err_response_surfaces_to_callback(port):
    """A rejected command must not fail silently."""
    box = await _connect_and_handshake(port)
    errors: list[str] = []
    box.add_error_callback(errors.append)
    try:
        Emulator.reject_next_set = True
        await box.async_set_temperature(21.0)
        await asyncio.sleep(1.5)
        assert errors == ["Device rejected SET,1:SETPTEMP,210"]
    finally:
        await _shutdown(box)


async def test_sync_setters_work_from_an_executor_thread(port):
    """The entity calls set_* from a worker thread; the write lands on the loop.

    From a worker thread each call also waits for its command to complete,
    so a caller that issues set_mode then set_temperature, as the climate
    entity does, gets them applied in that order.
    """
    box = await _connect_and_handshake(port)
    try:
        loop = asyncio.get_running_loop()
        device = Emulator.connections[0]

        def from_thread():
            box.set_mode("HEAT")
            # Returned only once the device confirmed the mode and the power-on
            # was queued, so what follows lands after it.
            assert device.state["MODE"] == "HEAT"
            box.set_temperature(22.5)
            box.set_fan_speed("2")

        await loop.run_in_executor(None, from_thread)
        await asyncio.sleep(1.0)
        assert device.state["ONOFF"] == "ON"
        assert device.state["SETPTEMP"] == "225"
        assert device.state["FANSP"] == "2"
        sets = [c for c in device.received if c.startswith("SET,")]
        assert sets == [
            "SET,1:MODE,HEAT",
            "SET,1:ONOFF,ON",
            "SET,1:SETPTEMP,225",
            "SET,1:FANSP,2",
        ]
    finally:
        await _shutdown(box)


async def test_sync_setters_only_schedule_when_called_on_the_loop():
    """On the loop thread there is nothing to block on; the command is queued."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    box.set_power_off()
    await asyncio.sleep(0.05)
    assert box._write_queue.get_nowait() == "SET,1:ONOFF,OFF"


async def test_unanswered_set_releases_the_writer(monkeypatch, caplog):
    monkeypatch.setattr(intesisbox, "SET_REPLY_TIMEOUT", 0.1)
    monkeypatch.setattr(intesisbox, "COMMAND_INTERVAL", 0.01)
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    transport = FakeTransport()
    box.connection_made(transport)
    try:
        with caplog.at_level(logging.DEBUG, logger="intesisbox"):
            await box.async_set_power_off()
            await box.async_set_horizontal_vane("1")
            await asyncio.sleep(0.8)
        assert b"SET,1:ONOFF,OFF\r" in transport.written
        assert b"SET,1:VANELR,1\r" in transport.written, "queue must move on"
        assert "No reply" in caplog.text
    finally:
        await _shutdown(box)


async def test_a_set_waits_for_the_previous_reply(monkeypatch):
    """Two SETs back to back: the second is not written until the first is answered."""
    monkeypatch.setattr(intesisbox, "COMMAND_INTERVAL", 0.01)
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    transport = FakeTransport()
    box.connection_made(transport)
    try:
        await box.async_set_power_on()
        await box.async_set_fan_speed("3")
        await asyncio.sleep(0.2)
        assert transport.written.count(b"SET,1:ONOFF,ON\r") == 1
        assert b"SET,1:FANSP,3\r" not in transport.written
        box.data_received(b"ACK\r\n")
        await asyncio.sleep(0.2)
        assert b"SET,1:FANSP,3\r" in transport.written
    finally:
        await _shutdown(box)


async def test_change_waiters_time_out_and_fail_on_disconnect():
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)
    assert await box._wait_for_value("MODE", "HEAT", timeout=0.05) is False
    assert box._change_waiters == []

    waiting = loop.create_task(box._wait_for_value("MODE", "HEAT", timeout=5))
    await asyncio.sleep(0)
    box._fail_change_waiters(ConnectionResetError("dropped"))
    assert await waiting is False


async def test_set_mode_edge_cases(caplog):
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)
    with caplog.at_level(logging.WARNING):
        await box.async_set_mode("WIBBLE")
    assert "unsupported mode" in caplog.text
    assert box._write_queue.empty()

    box.data_received(b"CHN,1:ONOFF,ON\r\n")
    await box.async_set_mode("HEAT")  # already on: no confirm-then-power-on
    assert box._write_queue.get_nowait() == "SET,1:MODE,HEAT"
    assert box._write_queue.empty()

    box.data_received(b"CHN,1:ONOFF,OFF\r\n")

    async def never(*a, **k):
        return False

    box._wait_for_value = never  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        await box.async_set_mode("COOL")
    assert "did not confirm mode" in caplog.text


async def test_writer_survives_a_failing_transport(caplog):
    class FailingTransport(FakeTransport):
        def write(self, data: bytes) -> None:
            raise OSError("socket went away")

    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    with caplog.at_level(logging.ERROR):
        box.connection_made(FailingTransport())
        await asyncio.sleep(0.1)
    try:
        assert "Failed to send" in caplog.text
        assert not box._tasks["writer"].done(), "one bad write must not kill the writer"
        # Failures are still paced: the seven handshake commands need well
        # over a second at the default interval, so not all have been tried.
        assert caplog.text.count("Failed to send") < 7
    finally:
        await _shutdown(box)


async def test_stale_commands_are_dropped_on_reconnect():
    """Anything queued while disconnected refers to a dead socket."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    await box._send("STALE")
    transport = FakeTransport()
    box.connection_made(transport)
    await asyncio.sleep(0.05)
    try:
        assert b"STALE\r" not in transport.written
        assert transport.written[0] == b"ID\r"
    finally:
        await _shutdown(box)


def test_bare_err_with_nothing_outstanding_is_logged(caplog):
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    errors: list[str] = []
    box.add_error_callback(errors.append)
    with caplog.at_level(logging.WARNING):
        box.data_received(b"ERR\r\n")
    assert "rejected a command" in caplog.text
    assert errors == ["Device rejected a command"]
    assert box.error_message == "Device rejected a command"
