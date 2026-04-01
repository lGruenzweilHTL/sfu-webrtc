import logging
import json
from aiohttp import web, WSMsgType
from manager import SFUManager
from aiortc import RTCIceCandidate
from aiortc.sdp import candidate_from_sdp

logger = logging.getLogger("sfu-server")

class SFUHandlers:
    def __init__(self, manager: SFUManager):
        self.manager = manager

    async def handle_publish(self, request: web.Request) -> web.Response:
        params = await request.json()
        result = await self.manager.add_publisher(params["sdp"], params["type"])
        return web.json_response(result)

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

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        session_id = None

        async def notify_client(message: dict):
            try:
                await ws.send_json(message)
            except Exception:
                pass

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    msg_type = data.get("type")

                    if not session_id:
                        if msg_type == "subscribe":
                            exclude = data.get("exclude_publisher")
                            offer_data = await self.manager.add_subscriber(notify_client, exclude)
                            session_id = offer_data["session_id"]
                            await notify_client(offer_data)
                            continue
                        else:
                            break

                    if data.get("session_id") != session_id:
                        continue

                    if msg_type in ("answer", "answer_renegotiation"):
                        await self.manager.set_answer(session_id, data["sdp"], data["type"])
                    elif msg_type == "ice_candidate":
                        pc = self.manager.peer_connections.get(session_id)
                        cand = data.get("candidate")
                        if pc and cand:
                            try:
                                candidate = candidate_from_sdp(cand["candidate"])
                                candidate.sdpMid = cand.get("sdpMid")
                                candidate.sdpMLineIndex = cand.get("sdpMLineIndex")
                                await pc.addIceCandidate(candidate)
                            except Exception as e:
                                logger.error(f"Failed to add ICE candidate: {e}")
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            if session_id:
                await self.manager.cleanup(session_id)
        return ws
