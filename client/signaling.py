import aiohttp

class SFUClient:
    def __init__(self, url, session: aiohttp.ClientSession):
        self.url = url.rstrip("/")
        self.s = session

    async def publish(self, sdp):
        async with self.s.post(f"{self.url}/publish", json={"sdp": sdp, "type": "offer"}) as r:
            return await r.json()

    async def subscribe(self, exclude=None):
        async with self.s.post(f"{self.url}/subscribe",
                               json={"exclude_publisher": exclude} if exclude else {}) as r:
            return await r.json()

    async def send_answer(self, sid, sdp, t):
        async with self.s.post(f"{self.url}/subscribe/answer",
                               json={"session_id": sid, "sdp": sdp, "type": t}) as r:
            return await r.json()

    async def disconnect(self, sid):
        async with self.s.post(f"{self.url}/disconnect",
                               json={"session_id": sid}) as r:
            return await r.json()
