"""
RemoteDesk Relay Server — WebSocket version
Works on render.com free tier (HTTP/WebSocket only)
"""
import asyncio, json, logging, os, time
import websockets
from websockets.server import WebSocketServerProtocol

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [RELAY] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("relay")

PORT = int(os.environ.get("PORT", 10000))
TIMEOUT = 600   # 10 min max wait for viewer

# Registry: device_id → asyncio.Queue (viewer websocket goes in here)
registry: dict[str, asyncio.Queue] = {}


async def handler(ws: WebSocketServerProtocol):
    addr = ws.remote_address
    try:
        # First message tells us the role
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        msg = json.loads(raw)
        role = msg.get("role")

        # ── HOST registers ────────────────────────────────────────────────────
        if role == "host":
            device_id = msg.get("device_id", "").strip()
            if not device_id:
                await ws.send(json.dumps({"status": "error", "msg": "no device_id"}))
                return

            log.info(f"Host registered: {device_id}")
            q: asyncio.Queue = asyncio.Queue(maxsize=1)
            registry[device_id] = q

            await ws.send(json.dumps({"status": "waiting"}))

            try:
                # Wait for a viewer to be matched
                viewer_ws = await asyncio.wait_for(q.get(), timeout=TIMEOUT)
            except asyncio.TimeoutError:
                log.info(f"Host {device_id} timed out waiting")
                registry.pop(device_id, None)
                return
            finally:
                registry.pop(device_id, None)

            log.info(f"Piping host {device_id} ↔ viewer")
            await ws.send(json.dumps({"status": "connected"}))

            # Pipe binary frames between host and viewer
            await pipe_ws(ws, viewer_ws)

        # ── VIEWER connects ───────────────────────────────────────────────────
        elif role == "viewer":
            device_id = msg.get("device_id", "").strip()
            log.info(f"Viewer wants: {device_id}")

            q = registry.get(device_id)
            if q is None:
                await ws.send(json.dumps({"status": "error",
                                          "msg": "Device not found or offline"}))
                return

            # Tell viewer we found the host
            await ws.send(json.dumps({"status": "connected"}))

            # Hand viewer WS to host queue
            await q.put(ws)

            # Keep viewer alive until pipe finishes
            try:
                await asyncio.wait_for(ws.wait_closed(), timeout=TIMEOUT)
            except Exception:
                pass

        # ── PING ──────────────────────────────────────────────────────────────
        elif role == "ping":
            await ws.send(json.dumps({"status": "pong"}))

        else:
            await ws.send(json.dumps({"status": "error", "msg": "unknown role"}))

    except Exception as e:
        log.debug(f"Handler error {addr}: {e}")


async def pipe_ws(ws_a: WebSocketServerProtocol,
                  ws_b: WebSocketServerProtocol):
    """Bidirectional pipe between two WebSocket connections."""
    async def forward(src, dst):
        try:
            async for msg in src:
                if dst.open:
                    await dst.send(msg)
        except Exception:
            pass
        finally:
            try: await dst.close()
            except: pass

    await asyncio.gather(
        forward(ws_a, ws_b),
        forward(ws_b, ws_a),
        return_exceptions=True,
    )


async def main():
    log.info(f"Relay WebSocket server starting on port {PORT}")
    async with websockets.serve(handler, "0.0.0.0", PORT,
                                 ping_interval=20, ping_timeout=30,
                                 max_size=10 * 1024 * 1024):  # 10MB max frame
        log.info(f"Ready — ws://0.0.0.0:{PORT}")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
