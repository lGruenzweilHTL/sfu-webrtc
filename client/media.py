import logging
import platform
from aiortc.contrib.media import MediaPlayer
from aiortc import MediaStreamTrack
import asyncio

class LatencyControlTrack(MediaStreamTrack):
    """
    Consumes frames from the source track as fast as possible to prevent aiortc's
    internal Queues from blowing up memory. Drops the oldest frames if network/UI is slow.
    """
    def __init__(self, track: MediaStreamTrack):
        super().__init__()
        self.kind = track.kind
        self._track = track
        max_size = 50 if self.kind == "audio" else 2
        self._queue = asyncio.Queue(maxsize=max_size)
        self._task = asyncio.ensure_future(self._run())

    async def _run(self):
        while True:
            try:
                frame = await self._track.recv()
                if self._queue.full():
                    try:
                        self._queue.get_nowait()
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

logger = logging.getLogger("webrtc-client")

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

def open_camera_and_mic(camera_index=0, audio_device=None, video_source=None):
    if video_source:
        logger.info(f"Opening video file: {video_source}")
        return MediaPlayer(video_source, loop=True)

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
