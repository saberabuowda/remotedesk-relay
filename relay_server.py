import asyncio, json, logging, os
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [RELAY] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("relay")

PORT = int(os.environ.get("PORT", 10000))

# Store WebSockets: device_id -> {"host": ws, "viewer": ws}
rooms = {}

async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(
        max_msg_size=10 * 1024 * 1024,
        heartbeat=30,
        receive_timeout=120,
    )
    await ws.prepare(request)

    role = None
    device_id = None

    try:
        first = await asyncio.wait_for(ws.receive(), timeout=20)
        if first.type != WSMsgType.TEXT:
            await ws.close()
            return ws

        data = json.loads(first.data)
        role = data.get("role", "")
        device_id = data.get("device_id", "").strip()

        if not device_id:
            await ws.close()
            return ws

        if device_id not in rooms:
            rooms[device_id] = {"host": None, "viewer": None}

        # 1. Register peer
        if role == "host":
            rooms[device_id]["host"] = ws
            log.info(f"Host {device_id} registered")
            await ws.send_str(json.dumps({"status": "waiting"}))
        elif role == "viewer":
            if rooms[device_id]["host"] is None:
                await ws.send_str(json.dumps({"status": "error", "msg": "Device not found or offline"}))
                await ws.close()
                return ws
            
            rooms[device_id]["viewer"] = ws
            log.info(f"Viewer {device_id} connected")
            
            # Notify both
            await ws.send_str(json.dumps({"status": "connected"}))
            await rooms[device_id]["host"].send_str(json.dumps({"status": "connected"}))
        else:
            await ws.close()
            return ws

        # 2. Main message loop (Task-Safe reading)
        async for msg in ws:
            partner_ws = None
            if role == "host":
                partner_ws = rooms[device_id]["viewer"]
            elif role == "viewer":
                partner_ws = rooms.get(device_id, {}).get("host")

            if not partner_ws or partner_ws.closed:
                continue

            if msg.type == WSMsgType.BINARY:
                await partner_ws.send_bytes(msg.data)
            elif msg.type == WSMsgType.TEXT:
                # Filter out pure control messages
                try:
                    j = json.loads(msg.data)
                    if "role" in j:
                        continue 
                except:
                    pass
                await partner_ws.send_str(msg.data)

    except Exception as e:
        log.debug(f"ws_handler err {device_id}: {e}")
    finally:
        log.info(f"{role} {device_id} cleanly disconnected")
        # Cleanup
        if device_id in rooms:
            if role == "host":
                rooms[device_id]["host"] = None
                # If host leaves, try to close viewer
                if rooms[device_id]["viewer"]:
                    try: await rooms[device_id]["viewer"].close()
                    except: pass
            elif role == "viewer":
                rooms[device_id]["viewer"] = None
            
            # Remove room if both empty
            if rooms[device_id]["host"] is None and rooms[device_id]["viewer"] is None:
                del rooms[device_id]

    return ws

async def health(request):
    return web.Response(text=f"RemoteDesk Relay OK | Active Rooms: {len(rooms)}")

async def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Relay ready on port {PORT}")
    await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
