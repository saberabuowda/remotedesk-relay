"""
RemoteDesk Relay Server — aiohttp WebSocket
Works on render.com free tier.
"""
import asyncio, json, logging, os
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [RELAY] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("relay")

PORT    = int(os.environ.get("PORT", 10000))
TIMEOUT = 600

registry: dict[str, asyncio.Queue] = {}


async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(
        max_msg_size=10 * 1024 * 1024,
        heartbeat=30,        # aiohttp sends ping every 30s automatically
        receive_timeout=120, # close if no data for 2 min
    )
    await ws.prepare(request)

    try:
        first = await asyncio.wait_for(ws.receive(), timeout=20)
        if first.type != WSMsgType.TEXT:
            await ws.close()
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


async def _handle_host(ws, data):
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


async def _handle_viewer(ws, data):
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

    try:
        await asyncio.wait_for(ws.wait_closed(), timeout=TIMEOUT)
    except Exception:
        pass


async def _pipe(a, b):
    async def fwd(src, dst):
        try:
            async for msg in src:
                if dst.closed:
                    break
                if msg.type == WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    # Skip internal JSON control messages (waiting/connected)
                    try:
                        j = json.loads(msg.data)
                        if "status" in j:
                            continue
                    except Exception:
                        pass
                    await dst.send_str(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        except Exception:
            pass
        finally:
            try: await dst.close()
            except: pass

    await asyncio.gather(fwd(a, b), fwd(b, a), return_exceptions=True)


async def health(request):
    return web.Response(
        text=f"RemoteDesk Relay OK — {len(registry)} host(s) online",
        content_type="text/plain"
    )


async def main():
    app = web.Application()
    app.router.add_get("/",   health)
    app.router.add_get("/ws", ws_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    log.info(f"Relay ready on port {PORT}")
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
