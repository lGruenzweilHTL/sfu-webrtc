import asyncio
import logging
import numpy as np

try:
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except (OSError, ImportError):
    AUDIO_AVAILABLE = False

logger = logging.getLogger("webrtc-client")

class VideoSink:
    def __init__(self, label: str, frame_store: dict):
        self.label = label
        self._frame_store = frame_store
        self._task = None
        self._track = None

    def add_track(self, track):
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

class AudioSink:
    SAMPLE_RATE = 48000
    CHANNELS = 1

    def __init__(self, label: str):
        self.label = label
        self._task = None
        self._track = None
        self._stream = None

    def add_track(self, track):
        self._track = track

    async def start(self):
        if not AUDIO_AVAILABLE or not self._track:
            if not AUDIO_AVAILABLE:
                logger.warning("sounddevice unavailable — audio playback disabled.")
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
