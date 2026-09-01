"""Minimal WMP device emulator used by the transport tests.

Unlike the emulator that used to sit inside the component package, this one
never starts a server on import, and it can be told to misbehave in the ways
real devices do: writing a byte at a time, rejecting a command, or dropping
the connection.
"""

from __future__ import annotations

import asyncio

DEFAULT_STATE = {
    "MODE": "AUTO",
    "SETPTEMP": "210",
    "ONOFF": "OFF",
    "FANSP": "AUTO",
    "AMBTEMP": "180",
    "VANEUD": "AUTO",
    "VANELR": "AUTO",
    "ERRSTATUS": "OK",
    "ERRCODE": "",
}

LIMITS = {
    "FANSP": "[AUTO,1,2,3,4]",
    "VANEUD": "[AUTO,1,2,3,SWING]",
    "VANELR": "[AUTO,1,2,3,SWING]",
    "SETPTEMP": "[160,300]",
    "MODE": "[AUTO,HEAT,DRY,COOL,FAN]",
}

ID_GEN1 = "ID:IS-IR-WMP-1,001DC9A2C911,192.168.100.246,ASCII,v1.0.2,-44"
ID_V6 = "ID:INWMPUNI001I000,001DC9A2C911,192.168.100.246,v1.0.1,-44,WMP_A2C911,N,6"


class Emulator(asyncio.Protocol):
    """A WMP device that can be made to behave badly on purpose."""

    #: Write responses one byte at a time, forcing the client to reassemble.
    tear_frames = False
    #: Answer the next SET with ERR instead of ACK.
    reject_next_set = False
    #: Which ID banner to report.
    id_banner = ID_GEN1
    #: LIMITS functions the device silently ignores, as real units do for
    #: capabilities they do not have.
    unanswered_limits: set[str] = set()
    #: Live connections, so a test can drop them.
    connections: list[Emulator] = []

    def __init__(self) -> None:
        """Start with a fresh copy of the default device state."""
        self.state = dict(DEFAULT_STATE)
        self.buffer = b""

    @classmethod
    def reset(cls) -> None:
        """Return the class-level knobs to their defaults."""
        cls.tear_frames = False
        cls.reject_next_set = False
        cls.id_banner = ID_GEN1
        cls.unanswered_limits = set()
        cls.connections = []

    @classmethod
    def drop_all(cls) -> None:
        """Close every live connection, as the device's watchdog would."""
        for conn in list(cls.connections):
            conn.transport.close()
        cls.connections = []

    def connection_made(self, transport):
        """Register the new connection."""
        self.transport = transport
        Emulator.connections.append(self)

    def connection_lost(self, exc):
        """Forget the closed connection."""
        if self in Emulator.connections:
            Emulator.connections.remove(self)

    def send(self, text: str) -> None:
        """Write a response, optionally one byte at a time."""
        data = text.encode("ascii")
        if Emulator.tear_frames:
            for index in range(len(data)):
                self.transport.write(data[index : index + 1])
        else:
            self.transport.write(data)

    def data_received(self, data: bytes) -> None:
        """Reassemble lines and dispatch them."""
        self.buffer += data
        self.buffer = self.buffer.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        *lines, self.buffer = self.buffer.split(b"\n")
        for raw in lines:
            line = raw.decode("ascii").strip()
            if line:
                self.handle(line)

    def handle(self, line: str) -> None:
        """Respond to one command."""
        head = line.split(",")[0]

        if line == "ID":
            self.send(f"{Emulator.id_banner}\r\n")
        elif line == "PING":
            self.send("ACK\r\n")
        elif line.startswith("LIMITS:"):
            function = line.split(":", 1)[1]
            if function in Emulator.unanswered_limits:
                return
            if function in LIMITS:
                self.send(f"LIMITS:{function},{LIMITS[function]}\r\n")
        elif head == "GET":
            function = line.split(":", 1)[1]
            if function == "*":
                for key, value in self.state.items():
                    self.send(f"CHN,1:{key},{value}\r\n")
            elif function in self.state:
                self.send(f"CHN,1:{function},{self.state[function]}\r\n")
            else:
                self.send("ERR\r\n")
        elif head == "SET":
            if Emulator.reject_next_set:
                Emulator.reject_next_set = False
                self.send("ERR\r\n")
                return
            payload = line.split(":", 1)[1]
            function, value = payload.split(",", 1)
            self.send("ACK\r\n")
            if self.state.get(function) != value:
                self.state[function] = value
                self.send(f"CHN,1:{function},{value}\r\n")


async def start(host: str = "127.0.0.1", port: int = 0):
    """Start an emulator server and return it."""
    loop = asyncio.get_running_loop()
    Emulator.reset()
    return await loop.create_server(Emulator, host, port)


if __name__ == "__main__":

    async def _main() -> None:
        server = await start("0.0.0.0", 3310)
        await server.serve_forever()

    asyncio.run(_main())
