import aiohttp
import json
import logging

logger = logging.getLogger("webrtc-client")

class SFUClient:
    def __init__(self, url, session: aiohttp.ClientSession):
        self.url = url.rstrip("/")
        # Convert http://127.0.0.1:8080 to ws://127.0.0.1:8080
        self.ws_url = self.url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
        self.s = session

    async def publish(self, sdp):
        async with self.s.post(f"{self.url}/publish", json={"sdp": sdp, "type": "offer"}) as r:
            return await r.json()

    async def connect_websocket(self):
        return await self.s.ws_connect(self.ws_url)

    async def disconnect(self, sid):
        async with self.s.post(f"{self.url}/disconnect",
                               json={"session_id": sid}) as r:
            return await r.json()
