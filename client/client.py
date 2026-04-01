"""
WebRTC Python Client — with live video display and audio playback
=================================================================
This client publishes your camera/mic to the SFU server and shows
all remote participants in a real-time OpenCV window.

Layout:
  ┌─────────────────────────────────┐
  │  Remote 1  │  Remote 2  │  ...  │  ← full-size remote feeds
  │            │            │       │
  └─────────────────────────────────┘
  │ [YOU] │  ← small local preview overlaid bottom-left

Controls (click the video window first):
  Q / Escape  — quit
  M           — toggle microphone mute
  V           — toggle camera (sends black frames)

Usage:
    python client.py                             # webcam + mic, show video
    python client.py --video-source test.mp4     # use a file
    python client.py --mode subscribe            # watch only
    python client.py --no-gui                    # headless / server mode
"""

import argparse
import asyncio
import logging
import signal
import threading
import numpy as np
import aiohttp
import av
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaBlackhole
from typing import Optional

try:
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except OSError:
    AUDIO_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("webrtc-client")


# ============================================================================
# Live video sink
# ============================================================================

class VideoSink:
    def __init__(self, label: str, frame_store: dict):
        self.label = label
        self._frame_store = frame_store
        self._task = None
        self._track = None

    def addTrack(self, track):
        self._track = track

    async def start(self):
        if self._track:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._frame_store.pop(self.label, None)

    async def _run(self):
        while True:
            try:
                frame = await self._track.recv()
                self._frame_store[self.label] = frame.to_ndarray(format="bgr24")
            except Exception:
                break


# ============================================================================
# Live audio sink
# ============================================================================

class AudioSink:
    SAMPLE_RATE = 48000
    CHANNELS = 1

    def __init__(self, label: str):
        self.label = label
        self._task = None
        self._track = None
        self._stream = None

    def addTrack(self, track):
        self._track = track

    async def start(self):
        if not AUDIO_AVAILABLE or not self._track:
            if not AUDIO_AVAILABLE:
                logger.warning("sounddevice unavailable — audio playback disabled. "
                               "Install portaudio: sudo apt install libportaudio2")
            return
        try:
            self._stream = sd.OutputStream(
                samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
                dtype="float32",
            )
            self._stream.start()
            self._task = asyncio.ensure_future(self._run())
        except Exception as e:
            logger.warning(f"Audio output error: {e}")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._stream:
            self._stream.stop()
            self._stream.close()

    async def _run(self):
        while True:
            try:
                frame = await self._track.recv()
                audio = frame.to_ndarray()
                mono = audio.mean(axis=0).astype(np.float32)
                if frame.format.name != "fltp":
                    mono = mono / 32768.0
                if self._stream and self._stream.active:
                    self._stream.write(mono)
            except Exception:
                break


# ============================================================================
# Media device helpers
# ============================================================================

def open_camera_and_mic(camera_index=0, audio_device=None, video_source=None):
    if video_source:
        logger.info(f"Opening video file: {video_source}")
        return MediaPlayer(video_source, loop=True)

    import platform
    os_name = platform.system()

    if os_name == "Linux":
        video_dev = f"/dev/video{camera_index}"
        audio_dev = audio_device or "default"
        logger.info(f"Linux: video={video_dev}, audio={audio_dev}")
        return _LinuxMediaPlayer(video_dev, audio_dev)
    elif os_name == "Darwin":
        device = f"{camera_index}:0"
        logger.info(f"macOS AVFoundation: {device}")
        return MediaPlayer(device, format="avfoundation",
                           options={"framerate": "30", "video_size": "1280x720"})
    elif os_name == "Windows":
        device = f"video={camera_index}:audio={audio_device or 'default'}"
        logger.info(f"Windows DirectShow: {device}")
        return MediaPlayer(device, format="dshow")
    else:
        raise RuntimeError(f"Unsupported platform: {os_name}")


class _LinuxMediaPlayer:
    def __init__(self, video_dev, audio_dev):
        self._vp = MediaPlayer(video_dev, format="v4l2",
                               options={"framerate": "30", "video_size": "1280x720"})
        self._ap = MediaPlayer(audio_dev, format="alsa",
                               options={"channels": "1", "sample_rate": "48000"})

    @property
    def video(self): return self._vp.video
    @property
    def audio(self): return self._ap.audio


# ============================================================================
# Local camera preview — reads frames from the local video track
# ============================================================================

class LocalPreviewSink:
    def __init__(self, track, local_frame_store: dict):
        self._track = track
        self._store = local_frame_store
        self._task = None

    async def start(self):
        self._task = asyncio.ensure_future(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        while True:
            try:
                frame = await self._track.recv()
                self._store["local"] = frame.to_ndarray(format="bgr24")
            except Exception:
                break


# ============================================================================
# SFU signaling
# ============================================================================

class SFUClient:
    def __init__(self, url, session):
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


# ============================================================================
# Publish / Subscribe
# ============================================================================

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
            sink.addTrack(track)
            await sink.start()
            video_sinks.append(sink)
        else:
            sink = AudioSink(label)
            sink.addTrack(track)
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


# ============================================================================
# OpenCV render loop (main thread)
# ============================================================================

WINDOW = "WebRTC Call"

def render_loop(frame_store, local_frame_store, stop_event, mute_state):
    if not CV2_AVAILABLE:
        logger.warning("OpenCV not available — headless. pip install opencv-python")
        stop_event.wait()
        return

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW, 1280, 720)
    W, H = 1280, 720

    while not stop_event.is_set():
        remote = dict(frame_store)

        # ---- build canvas ----
        if not remote:
            canvas = np.full((H, W, 3), (20, 20, 20), dtype=np.uint8)
            _centered(canvas, "Waiting for participants...", W // 2, H // 2)
        else:
            canvas = _grid(remote, W, H)

        # ---- local preview overlay ----
        lf = local_frame_store.get("local")
        if lf is not None:
            thumb_w = int(W * 0.22)
            thumb_h = int(thumb_w * lf.shape[0] / max(lf.shape[1], 1))
            thumb = cv2.resize(lf, (thumb_w, thumb_h))
            if mute_state.get("cam"):
                thumb[:] = (25, 25, 25)
                _centered(thumb, "CAM OFF", thumb_w // 2, thumb_h // 2, scale=0.5)
            # border
            thumb = cv2.copyMakeBorder(thumb, 2, 2, 2, 2, cv2.BORDER_CONSTANT,
                                       value=(80, 80, 200))
            pad = 12
            y1, x1 = H - thumb.shape[0] - pad, pad
            y2, x2 = y1 + thumb.shape[0], x1 + thumb.shape[1]
            if 0 <= y1 and y2 <= H and 0 <= x1 and x2 <= W:
                roi = canvas[y1:y2, x1:x2]
                canvas[y1:y2, x1:x2] = cv2.addWeighted(thumb, 0.88, roi, 0.12, 0)
            cv2.putText(canvas, "YOU", (x1 + 6, y1 + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1, cv2.LINE_AA)

        # ---- mute indicators ----
        if mute_state.get("mic"):
            cv2.putText(canvas, "MIC MUTED", (W - 160, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (80, 80, 255), 2, cv2.LINE_AA)
        if mute_state.get("cam"):
            cv2.putText(canvas, "CAM OFF", (W - 140, 58),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (80, 80, 255), 2, cv2.LINE_AA)

        # ---- hint bar ----
        cv2.putText(canvas, "Q/Esc=quit   M=mute mic   V=mute cam",
                    (8, H - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (100, 100, 100), 1, cv2.LINE_AA)

        cv2.imshow(WINDOW, canvas)
        key = cv2.waitKey(33) & 0xFF
        if key in (ord("q"), 27):
            stop_event.set()
            break
        elif key == ord("m"):
            mute_state["mic"] = not mute_state.get("mic", False)
            logger.info("Mic %s", "muted" if mute_state["mic"] else "unmuted")
        elif key == ord("v"):
            mute_state["cam"] = not mute_state.get("cam", False)
            logger.info("Camera %s", "off" if mute_state["cam"] else "on")

    cv2.destroyAllWindows()


def _grid(frames, W, H):
    n = len(frames)
    cols = max(1, int(np.ceil(np.sqrt(n))))
    rows = max(1, int(np.ceil(n / cols)))
    cell_w, cell_h = W // cols, H // rows
    label_h = 26
    canvas = np.zeros((H, W, 3), dtype=np.uint8)

    for i, (label, frame) in enumerate(frames.items()):
        r, c = divmod(i, cols)
        x1, y1 = c * cell_w, r * cell_h
        x2, y2 = x1 + cell_w, y1 + cell_h
        fh, fw = frame.shape[:2]
        scale = min(cell_w / max(fw, 1), (cell_h - label_h) / max(fh, 1))
        nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
        resized = cv2.resize(frame, (nw, nh))
        ox = x1 + (cell_w - nw) // 2
        oy = y1 + (cell_h - label_h - nh) // 2
        canvas[oy:oy + nh, ox:ox + nw] = resized
        cv2.rectangle(canvas, (x1, y2 - label_h), (x2, y2), (30, 30, 30), -1)
        cv2.putText(canvas, label, (x1 + 8, y2 - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (200, 200, 200), 1, cv2.LINE_AA)
        cv2.rectangle(canvas, (x1, y1), (x2 - 1, y2 - 1), (60, 180, 60), 1)
    return canvas


def _centered(img, text, cx, cy, scale=0.75, color=(150, 150, 150)):
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    cv2.putText(img, text, (cx - tw // 2, cy + th // 2),
                cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


# ============================================================================
# Async main
# ============================================================================

async def run_async(args, frame_store, local_frame_store, mute_state, stop_event):
    url = f"http://{args.host}:{args.port}"
    logger.info(f"SFU: {url}")

    pub_pc = sub_pc = None
    pub_sid = sub_sid = None
    preview = None
    all_sinks = []

    async with aiohttp.ClientSession() as http:
        sfu = SFUClient(url, http)

        if args.mode in ("publish", "both"):
            try:
                media = open_camera_and_mic(args.camera_index, args.audio_device, args.video_source)
            except Exception as e:
                logger.error(f"Cannot open media: {e}")
                stop_event.set()
                return

            pub_pc, pub_sid = await do_publish(sfu, media, args.no_video, args.no_audio)

            if media.video and not args.no_video and not args.no_gui:
                preview = LocalPreviewSink(media.video, local_frame_store)
                await preview.start()

        if args.mode in ("subscribe", "both"):
            sub_pc, sub_sid, vsinks, asinks = await do_subscribe(
                sfu, frame_store, exclude=pub_sid)
            all_sinks = vsinks + asinks

        logger.info("Live — Q in window or Ctrl+C to quit")

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)

        while not stop_event.is_set():
            await asyncio.sleep(0.1)

        logger.info("Cleaning up...")
        if preview:
            await preview.stop()
        for s in all_sinks:
            await s.stop()
        if pub_sid:
            await sfu.disconnect(pub_sid)
        if sub_sid:
            await sfu.disconnect(sub_sid)
        for pc in (pub_pc, sub_pc):
            if pc:
                await pc.close()
        logger.info("Done.")


# ============================================================================
# Entry point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="WebRTC SFU client with live video",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--mode", choices=["publish", "subscribe", "both"], default="both")
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument("--audio-device", default=None)
    parser.add_argument("--video-source", default=None,
                        help="Use a video file instead of webcam")
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--no-gui", action="store_true",
                        help="Headless mode — no OpenCV window")
    args = parser.parse_args()

    if args.no_gui or not CV2_AVAILABLE:
        args.no_gui = True
        if not CV2_AVAILABLE:
            logger.warning("OpenCV missing — headless. pip install opencv-python")

    frame_store: dict = {}
    local_frame_store: dict = {}
    mute_state: dict = {"mic": False, "cam": False}
    stop_event = threading.Event()

    def _bg():
        asyncio.run(run_async(args, frame_store, local_frame_store, mute_state, stop_event))

    t = threading.Thread(target=_bg, daemon=True)
    t.start()

    if args.no_gui:
        logger.info("Headless — Ctrl+C to stop")
        try:
            t.join()
        except KeyboardInterrupt:
            stop_event.set()
            t.join(timeout=5)
    else:
        # OpenCV imshow MUST run on the main thread
        render_loop(frame_store, local_frame_store, stop_event, mute_state)
        t.join(timeout=5)


if __name__ == "__main__":
    main()
