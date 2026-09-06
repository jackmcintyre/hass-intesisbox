"""Regression tests for the IntesisBox transport layer.

These cover the failure modes that made the integration flaky: torn TCP frames,
duplicated poller tasks across reconnects, a wedged reconnect after a failed
first connection, and setup racing the LIMITS replies.
"""

from __future__ import annotations

import asyncio
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
def port(server):
    """Port the emulator is listening on."""
    return server.sockets[0].getsockname()[1]


async def _connected_box(port: int) -> Any:
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    assert await box.async_connect(timeout=15)
    return box


async def test_torn_frames_are_reassembled(port):
    """A response split byte-by-byte must still parse.

    Previously data_received() called splitlines() on each raw chunk, so a
    frame split mid-line raised IndexError inside the protocol callback.
    """
    Emulator.tear_frames = True
    box = await _connected_box(port)
    try:
        assert box.device_model == "IS-IR-WMP-1"
        assert box.device_mac_address == "001DC9A2C911"
        assert box.firmware_version == "v1.0.2"
        assert box.rssi == "-44"
        assert box.fan_speed_list == ["AUTO", "1", "2", "3", "4"]
        assert box.operation_list == ["AUTO", "HEAT", "DRY", "COOL", "FAN"]
        assert (box.min_setpoint, box.max_setpoint) == (16.0, 30.0)

        await box._send("GET,1:*")
        await asyncio.sleep(2)
        assert box.ambient_temperature == 18.0
    finally:
        box.stop()


def test_torn_frame_unit():
    """The buffer must hold a partial line until the remainder arrives."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box.data_received(b"CHN,1:AMBTE")
    assert box.ambient_temperature is None
    box.data_received(b"MP,220\r\n")
    assert box.ambient_temperature == 22.0


def test_multiple_lines_in_one_chunk():
    """Several frames arriving together must all be processed."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box.data_received(b"CHN,1:MODE,HEAT\r\nCHN,1:ONOFF,ON\r\nCHN,1:SETPTEMP,215\r\n")
    assert box.mode == "HEAT"
    assert box.is_on
    assert box.setpoint == 21.5


def test_malformed_line_does_not_raise():
    """A garbage line must be logged and skipped, not kill the connection."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box.data_received(b"CHN,1:NOCOMMA\r\nLIMITS:\r\nCHN,1:MODE,COOL\r\n")
    assert box.mode == "COOL"


def test_v6_id_banner_field_offsets():
    """V6 devices omit the Protocol field, shifting version and RSSI left."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box.data_received(f"{ID_V6}\r\n".encode())
    assert box.firmware_version == "v1.0.1"
    assert box.rssi == "-44"


async def test_ready_waits_for_limits(port):
    """async_connect must not return until every LIMITS reply has arrived."""
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    try:
        assert await box.async_connect(timeout=15)
        # The climate entity builds its mode and fan lists from these; if
        # setup returns early they are empty and the platform raises
        # PlatformNotReady on every Home Assistant restart.
        assert box.is_initialized
        assert box.fan_speed_list
        assert box.operation_list
        assert box.min_setpoint is not None
    finally:
        box.stop()


async def test_ready_when_device_ignores_a_limits_query(port):
    """A unit that never answers LIMITS:VANELR must still come up.

    Real units silently ignore queries for capabilities they do not have. A
    readiness gate that waits for all five replies would leave every entity
    unavailable rather than degrading to the capabilities it does know about.
    """
    Emulator.unanswered_limits = {"VANELR"}
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    try:
        assert await box.async_connect(timeout=20)
        assert box.is_initialized
        # Everything the device did answer is still populated.
        assert box.operation_list == ["AUTO", "HEAT", "DRY", "COOL", "FAN"]
        assert box.fan_speed_list == ["AUTO", "1", "2", "3", "4"]
        assert box.vane_vertical_list == ["AUTO", "1", "2", "3", "SWING"]
        # LIMITS:VANELR went unanswered, but the device reports VANELR in its
        # status dump, so the axis is inferred and offered with defaults.
        assert box.vane_horizontal_list == intesisbox.DEFAULT_VANE_POSITIONS
        assert box.has_horizontal_vane
    finally:
        box.stop()


async def test_ready_on_id_alone_when_no_limits_answered(port):
    """A device that answers only ID must still come up, with empty limits."""
    Emulator.unanswered_limits = {"SETPTEMP", "FANSP", "MODE", "VANEUD", "VANELR"}
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    try:
        # ID is still answered, so this comes up on the grace path.
        assert await box.async_connect(timeout=20)
        assert box.device_mac_address == "001DC9A2C911"
    finally:
        box.stop()


async def test_vertical_only_unit_infers_one_axis(port):
    """The shape real hardware presents: VANEUD reported, VANELR absent.

    Observed on five live units - they ignore both LIMITS vane queries and
    never mention VANELR at all, so capability has to come from the status
    dump rather than from LIMITS.
    """
    Emulator.unanswered_limits = {"VANEUD", "VANELR"}
    Emulator.absent_functions = {"VANELR"}
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    try:
        assert await box.async_connect(timeout=20)
        assert box.has_vertical_vane
        assert not box.has_horizontal_vane
        assert box.vane_vertical_list == intesisbox.DEFAULT_VANE_POSITIONS
        assert box.vane_horizontal_list == []
    finally:
        box.stop()


async def test_reported_position_outside_defaults_is_offered(port):
    """A position the device reports must be selectable even if unusual."""
    Emulator.unanswered_limits = {"VANEUD", "VANELR"}
    Emulator.absent_functions = {"VANELR"}
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    try:
        assert await box.async_connect(timeout=20)
        box.data_received(b"CHN,1:VANEUD,8\r\n")
        assert "8" in box.vane_vertical_list
        # SWING stays last so the list reads sensibly.
        assert box.vane_vertical_list[-1] == "SWING"
    finally:
        box.stop()


async def test_refused_vane_write_drops_the_capability(port):
    """A unit that reports a vane but refuses to set it must stop offering it.

    Observed on five TO-RC-WMP-1 gateways: they report CHN,1:VANEUD faithfully,
    move the vane themselves, and answer ERR to every SET,1:VANEUD - whether
    the unit is off or running, and for AUTO as well as numbered positions.
    """
    Emulator.unanswered_limits = {"VANEUD", "VANELR"}
    Emulator.absent_functions = {"VANELR"}
    Emulator.readonly_functions = {"VANEUD"}
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    try:
        assert await box.async_connect(timeout=20)
        # Before anyone tries, the axis looks settable.
        assert box.has_vertical_vane

        await box.async_set_vertical_vane("3")
        await asyncio.sleep(1.5)

        # The refusal is learned, not assumed.
        assert not box.has_vertical_vane
        # Position is still read, because reading works where writing does not.
        assert box.vertical_swing == "AUTO"
    finally:
        box.stop()


async def test_unrelated_error_does_not_drop_a_capability(port):
    """An ERR for something else must not be blamed on the vane."""
    box = await _connected_box(port)
    try:
        assert box.has_vertical_vane
        Emulator.reject_next_set = True
        await box.async_set_fan_speed("9")
        await asyncio.sleep(1.5)
        assert box.has_vertical_vane
    finally:
        box.stop()


async def test_set_mode_confirms_before_power_on(port):
    """Mode is confirmed from the device's own push, not by polling 30 times."""
    box = await _connected_box(port)
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
        box.stop()


async def test_err_response_surfaces_to_callback(port):
    """A rejected command must not fail silently."""
    box = await _connected_box(port)
    errors: list[str] = []
    box.add_error_callback(errors.append)
    try:
        Emulator.reject_next_set = True
        await box.async_set_temperature(21.0)
        await asyncio.sleep(1.5)
        assert errors
    finally:
        box.stop()


async def test_reconnect_does_not_duplicate_pollers(port):
    """Reconnecting must replace the periodic tasks, not add to them."""
    box = await _connected_box(port)
    try:
        Emulator.drop_all()
        await asyncio.sleep(0.5)
        assert not box.is_connected

        for _ in range(60):
            await asyncio.sleep(0.5)
            if box.is_connected:
                break
        assert box.is_connected
        assert box.is_initialized

        # Readiness is set from inside the handshake; the reconnect task that
        # was waiting on it retires on a later event-loop tick. Give the
        # transient tasks a moment to finish before counting what is left,
        # otherwise this races and fails only under a loaded suite.
        for _ in range(20):
            transient = {"reconnect", "init", "limits_grace"}
            if not any(
                name in transient and not task.done()
                for name, task in box._tasks.items()
            ):
                break
            await asyncio.sleep(0.1)

        live = [name for name, task in box._tasks.items() if not task.done()]
        assert sorted(live) == ["keepalive", "poll_ambtemp", "poll_status", "writer"]
    finally:
        box.stop()


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
    box._stopped = False
    assert await box.async_connect(timeout=15)
    box.stop()


async def test_stop_cancels_all_tasks(port):
    """stop() must leave nothing running."""
    box = await _connected_box(port)
    box.stop()
    await asyncio.sleep(0.5)
    assert all(task.done() for task in box._tasks.values())
    assert not box.is_connected


# --------------------------------------------------------------------------
# #14 - the diagnostic channel
# --------------------------------------------------------------------------


def test_pong_carries_live_rssi():
    """PONG:<rssi> is not in the spec but real gateways send it; keep it."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    pushes = []
    box.add_update_callback(lambda: pushes.append(True))
    box.data_received(b"PONG:-54\r\n")
    assert box.rssi == "-54"
    assert pushes, "a fresh RSSI must reach the entities"
    # A bare PONG is a keepalive answer and nothing more.
    box.data_received(b"PONG\r\n")
    assert box.rssi == "-54"


def test_fault_status_and_code_are_readable():
    """ERRSTATUS and ERRCODE were stored and read by nothing."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    assert box.error_status is None
    box.data_received(b"CHN,1:ERRSTATUS,ERR\r\nCHN,1:ERRCODE,E7\r\n")
    assert box.error_status == "ERR"
    assert box.error_code == "E7"


def test_removed_update_callback_is_not_called():
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    calls = []
    cb = lambda: calls.append(True)  # noqa: E731
    box.add_update_callback(cb)
    box.remove_update_callback(cb)
    box.remove_update_callback(cb)  # tolerated
    box.data_received(b"CHN,1:MODE,HEAT\r\n")
    assert not calls


# --------------------------------------------------------------------------
# Failure paths, driven deterministically
# --------------------------------------------------------------------------


class FakeTransport:
    """Stands in for asyncio's transport so writer paths can be forced."""

    def __init__(self, fail: bool = False) -> None:
        self.written: list[bytes] = []
        self.fail = fail
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.fail:
            raise OSError("socket went away")
        self.written.append(data)

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


async def _settle(box) -> None:
    box.stop()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_garbage_and_edge_lines_are_tolerated(caplog):
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    with caplog.at_level(logging.WARNING):
        box.data_received(
            b"\r\n\r\n"  # blank lines
            b"CHN,1:MODE,H\xc3\xa9AT\r\n"  # non-ASCII
            b"HELLO\r\n"  # no colon
            b"PONG\r\n"  # bare keepalive answer
            b"ID:too,short\r\n"
            b"LIMITS:SETPTEMP,[a,b]\r\n"
            b"LIMITS:WIBBLE,[1,2]\r\n"
            b"CHN,1:AMBTEMP,32768\r\n"  # the null value
            b"ERR\r\n"  # nothing outstanding to blame
        )
    assert "non-ASCII" in caplog.text
    assert "Unexpected ID reply" in caplog.text
    assert "Non-numeric setpoint limits" in caplog.text
    assert "rejected a command" in caplog.text
    assert box.ambient_temperature is None
    assert box.device_mac_address is None


def test_a_parser_exception_does_not_kill_the_socket(caplog):
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box._parse_change_received = lambda args: 1 / 0  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        box.data_received(b"CHN,1:MODE,HEAT\r\nCHN,1:ONOFF,ON\r\n")
    assert "Failed to process line" in caplog.text


def test_properties_read_the_device_dict():
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box.data_received(b"CHN,1:FANSP,2\r\nCHN,1:VANELR,3\r\nCHN,1:VANEUD,1\r\n")
    assert box.fan_speed == "2"
    assert box.horizontal_swing == "3"
    assert box.has_swing_control
    assert box.error_message is None
    box._send_error_callback("boom")
    assert box.error_message == "boom"


async def test_background_task_failures_are_logged(caplog):
    async def boom():
        raise RuntimeError("task blew up")

    with caplog.at_level(logging.ERROR):
        task = intesisbox.ensure_background_task(boom(), asyncio.get_running_loop())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert task.done()
    assert "Background task failed" in caplog.text


async def test_send_threadsafe_queues_from_another_thread():
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)
    await loop.run_in_executor(None, box.send_threadsafe, "PING")
    await asyncio.sleep(0.05)
    assert box._write_queue.get_nowait() == "PING"


async def test_connection_made_drains_stale_commands_and_writes_the_handshake():
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)
    box._write_queue.put_nowait("STALE")
    transport = FakeTransport()
    box.connection_made(transport)
    await asyncio.sleep(0.05)
    assert b"STALE\r" not in transport.written
    assert transport.written[0] == b"ID\r"
    await _settle(box)


async def test_writer_drops_commands_when_the_transport_is_gone(monkeypatch, caplog):
    monkeypatch.setattr(intesisbox, "COMMAND_INTERVAL", 0.01)
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)
    box.connection_made(FakeTransport())
    await asyncio.sleep(0.05)
    box._transport = None
    with caplog.at_level(logging.DEBUG, logger="intesisbox"):
        await box._send("GET,1:MODE")
        await asyncio.sleep(0.3)
    assert "Dropping" in caplog.text
    await _settle(box)


async def test_writer_survives_a_failing_transport(caplog):
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)
    with caplog.at_level(logging.ERROR):
        box.connection_made(FakeTransport(fail=True))
        await asyncio.sleep(0.1)
    assert "Failed to send" in caplog.text
    live = box._tasks["writer"]
    assert not live.done(), "one bad write must not kill the writer"
    await _settle(box)


async def test_unanswered_set_releases_the_writer(monkeypatch, caplog):
    monkeypatch.setattr(intesisbox, "SET_REPLY_TIMEOUT", 0.1)
    monkeypatch.setattr(intesisbox, "COMMAND_INTERVAL", 0.01)
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)
    transport = FakeTransport()
    box.connection_made(transport)
    with caplog.at_level(logging.DEBUG, logger="intesisbox"):
        await box.async_set_power_off()
        await box.async_set_horizontal_vane("1")
        await asyncio.sleep(0.8)
    assert b"SET,1:ONOFF,OFF\r" in transport.written
    assert b"SET,1:VANELR,1\r" in transport.written, "queue must move on"
    assert "No reply" in caplog.text
    await _settle(box)


async def test_periodic_tasks_send_keepalive_and_ambient_poll(monkeypatch):
    monkeypatch.setattr(intesisbox, "COMMAND_INTERVAL", 0.01)
    monkeypatch.setattr(intesisbox, "KEEPALIVE_INTERVAL", 0.05)
    monkeypatch.setattr(intesisbox, "AMBTEMP_POLL_INTERVAL", 0.05)
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)
    transport = FakeTransport()
    box.connection_made(transport)
    box._become_ready()
    await asyncio.sleep(0.4)
    assert b"PING\r" in transport.written
    assert b"GET,1:AMBTEMP\r" in transport.written
    assert b"GET,1:*\r" in transport.written
    await _settle(box)


async def test_ready_can_be_forced_from_outside_the_grace_task():
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)
    box._pending_init = {"ID", "LIMITS:FANSP"}
    box.data_received(b"ID:IS-IR-WMP-1,001DC9A2C911,1.2.3.4,ASCII,v1,-40\r\n")
    assert "limits_grace" in box._tasks  # only the optional reply is outstanding
    box._become_ready()  # not from inside the grace task: it must be cancelled
    await asyncio.sleep(0)
    assert box.is_connected
    assert "limits_grace" not in box._tasks
    # A late reply after readiness is ignored, even with a stale pending entry.
    box._pending_init = {"LIMITS:VANEUD"}
    box.data_received(b"LIMITS:FANSP,[AUTO,1]\r\n")
    assert box.is_initialized
    assert "limits_grace" not in box._tasks
    await _settle(box)


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


async def test_connection_lost_after_stop_does_not_reconnect():
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)
    box.stop()
    box.connection_lost(OSError("reset"))
    assert "reconnect" not in box._tasks


async def test_missing_address_is_a_connection_failure():
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("", 0, loop=loop)
    assert await box.async_connect(timeout=1) is False
    await _settle(box)


async def test_reconnect_backs_off_while_the_device_is_unreachable():
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)  # nothing listens on 1
    box._reconnect_delay = 0.05
    box._schedule_reconnect()
    await asyncio.sleep(0.5)
    assert box._reconnect_delay > 0.05
    assert not box.is_connected
    await _settle(box)


async def test_silent_device_times_out_the_handshake(monkeypatch, port):
    """A box that accepts TCP and never answers must not count as connected."""
    monkeypatch.setattr(intesisbox, "CONFIRM_TIMEOUT", 0.3)
    monkeypatch.setattr(intesisbox, "LIMITS_GRACE", 0.1)
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
        await _settle(box)


async def test_connect_and_async_connect_are_idempotent_once_connected(port):
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    try:
        box.connect()
        for _ in range(60):
            await asyncio.sleep(0.1)
            if box.is_connected:
                break
        assert box.is_connected
        assert await box.async_connect(timeout=1) is True
        box.connect()  # no second connection
        assert len(Emulator.connections) == 1
    finally:
        await _settle(box)


async def test_horizontal_vane_and_power_off_are_queued():
    loop = asyncio.get_running_loop()
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=loop)
    await box.async_set_horizontal_vane("SWING")
    await box.async_set_power_off()
    assert box._write_queue.get_nowait() == "SET,1:VANELR,SWING"
    assert box._write_queue.get_nowait() == "SET,1:ONOFF,OFF"


def test_vane_list_prefers_reported_limits_and_tolerates_unknowns():
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    assert box.vane_horizontal_list == []  # never reported, no limits
    box.data_received(b"LIMITS:VANELR,[AUTO,1,SWING]\r\n")
    assert box.vane_horizontal_list == ["AUTO", "1", "SWING"]
    assert box.has_horizontal_vane
