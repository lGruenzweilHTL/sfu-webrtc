"""
WebRTC SFU (Selective Forwarding Unit) Server
=============================================
This server receives audio/video tracks from publisher clients and
forwards them to all subscriber clients. Unlike a P2P setup, all
media flows through this central server.

Architecture:
  Publisher  ──▶  SFU Server  ──▶  Subscriber A
                              ──▶  Subscriber B
                              ──▶  Subscriber C

Signaling is done via HTTP (offer/answer exchange + ICE candidates).
"""

import asyncio
import logging
import uuid
from typing import Dict, List, Optional, Set

from aiohttp import web
import aiohttp_cors
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaRelay

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sfu-server")


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

relay = MediaRelay()          # aiortc helper that copies tracks to many consumers

# All active peer connections, keyed by a random session ID
peer_connections: Dict[str, RTCPeerConnection] = {}

# Tracks published by clients: list of (audio_track, video_track) tuples
# We use MediaRelay proxies so each subscriber gets its own independent copy.
published_tracks: List[Dict] = []   # [{"audio": track|None, "video": track|None, "publisher_id": str}]

# ICE candidate queues per session (for trickle ICE)
ice_candidates: Dict[str, List[dict]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_session_id() -> str:
    return str(uuid.uuid4())


def get_published_relay_tracks(exclude_publisher: Optional[str] = None):
    """Return relayed copies of all published tracks, optionally excluding one publisher."""
    result = []
    for pub in published_tracks:
        if exclude_publisher and pub["publisher_id"] == exclude_publisher:
            continue
        entry = {"publisher_id": pub["publisher_id"]}
        if pub.get("audio"):
            entry["audio"] = relay.subscribe(pub["audio"])
        if pub.get("video"):
            entry["video"] = relay.subscribe(pub["video"])
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def handle_publish(request: web.Request) -> web.Response:
    """
    Publisher endpoint.
    The client sends an SDP offer containing its camera/mic tracks.
    The server answers and starts recording/relaying those tracks.
    """
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    session_id = create_session_id()
    pc = RTCPeerConnection()
    peer_connections[session_id] = pc
    ice_candidates[session_id] = []

    pub_entry = { "publisher_id": session_id, "audio": None, "video": None }
    published_tracks.append(pub_entry)

    @pc.on("track")
    async def on_track(track: MediaStreamTrack):
        logger.info(f"[{session_id}] Publisher sent {track.kind} track")
        if track.kind == "audio":
            pub_entry["audio"] = track
        elif track.kind == "video":
            pub_entry["video"] = track

        @track.on("ended")
        async def on_ended():
            logger.info(f"[{session_id}] {track.kind} track ended")

    @pc.on("connectionstatechange")
    async def on_state():
        logger.info(f"[{session_id}] Publisher state: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed"):
            await _cleanup(session_id)

    @pc.on("icecandidate")
    async def on_ice(candidate):
        if candidate:
            ice_candidates[session_id].append({
                "candidate": candidate.candidate,
                "sdpMid": candidate.sdpMid,
                "sdpMLineIndex": candidate.sdpMLineIndex,
            })

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response({
        "session_id": session_id,
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
    })


async def handle_subscribe(request: web.Request) -> web.Response:
    """
    Subscriber endpoint.
    The server creates an offer containing relayed tracks from all publishers,
    and the client sends back an answer.
    """
    params = await request.json()
    exclude = params.get("exclude_publisher")  # subscribers can exclude their own publish session

    session_id = create_session_id()
    pc = RTCPeerConnection()
    peer_connections[session_id] = pc
    ice_candidates[session_id] = []

    # Add all currently published tracks to this subscriber connection
    relay_tracks = get_published_relay_tracks(exclude_publisher=exclude)
    for entry in relay_tracks:
        if "audio" in entry:
            pc.addTrack(entry["audio"])
        if "video" in entry:
            pc.addTrack(entry["video"])

    if not relay_tracks:
        logger.info(f"[{session_id}] No published tracks available yet for subscriber")

    @pc.on("connectionstatechange")
    async def on_state():
        logger.info(f"[{session_id}] Subscriber state: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed"):
            await _cleanup(session_id)

    @pc.on("icecandidate")
    async def on_ice(candidate):
        if candidate:
            ice_candidates[session_id].append({
                "candidate": candidate.candidate,
                "sdpMid": candidate.sdpMid,
                "sdpMLineIndex": candidate.sdpMLineIndex,
            })

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    return web.json_response({
        "session_id": session_id,
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
    })


async def handle_subscribe_answer(request: web.Request) -> web.Response:
    """Receive the subscriber's SDP answer."""
    params = await request.json()
    session_id = params["session_id"]
    pc = peer_connections.get(session_id)
    if not pc:
        return web.json_response({"error": "session not found"}, status=404)

    answer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    await pc.setRemoteDescription(answer)
    return web.json_response({"ok": True})


async def handle_ice_candidate(request: web.Request) -> web.Response:
    """Add a trickle ICE candidate from the client."""
    params = await request.json()
    session_id = params["session_id"]
    pc = peer_connections.get(session_id)
    if not pc:
        return web.json_response({"error": "session not found"}, status=404)

    from aiortc import RTCIceCandidate
    candidate_data = params.get("candidate")
    if candidate_data:
        candidate = RTCIceCandidate(
            component=1,
            foundation="0",
            ip=None,
            port=0,
            priority=0,
            protocol="udp",
            type="host",
            sdpMid=candidate_data.get("sdpMid"),
            sdpMLineIndex=candidate_data.get("sdpMLineIndex"),
        )
        # aiortc accepts raw candidate strings via addIceCandidate
        await pc.addIceCandidate(candidate)

    return web.json_response({"ok": True})


async def handle_get_ice_candidates(request: web.Request) -> web.Response:
    """Poll for ICE candidates the server generated for a session (simple trickle)."""
    session_id = request.match_info["session_id"]
    candidates = ice_candidates.pop(session_id, [])
    return web.json_response({"candidates": candidates})


async def handle_list_publishers(request: web.Request) -> web.Response:
    """Return the list of active publisher session IDs."""
    return web.json_response({
        "publishers": [
            {"publisher_id": p["publisher_id"],
             "has_audio": p["audio"] is not None,
             "has_video": p["video"] is not None}
            for p in published_tracks
        ]
    })


async def handle_disconnect(request: web.Request) -> web.Response:
    """Cleanly close a session."""
    params = await request.json()
    session_id = params.get("session_id")
    await _cleanup(session_id)
    return web.json_response({"ok": True})


async def _cleanup(session_id: str):
    pc = peer_connections.pop(session_id, None)
    if pc:
        await pc.close()
    # Remove from published tracks if this was a publisher
    global published_tracks
    published_tracks = [p for p in published_tracks if p["publisher_id"] != session_id]
    ice_candidates.pop(session_id, None)
    logger.info(f"[{session_id}] Cleaned up session")


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

async def on_shutdown(app):
    logger.info("Shutting down — closing all peer connections")
    coros = [pc.close() for pc in peer_connections.values()]
    await asyncio.gather(*coros)
    peer_connections.clear()


def create_app() -> web.Application:
    app = web.Application()
    app.on_shutdown.append(on_shutdown)

    # CORS so a browser-based client on a different origin can connect
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods=["GET", "POST", "OPTIONS"],
        )
    })

    routes = [
        app.router.add_post("/publish", handle_publish),
        app.router.add_post("/subscribe", handle_subscribe),
        app.router.add_post("/subscribe/answer", handle_subscribe_answer),
        app.router.add_post("/ice-candidate", handle_ice_candidate),
        app.router.add_get("/ice-candidates/{session_id}", handle_get_ice_candidates),
        app.router.add_get("/publishers", handle_list_publishers),
        app.router.add_post("/disconnect", handle_disconnect),
    ]
    for route in routes:
        cors.add(route)

    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="WebRTC SFU Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logger.info(f"Starting SFU server on {args.host}:{args.port}")
    web.run_app(create_app(), host=args.host, port=args.port)
