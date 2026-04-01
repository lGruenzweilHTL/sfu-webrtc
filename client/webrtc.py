import asyncio
import json
import logging
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from sinks import VideoSink, AudioSink
from aiohttp import WSMsgType

logger = logging.getLogger("webrtc-client")

async def do_publish(sfu, media, no_video=False, no_audio=False):
    pc = RTCPeerConnection()
    if not no_video and media.video:
        pc.addTrack(media.video)
    if not no_audio and media.audio:
        pc.addTrack(media.audio)

    @pc.on("connectionstatechange")
    async def _():
        logger.info(f"[publish] {pc.connectionState}")

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    
    # Wait for local ICE candidates to finish gathering before sending the SDP
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.1)

    resp = await sfu.publish(pc.localDescription.sdp)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=resp["sdp"], type=resp["type"]))
    logger.info(f"Published — session {resp['session_id']}")
    return pc, resp["session_id"]

async def do_subscribe(sfu, video_queue, exclude=None):
    ws = await sfu.connect_websocket()
    await ws.send_json({"type": "subscribe", "exclude_publisher": exclude})
    
    pc = RTCPeerConnection()
    sid = None
    video_sinks, audio_sinks = [], []
    counters = {"v": 0, "a": 0}
    negotiation_lock = asyncio.Lock()
    managed_track_ids = set()

    @pc.on("track")
    async def on_track(track):
        if track.id in managed_track_ids:
            return
        managed_track_ids.add(track.id)

        idx = counters[track.kind[0]]
        counters[track.kind[0]] += 1
        label = f"remote_{idx}"
        if track.kind == "video":
            sink = VideoSink(label, video_queue)
            sink.add_track(track)
            await sink.start()
            video_sinks.append(sink)
        else:
            sink = AudioSink(label)
            sink.add_track(track)
            await sink.start()
            audio_sinks.append(sink)
        logger.info(f"[subscribe] {track.kind} → {label} (ID: {track.id})")

    @pc.on("connectionstatechange")
    async def _():
        logger.info(f"[subscribe] {pc.connectionState}")

    @pc.on("icecandidate")
    async def on_ice(candidate):
        if candidate and sid and not ws.closed:
            try:
                await ws.send_json({
                    "type": "ice_candidate",
                    "session_id": sid,
                    "candidate": {
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdpMid,
                        "sdpMLineIndex": candidate.sdpMLineIndex,
                    }
                })
            except Exception:
                pass

    async def handle_messages():
        nonlocal sid
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                msg_type = data.get("type")
                
                if msg_type == "offer":
                    async with negotiation_lock:
                        is_renegotiation = sid is not None
                        if not sid:
                            sid = data.get("session_id")
                            logger.info(f"Subscribed — session {sid}")

                        try:
                            offer = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
                            await pc.setRemoteDescription(offer)
                            
                            answer = await pc.createAnswer()
                            await pc.setLocalDescription(answer)
                            
                            if not ws.closed:
                                await ws.send_json({
                                    "type": "answer_renegotiation" if is_renegotiation else "answer",
                                    "session_id": sid,
                                    "sdp": pc.localDescription.sdp,
                                    "type": pc.localDescription.type
                                })
                        except Exception as e:
                            logger.error(f"Signaling error: {e}")
                elif msg_type == "ice_candidate":
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

    asyncio.ensure_future(handle_messages())

    while not sid:
        await asyncio.sleep(0.1)

    return pc, sid, video_sinks, audio_sinks, ws
