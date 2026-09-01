"""Regression tests for the IntesisBox transport layer.

These cover the failure modes that made the integration flaky: torn TCP frames,
duplicated poller tasks across reconnects, a wedged reconnect after a failed
first connection, and setup racing the LIMITS replies.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from .emulator import ID_V6, Emulator, start

_SPEC = importlib.util.spec_from_file_location(
    "intesisbox",
    Path(__file__).parent.parent / "custom_components" / "intesisbox" / "intesisbox.py",
)
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


async def _connected_box(port: int) -> intesisbox.IntesisBox:
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
