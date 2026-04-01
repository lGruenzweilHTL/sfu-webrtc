import asyncio
import json
import logging
from aiortc import RTCPeerConnection, RTCSessionDescription
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
    resp = await sfu.publish(pc.localDescription.sdp)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=resp["sdp"], type=resp["type"]))
    logger.info(f"Published — session {resp['session_id']}")
    return pc, resp["session_id"]

async def do_subscribe(sfu, frame_store, exclude=None):
    ws = await sfu.connect_websocket()
    await ws.send_json({"type": "subscribe", "exclude_publisher": exclude})
    
    pc = RTCPeerConnection()
    sid = None
    video_sinks, audio_sinks = [], []
    counters = {"v": 0, "a": 0}

    @pc.on("track")
    async def on_track(track):
        idx = counters[track.kind[0]]
        counters[track.kind[0]] += 1
        label = f"remote_{idx}"
        if track.kind == "video":
            sink = VideoSink(label, frame_store)
            sink.add_track(track)
            await sink.start()
            video_sinks.append(sink)
        else:
            sink = AudioSink(label)
            sink.add_track(track)
            await sink.start()
            audio_sinks.append(sink)
        logger.info(f"[subscribe] {track.kind} → {label}")

    @pc.on("connectionstatechange")
    async def _():
        logger.info(f"[subscribe] {pc.connectionState}")

    @pc.on("icecandidate")
    async def on_ice(candidate):
        if candidate and sid:
            await ws.send_json({
                "type": "ice_candidate",
                "session_id": sid,
                "candidate": {
                    "candidate": candidate.candidate,
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                }
            })

    async def handle_messages():
        nonlocal sid
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                msg_type = data.get("type")
                
                if msg_type == "offer":
                    is_renegotiation = sid is not None
                    if not sid:
                        sid = data.get("session_id")
                        logger.info(f"Subscribed — session {sid}")

                    offer = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
                    await pc.setRemoteDescription(offer)
                    
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)
                    
                    await ws.send_json({
                        "type": "answer_renegotiation" if is_renegotiation else "answer",
                        "session_id": sid,
                        "sdp": pc.localDescription.sdp,
                        "type": pc.localDescription.type
                    })
                else:
                    logger.warning(f"Unknown message type from server: {msg_type}")
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break

    asyncio.ensure_future(handle_messages())

    while not sid:
        await asyncio.sleep(0.1)

    return pc, sid, video_sinks, audio_sinks, ws
