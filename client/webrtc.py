import logging
from aiortc import RTCPeerConnection, RTCSessionDescription
from sinks import VideoSink, AudioSink

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
    resp = await sfu.subscribe(exclude=exclude)
    sid = resp["session_id"]
    pc = RTCPeerConnection()

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

    await pc.setRemoteDescription(RTCSessionDescription(sdp=resp["sdp"], type=resp["type"]))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    await sfu.send_answer(sid, pc.localDescription.sdp, pc.localDescription.type)
    logger.info(f"Subscribed — session {sid}")
    return pc, sid, video_sinks, audio_sinks
