"""
RemoteDesk Relay Server
Uses aiohttp — handles HTTP health checks + WebSocket on same port.
Works on render.com free tier.
"""
import asyncio, json, logging, os
from aiohttp import web
import aiohttp

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [RELAY] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("relay")

PORT    = int(os.environ.get("PORT", 10000))
TIMEOUT = 600   # 10 min

# device_id → asyncio.Queue (holds the viewer WS when one connects)
registry: dict[str, asyncio.Queue] = {}


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024)
    await ws.prepare(request)

    try:
        first = await asyncio.wait_for(ws.receive(), timeout=15)
        if first.type != aiohttp.WSMsgType.TEXT:
            return ws
        data = json.loads(first.data)
        role = data.get("role", "")

        if role == "host":
            await _handle_host(ws, data)
        elif role == "viewer":
            await _handle_viewer(ws, data)
        elif role == "ping":
            await ws.send_str(json.dumps({"status": "pong"}))
        else:
            await ws.send_str(json.dumps({"status": "error", "msg": "unknown role"}))

    except Exception as e:
        log.debug(f"ws_handler error: {e}")

    return ws


async def _handle_host(ws: web.WebSocketResponse, data: dict):
    device_id = data.get("device_id", "").strip()
    if not device_id:
        await ws.send_str(json.dumps({"status": "error", "msg": "no device_id"}))
        return

    log.info(f"Host {device_id} registered")
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    registry[device_id] = q
    await ws.send_str(json.dumps({"status": "waiting"}))

    try:
        viewer_ws = await asyncio.wait_for(q.get(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        log.info(f"Host {device_id} timed out")
        return
    finally:
        registry.pop(device_id, None)

    log.info(f"Piping {device_id}")
    await ws.send_str(json.dumps({"status": "connected"}))
    await _pipe(ws, viewer_ws)
    log.info(f"Session {device_id} ended")


async def _handle_viewer(ws: web.WebSocketResponse, data: dict):
    device_id = data.get("device_id", "").strip()
    log.info(f"Viewer → {device_id}")

    q = registry.get(device_id)
    if q is None:
        await ws.send_str(json.dumps({
            "status": "error",
            "msg": "Device not found or offline"
        }))
        return

    await ws.send_str(json.dumps({"status": "connected"}))
    await q.put(ws)

    # Stay alive while pipe runs
    try:
        await asyncio.wait_for(ws.wait_closed(), timeout=TIMEOUT)
    except Exception:
        pass


async def _pipe(a: web.WebSocketResponse, b: web.WebSocketResponse):
    async def fwd(src, dst):
        try:
            async for msg in src:
                if dst.closed:
                    break
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE,
                                  aiohttp.WSMsgType.ERROR):
                    break
        except Exception:
            pass
        finally:
            try: await dst.close()
            except: pass

    await asyncio.gather(fwd(a, b), fwd(b, a), return_exceptions=True)


# ── HTTP health check ─────────────────────────────────────────────────────────

async def health(request: web.Request):
    return web.Response(
        text=f"RemoteDesk Relay OK — {len(registry)} host(s) online",
        content_type="text/plain"
    )


# ── App ───────────────────────────────────────────────────────────────────────

async def main():
    app = web.Application()
    app.router.add_get("/",   health)
    app.router.add_get("/ws", ws_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    log.info(f"Relay ready on port {PORT}")
    log.info(f"Connect via: wss://YOUR-APP.onrender.com/ws")
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
