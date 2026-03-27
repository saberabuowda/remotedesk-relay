"""
RemoteDesk Relay Server
- Runs on any VPS or render.com (free)
- Connects Host and Viewer by Device ID
- Just forwards encrypted bytes — never sees content
"""
import asyncio, json, logging, time, os
from dataclasses import dataclass, field
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RELAY] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("relay")

PORT     = int(os.environ.get("PORT", 7979))
TIMEOUT  = 60          # seconds to wait for peer
BUF      = 65536       # pipe buffer size

# ── Message types (same as client) ───────────────────────────────────────────
RELAY_REGISTER = 0xA0  # host registers its device_id
RELAY_CONNECT  = 0xA1  # viewer requests to connect to device_id
RELAY_READY    = 0xA2  # relay tells both sides: peer is connected
RELAY_FAIL     = 0xA3  # relay tells viewer: device not found / timeout
RELAY_PING     = 0x40
RELAY_PONG     = 0x41
HEADER         = 5


def pack(t: int, payload: bytes) -> bytes:
    import struct
    return struct.pack("!BI", t, len(payload)) + payload


async def read_msg(reader: asyncio.StreamReader):
    import struct
    hdr  = await asyncio.wait_for(reader.readexactly(HEADER), timeout=30)
    t, n = struct.unpack("!BI", hdr)
    data = await reader.readexactly(n) if n else b""
    return t, data


# ── Peer registry ─────────────────────────────────────────────────────────────

@dataclass
class WaitingHost:
    device_id: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    registered_at: float = field(default_factory=time.time)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    viewer_reader: Optional[asyncio.StreamReader] = None
    viewer_writer: Optional[asyncio.StreamWriter] = None


class Registry:
    def __init__(self):
        self._hosts: dict[str, WaitingHost] = {}

    def register(self, device_id: str, entry: WaitingHost):
        self._hosts[device_id] = entry
        log.info(f"Registered host {device_id} | total={len(self._hosts)}")

    def unregister(self, device_id: str):
        self._hosts.pop(device_id, None)

    def get(self, device_id: str) -> Optional[WaitingHost]:
        return self._hosts.get(device_id)

    def cleanup_old(self):
        now = time.time()
        dead = [k for k, v in self._hosts.items()
                if now - v.registered_at > TIMEOUT * 2]
        for k in dead:
            self._hosts.pop(k)
            log.info(f"Cleaned up stale host {k}")


registry = Registry()


# ── Pipe two streams together ─────────────────────────────────────────────────

async def pipe(src_r: asyncio.StreamReader, dst_w: asyncio.StreamWriter):
    """Forward bytes from src to dst until EOF or error."""
    try:
        while True:
            data = await src_r.read(BUF)
            if not data:
                break
            dst_w.write(data)
            await dst_w.drain()
    except Exception:
        pass
    finally:
        try:
            dst_w.close()
        except Exception:
            pass


# ── Connection handler ────────────────────────────────────────────────────────

async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    log.info(f"New connection: {addr[0]}:{addr[1]}")

    try:
        t, data = await read_msg(reader)
    except Exception as e:
        log.warning(f"Failed to read initial msg from {addr}: {e}")
        writer.close()
        return

    # ── HOST registers ────────────────────────────────────────────────────────
    if t == RELAY_REGISTER:
        try:
            info      = json.loads(data)
            device_id = info["device_id"]
        except Exception:
            writer.close()
            return

        entry = WaitingHost(device_id=device_id, reader=reader, writer=writer)
        registry.register(device_id, entry)
        registry.cleanup_old()

        # Tell host we got it
        writer.write(pack(RELAY_READY, json.dumps({"status": "waiting"}).encode()))
        await writer.drain()

        log.info(f"Host {device_id} waiting for viewer...")

        # Wait until a viewer connects (or timeout)
        try:
            await asyncio.wait_for(entry.event.wait(), timeout=TIMEOUT * 10)
        except asyncio.TimeoutError:
            registry.unregister(device_id)
            try:
                writer.close()
            except Exception:
                pass
            return

        # Viewer connected — pipe both ways simultaneously
        vr, vw = entry.viewer_reader, entry.viewer_writer
        log.info(f"Piping {device_id}: host ↔ viewer")
        await asyncio.gather(
            pipe(reader, vw),
            pipe(vr, writer),
            return_exceptions=True,
        )
        registry.unregister(device_id)
        log.info(f"Session {device_id} ended")

    # ── VIEWER requests connection ─────────────────────────────────────────────
    elif t == RELAY_CONNECT:
        try:
            info      = json.loads(data)
            device_id = info["device_id"]
        except Exception:
            writer.close()
            return

        log.info(f"Viewer requesting {device_id}")
        entry = registry.get(device_id)

        if entry is None:
            writer.write(pack(RELAY_FAIL,
                              json.dumps({"error": "Device not found or offline"}).encode()))
            await writer.drain()
            writer.close()
            log.info(f"Device {device_id} not found")
            return

        # Hand viewer's streams to host entry
        entry.viewer_reader = reader
        entry.viewer_writer = writer
        entry.event.set()   # wake up host handler

        # Tell viewer we're connected
        writer.write(pack(RELAY_READY, json.dumps({"status": "connected"}).encode()))
        await writer.drain()

        # Viewer handler exits here — piping is done in host handler
        log.info(f"Viewer handed off to host handler for {device_id}")

    # ── PING ──────────────────────────────────────────────────────────────────
    elif t == RELAY_PING:
        writer.write(pack(RELAY_PONG, b""))
        await writer.drain()
        writer.close()

    else:
        log.warning(f"Unknown msg type {hex(t)} from {addr}")
        writer.close()


# ── Health check (HTTP) for render.com ───────────────────────────────────────

async def http_health(reader, writer):
    """Tiny HTTP handler so render.com health check passes."""
    try:
        await reader.read(1024)
        body = b"RemoteDesk Relay OK"
        resp = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(resp)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def smart_handle(reader, writer):
    """Peek at first byte: HTTP GET → health check, else relay protocol."""
    try:
        first = await asyncio.wait_for(reader.read(1), timeout=5)
    except Exception:
        writer.close()
        return

    # Re-inject the byte into a new reader-like object
    class PeekReader:
        def __init__(self, first, real):
            self._buf  = first
            self._real = real

        async def readexactly(self, n):
            if self._buf:
                if n == 1:
                    b, self._buf = self._buf, b""
                    return b
                rest = await self._real.readexactly(n - 1)
                b, self._buf = self._buf, b""
                return b + rest
            return await self._real.readexactly(n)

        async def read(self, n):
            if self._buf:
                b, self._buf = self._buf, b""
                return b
            return await self._real.read(n)

    if first == b"G":   # HTTP GET
        await http_health(PeekReader(first, reader), writer)
    else:
        await handle(PeekReader(first, reader), writer)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    server = await asyncio.start_server(smart_handle, "0.0.0.0", PORT)
    addrs  = [str(s.getsockname()) for s in server.sockets]
    log.info(f"Relay server listening on {addrs}")
    log.info(f"Deploy to render.com → set PORT env var if needed")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Relay stopped.")
