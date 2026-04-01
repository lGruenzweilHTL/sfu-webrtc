import logging
from aiohttp import web
from manager import SFUManager

logger = logging.getLogger("sfu-server")

class SFUHandlers:
    def __init__(self, manager: SFUManager):
        self.manager = manager

    async def handle_publish(self, request: web.Request) -> web.Response:
        params = await request.json()
        result = await self.manager.add_publisher(params["sdp"], params["type"])
        return web.json_response(result)

    async def handle_subscribe(self, request: web.Request) -> web.Response:
        params = await request.json()
        exclude = params.get("exclude_publisher")
        result = await self.manager.add_subscriber(exclude_publisher=exclude)
        return web.json_response(result)

    async def handle_subscribe_answer(self, request: web.Request) -> web.Response:
        params = await request.json()
        session_id = params["session_id"]
        ok = await self.manager.set_answer(session_id, params["sdp"], params["type"])
        if not ok:
            return web.json_response({"error": "session not found"}, status=404)
        return web.json_response({"ok": True})

    async def handle_disconnect(self, request: web.Request) -> web.Response:
        params = await request.json()
        session_id = params.get("session_id")
        await self.manager.cleanup(session_id)
        return web.json_response({"ok": True})

    async def handle_list_publishers(self, request: web.Request) -> web.Response:
        return web.json_response({
            "publishers": [
                {
                    "publisher_id": p["publisher_id"],
                    "has_audio": p["audio"] is not None,
                    "has_video": p["video"] is not None
                }
                for p in self.manager.published_tracks
            ]
        })

    # Note: ICE candidates handling could be moved to manager if needed,
    # but since it's state-heavy and the client's current use is simple polling,
    # keeping it simple here for now or moving to manager if it gets complex.
    async def handle_get_ice_candidates(self, request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        candidates = self.manager.ice_candidates.pop(session_id, [])
        return web.json_response({"candidates": candidates})

    async def handle_ice_candidate(self, request: web.Request) -> web.Response:
        params = await request.json()
        session_id = params["session_id"]
        pc = self.manager.peer_connections.get(session_id)
        if not pc:
            return web.json_response({"error": "session not found"}, status=404)

        from aiortc import RTCIceCandidate
        candidate_data = params.get("candidate")
        if candidate_data:
            candidate = RTCIceCandidate(
                component=1, foundation="0", ip=None, port=0, priority=0,
                protocol="udp", type="host",
                sdpMid=candidate_data.get("sdpMid"),
                sdpMLineIndex=candidate_data.get("sdpMLineIndex"),
            )
            await pc.addIceCandidate(candidate)
        return web.json_response({"ok": True})
