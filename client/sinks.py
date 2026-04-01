import asyncio
import logging
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from functools import partial

try:
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except (OSError, ImportError):
    AUDIO_AVAILABLE = False

logger = logging.getLogger("webrtc-client")
# Thread pool used only for track.recv() / decoding if needed
executor = ThreadPoolExecutor(max_workers=8)

class VideoSink:
    def __init__(self, label: str, video_queue):
        self.label = label
        self._video_queue = video_queue
        self._task = None
        self._track = None
        self._busy = False

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

    async def _run(self):
        loop = asyncio.get_event_loop()
        while True:
            try:
                frame = await self._track.recv()
                
                # Maintain low latency by skipping frames if previous processing is still ongoing
                if self._busy:
                    continue
                    
                self._busy = True
                try:
                    # Offload the CPU-bound BGR conversion to the thread pool
                    bgr_frame = await loop.run_in_executor(
                        executor, partial(frame.to_ndarray, format="bgr24")
                    )
                    # Push directly to the multiprocessing queue for the GUI process
                    # block=False + full exception handling drops frame if queue is congested
                    try:
                        self._video_queue.put_nowait((self.label, bgr_frame))
                    except Exception:
                        pass
                finally:
                    self._busy = False
                    
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
            return
        try:
            self._stream = sd.OutputStream(
                samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
                dtype="float32",
            )
            self._stream.start()
            self._task = asyncio.ensure_future(self._run())
        except Exception:
            pass

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
    def __init__(self, track, video_queue):
        self._track = track
        self._video_queue = video_queue
        self._task = None
        self._busy = False

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
        loop = asyncio.get_event_loop()
        while True:
            try:
                frame = await self._track.recv()
                
                if self._busy:
                    continue
                    
                self._busy = True
                try:
                    bgr_frame = await loop.run_in_executor(
                        executor, partial(frame.to_ndarray, format="bgr24")
                    )
                    try:
                        self._video_queue.put_nowait(("local", bgr_frame))
                    except Exception:
                        pass
                finally:
                    self._busy = False
            except Exception:
                break
