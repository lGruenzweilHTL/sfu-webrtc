"""
WebRTC SFU (Selective Forwarding Unit) Server
=============================================
This server receives audio/video tracks from publisher clients and
forwards them to all subscriber clients.
"""

import argparse
import logging
from aiohttp import web
import aiohttp_cors

from manager import SFUManager
from handlers import SFUHandlers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sfu-server")

async def on_shutdown(app):
    manager = app["sfu_manager"]
    logger.info("Shutting down — closing all peer connections")
    await manager.close_all()

def create_app() -> web.Application:
    app = web.Application()
    
    manager = SFUManager()
    app["sfu_manager"] = manager
    handlers = SFUHandlers(manager)

    app.on_shutdown.append(on_shutdown)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods=["GET", "POST", "OPTIONS"],
        )
    })

    routes = [
        app.router.add_post("/publish", handlers.handle_publish),
        app.router.add_get("/ws", handlers.handle_websocket), # New WebSocket endpoint
        app.router.add_get("/publishers", handlers.handle_list_publishers),
        app.router.add_post("/disconnect", handlers.handle_disconnect),
    ]
    
    for route in routes:
        cors.add(route)

    return app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WebRTC SFU Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logger.info(f"Starting SFU server on {args.host}:{args.port}")
    web.run_app(create_app(), host=args.host, port=args.port)
