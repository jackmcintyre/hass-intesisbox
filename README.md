# IntesisBox for Home Assistant

Local control of Intesis IntesisBox and WMP air-conditioning gateways over TCP, with no cloud in the path.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![GitHub release](https://img.shields.io/github/v/release/jackmcintyre/hass-intesisbox)](https://github.com/jackmcintyre/hass-intesisbox/releases)

This is a fork of [jnimmo/hass-intesisbox](https://github.com/jnimmo/hass-intesisbox). It speaks the Intesis WMP protocol (ASCII over TCP, port 3310) directly to the gateway on your LAN, so state changes are pushed to Home Assistant as they happen rather than polled. One config entry is one gateway is one device.

## Supported hardware

Any Intesis gateway that implements the WMP protocol. The integration reads the gateway's capabilities (modes, fan speeds, set point range, vane positions) from the device at connect time rather than assuming them, so it adapts to what each unit reports.

Development and testing was done against Intesis TO-RC-WMP-1 gateways on firmware v1.3.3. Things observed on that hardware, which the integration now accounts for:

- It does not answer `LIMITS:VANEUD` or `LIMITS:VANELR`. The WMP spec says an unrecognised command is discarded silently, so these queries are treated as best effort; a default position set is offered if the device stays quiet.
- It reports the vane position but returns `ERR` to every attempt to set it. See "Read-only vanes" below.
- It answers `PING` with `PONG:<rssi>`, which is not in the spec. The integration uses it for the signal-strength sensor.

If your gateway behaves differently, a diagnostics download (below) attached to an issue is the most useful thing you can send.

## Installation

### HACS

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/jackmcintyre/hass-intesisbox` with category **Integration**.
3. Search for **IntesisBox** in HACS and install it.
4. Restart Home Assistant.

### Manual

Copy the `custom_components/intesisbox` directory into the `custom_components` directory of your Home Assistant configuration, then restart Home Assistant.

## Configuration

Setup is through the UI only. YAML platform configuration was removed in 2.4.0.

1. Go to **Settings → Devices & services → Add integration** and choose **IntesisBox**.
2. Enter the gateway's hostname or IP address.

The flow connects to the gateway and completes the WMP handshake before creating an entry, so a wrong address or a powered-off box is reported as an error in the form rather than as an entry stuck retrying. The gateway's MAC address becomes the entry's identity: adding the same box again under a different address updates the existing entry instead of creating a duplicate.

The device is named after the host you entered. Rename it in Home Assistant and its entities follow.

### Changing the address

If the gateway moves to a new IP or hostname, use **Reconfigure** from the entry's menu on the integration page. The host is changed in place and entity ids, names, areas and history are kept. The flow checks the MAC of the device at the new address and refuses to point the entry at a different gateway.

## Entities

Each gateway is one device with the following entities.

| Entity | Platform | Notes |
| --- | --- | --- |
| Climate | `climate` | HVAC modes `off`, `heat`, `cool`, `dry`, `fan_only` and `heat_cool`, as reported by the device. Fan modes `auto`, `low`, `medium`, `high`, `ultra high`. Target temperature in °C. Vertical swing and horizontal swing where the device supports them. |
| Fault | `binary_sensor` | Problem class. On while the indoor unit reports `ERRSTATUS` as anything other than `OK`. Diagnostic category. |
| Fault code | `sensor` | The raw `ERRCODE` token from the device. Codes are manufacturer-specific. Diagnostic category. |
| Signal strength | `sensor` | Gateway Wi-Fi RSSI in dBm, refreshed on every keepalive. Diagnostic category, **disabled by default**. |

The climate entity also exposes `vertical_swing` and `horizontal_swing` as state attributes whenever the device reports a position, including on hardware where the vane cannot be commanded.

All entities become unavailable while the TCP connection is down and recover when it is re-established.

## Diagnostics

The entry supports Home Assistant's diagnostics download (**Settings → Devices & services → IntesisBox → three-dot menu → Download diagnostics**). It contains the device model and firmware, the capability lists negotiated at connect time, which vane axes the device has refused to set, the current state of every function, and the controller's connection state. The host address, MAC and unique id are redacted before download.

## Behaviour and limitations

**Keepalive.** A WMP gateway closes an idle TCP connection after about a minute. The integration sends `PING` every 45 seconds to keep the socket open, and reconnects with exponential backoff (1.5 s rising to 60 s) if the connection drops.

**Two connections only.** A WMP gateway allows at most two simultaneous TCP connections. Do not run two clients (for example this integration plus another controller, or two Home Assistant instances) against the same box.

**One command at a time.** Commands are serialised and only one `SET` is ever outstanding. The device answers a rejected write with a bare `ERR` that names no command, so serialising is what lets the integration attribute a refusal to the write that caused it.

**Read-only vanes.** Some gateways report a vane position but refuse every attempt to set one. The integration cannot tell this in advance, so it offers the swing control and learns from the first `ERR`: that axis is marked read-only for the session, the control is removed from the climate entity, and a warning is logged. The learned state is not persisted, so after a restart or reload the control reappears until something tries it again. The position is still reported as an attribute either way.

**Legacy swing values.** Automations written against versions before 2.3 that call `climate.set_swing_mode` with `vertical`, `horizontal` or `both` still work; they are mapped onto the per-axis controls.

**Degraded devices.** If a device does not answer a `LIMITS` query, the integration still comes up with whatever it did learn. A device that reports no usable modes is offered the standard WMP set; a device that reports no fan speeds gets a climate entity without fan control.

**Polling backstop.** State changes arrive as pushes. A full status refresh is also requested every five minutes, and the ambient temperature every minute, so a missed push cannot leave a value stale indefinitely.

## Troubleshooting

**Enable debug logging** for the integration from **Settings → System → Logs**, or call the `logger.set_level` action with `custom_components.intesisbox: debug`. The log shows every line sent to and received from the gateway.

**Download diagnostics** from the entry (see above) and attach the file to any issue. It answers most questions about what the device reported and what the integration made of it.

**Entry keeps retrying.** Setup waits up to 30 seconds for the gateway to complete its handshake. If it does not, Home Assistant retries on its own schedule. Check that nothing else is holding the gateway's two connections, and that the address is still correct (use Reconfigure if it has changed).

**Swing control disappeared.** Your gateway refused a vane write. This is expected on some hardware; see "Read-only vanes" above.

Report problems at <https://github.com/jackmcintyre/hass-intesisbox/issues>.

## Development

The project is managed with [uv](https://docs.astral.sh/uv/) and needs Python 3.14.2 or newer.

```sh
uv sync --group dev
uv run python -m pytest tests/ -q
```

There are 65 tests. `pytest-homeassistant-custom-component` provides a real Home Assistant instance, so the config-flow and setup tests exercise Home Assistant's own unique-id, reconfigure, registry and unload machinery rather than stand-ins. The transport tests run against a WMP emulator in `tests/emulator.py` that can be told to misbehave the way real devices do: tear frames, refuse a write, ignore a `LIMITS` query, or drop the connection.

The emulator can also be run standalone, listening on `0.0.0.0:3310`, to develop against without hardware:

```sh
uv run python -m tests.emulator
```

Linting and formatting use `ruff` (v0.14.10), pinned in `.pre-commit-config.yaml` alongside `codespell` and `mypy`. CI runs the same pre-commit hooks and HACS validation on every pull request.

```sh
pre-commit run --all-files
```

## Credits

- [jnimmo/hass-intesisbox](https://github.com/jnimmo/hass-intesisbox), the upstream integration this fork is based on.
- The WMP protocol is specified by Intesis; the v1.9 specification is included in the repository as `wmp-protocol-specs-v1-9.pdf`.
