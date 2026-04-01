import asyncio
import logging
import uuid
from typing import Dict, List, Optional, Callable, Any, Set
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaRelay

logger = logging.getLogger("sfu-server")

class LatencyControlTrack(MediaStreamTrack):
    """
    Consumes frames from the source track as fast as possible to prevent aiortc's
    internal JitterBuffers/Queues from blowing up memory. If the sink (network or UI)
    is too slow, it drops the oldest frames to maintain real-time latency.
    """
    def __init__(self, track: MediaStreamTrack):
        super().__init__()
        self.kind = track.kind
        self._track = track
        # Audio frames are tiny and frequent (typically 20ms). 50 frames = 1 second buffer.
        # Video frames are huge. 2 frames is plenty.
        max_size = 50 if self.kind == "audio" else 2
        self._queue = asyncio.Queue(maxsize=max_size)
        self._task = asyncio.ensure_future(self._run())

    async def _run(self):
        while True:
            try:
                frame = await self._track.recv()
                if self._queue.full():
                    try:
                        self._queue.get_nowait()  # Drop the oldest frame
                    except asyncio.QueueEmpty:
                        pass
                self._queue.put_nowait(frame)
            except Exception:
                break

    async def recv(self):
        if self.readyState != "live":
            raise Exception("Track is closed")
        return await self._queue.get()

    def stop(self):
        super().stop()
        if self._task:
            self._task.cancel()
        if hasattr(self._track, "stop"):
            self._track.stop()

class SFUManager:
    def __init__(self):
        self.relay = MediaRelay()
        self.peer_connections: Dict[str, RTCPeerConnection] = {}
        self.published_tracks: List[Dict] = []
        self.ice_candidates: Dict[str, List[dict]] = {}
        self.subscribers: Dict[str, Dict[str, Any]] = {}
        # Keep references to the background tasks that drain the server's JitterBuffers
        self._drain_tasks: Set[asyncio.Task] = set()

    def create_session_id(self) -> str:
        return str(uuid.uuid4())

    def get_published_source_tracks(self, exclude_publisher: Optional[str] = None):
        """Returns the original source tracks from publishers."""
        result = []
        for pub in self.published_tracks:
            if exclude_publisher and pub["publisher_id"] == exclude_publisher:
                continue
            entry = {"publisher_id": pub["publisher_id"]}
            if pub.get("audio"):
                entry["audio"] = pub["audio"]
            if pub.get("video"):
                entry["video"] = pub["video"]
            result.append(entry)
        return result

    async def add_publisher(self, sdp: str, sdp_type: str):
        session_id = self.create_session_id()
        pc = RTCPeerConnection()
        self.peer_connections[session_id] = pc
        self.ice_candidates[session_id] = []

        pub_entry = {"publisher_id": session_id, "audio": None, "video": None}
        self.published_tracks.append(pub_entry)

        @pc.on("track")
        async def on_track(track: MediaStreamTrack):
            logger.info(f"[{session_id}] Publisher sent {track.kind} track: {track.id}")
            if track.kind == "audio":
                pub_entry["audio"] = track
            elif track.kind == "video":
                pub_entry["video"] = track
                
            # Crucial Fix: Create a silent proxy track that instantly drains 
            # the JitterBuffer even if no subscribers are in the room.
            dummy_proxy = self.relay.subscribe(track)
            
            async def _drain(t):
                try:
                    while True:
                        await t.recv()
                except Exception:
                    pass
                    
            task = asyncio.create_task(_drain(dummy_proxy))
            self._drain_tasks.add(task)
            task.add_done_callback(self._drain_tasks.discard)
            
            # Debounce/Notify after a small delay to catch multiple tracks (audio+video)
            asyncio.create_task(self._notify_subscribers_debounced())

        @pc.on("icecandidate")
        async def on_ice(candidate):
            if candidate:
                cand_dict = {
                    "candidate": candidate.candidate,
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                }
                self.ice_candidates[session_id].append(cand_dict)
                # Send to subscriber via websocket if available
                sub = self.subscribers.get(session_id)
                if sub:
                    try:
                        await sub["callback"]({
                            "type": "ice_candidate",
                            "session_id": session_id,
                            "candidate": cand_dict
                        })
                    except Exception:
                        pass

        @pc.on("connectionstatechange")
        async def on_state():
            if pc.connectionState in ("failed", "closed"):
                await self.cleanup(session_id)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        while pc.iceGatheringState != "complete":
            await asyncio.sleep(0.1)

        return {
            "session_id": session_id,
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    async def add_subscriber(self, notify_callback: Callable, exclude_publisher: Optional[str] = None):
        session_id = self.create_session_id()
        pc = RTCPeerConnection()
        self.peer_connections[session_id] = pc
        self.ice_candidates[session_id] = []
        self.subscribers[session_id] = {
            "callback": notify_callback,
            "exclude": exclude_publisher,
            "pc": pc,
            "negotiating": False,
            "added_track_ids": set() # Track IDs already added to this PC
        }

        await self._update_subscriber_tracks(session_id)

        @pc.on("icecandidate")
        async def on_ice(candidate):
            if candidate:
                cand_dict = {
                    "candidate": candidate.candidate,
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                }
                self.ice_candidates[session_id].append(cand_dict)
                # Send to subscriber via websocket if available
                sub = self.subscribers.get(session_id)
                if sub:
                    try:
                        await sub["callback"]({
                            "type": "ice_candidate",
                            "session_id": session_id,
                            "candidate": cand_dict
                        })
                    except Exception:
                        pass

        @pc.on("connectionstatechange")
        async def on_state():
            if pc.connectionState in ("failed", "closed"):
                await self.cleanup(session_id)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        return {
            "session_id": session_id,
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    async def _update_subscriber_tracks(self, session_id: str):
        sub = self.subscribers.get(session_id)
        if not sub: return False
        
        pc = sub["pc"]
        exclude = sub["exclude"]
        added_ids = sub["added_track_ids"]
        
        sources = self.get_published_source_tracks(exclude_publisher=exclude)
        
        added = False
        for entry in sources:
            for kind in ["audio", "video"]:
                track = entry.get(kind)
                if track and track.id not in added_ids:
                    # Create a proxy ONLY when adding to a new subscriber
                    proxy = self.relay.subscribe(track)
                    # Wrap it to prevent slow subscribers from bloat-leaking the server RAM
                    latency_controlled_proxy = LatencyControlTrack(proxy)
                    pc.addTrack(latency_controlled_proxy)
                    added_ids.add(track.id)
                    added = True
        return added

    async def _notify_subscribers_debounced(self):
        # Wait a bit to group audio/video track updates
        await asyncio.sleep(0.5)
        await self._notify_subscribers()

    async def _notify_subscribers(self):
        for session_id, sub in list(self.subscribers.items()):
            if sub["negotiating"] or sub["pc"].signalingState != "stable":
                continue
                
            if await self._update_subscriber_tracks(session_id):
                try:
                    sub["negotiating"] = True
                    pc = sub["pc"]
                    offer = await pc.createOffer()
                    await pc.setLocalDescription(offer)
                    await sub["callback"]({
                        "type": "offer",
                        "sdp": pc.localDescription.sdp,
                        "session_id": session_id
                    })
                except Exception:
                    sub["negotiating"] = False

    async def set_answer(self, session_id: str, sdp: str, sdp_type: str):
        pc = self.peer_connections.get(session_id)
        if not pc: return False
        
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
            if session_id in self.subscribers:
                self.subscribers[session_id]["negotiating"] = False
                # Re-check for any tracks added during negotiation
                asyncio.create_task(self._notify_subscribers())
            return True
        except Exception:
            if session_id in self.subscribers:
                self.subscribers[session_id]["negotiating"] = False
            return False

    async def cleanup(self, session_id: str):
        pc = self.peer_connections.pop(session_id, None)
        if pc:
            await pc.close()
        self.published_tracks = [p for p in self.published_tracks if p["publisher_id"] != session_id]
        self.ice_candidates.pop(session_id, None)
        self.subscribers.pop(session_id, None)
        logger.info(f"[{session_id}] Cleaned up session")

    async def close_all(self):
        coros = [pc.close() for pc in self.peer_connections.values()]
        await asyncio.gather(*coros)
        self.peer_connections.clear()
        self.published_tracks.clear()
        self.ice_candidates.clear()
        self.subscribers.clear()
        for task in self._drain_tasks:
            task.cancel()
        self._drain_tasks.clear()
