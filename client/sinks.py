import asyncio
import logging
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from functools import partial

try:
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except (OSError, ImportError):
    sd = None
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
                
                def _process_video(f):
                    try:
                        bgr_frame = f.to_ndarray(format="bgr24")
                        self._video_queue.put_nowait((self.label, bgr_frame))
                    except Exception:
                        pass
                    finally:
                        self._busy = False
                        
                # Run in background to keep the async loop free to recv() more frames
                loop.run_in_executor(executor, _process_video, frame)
                    
            except Exception:
                break

class AudioSink:
    CHANNELS = 1

    def __init__(self, label: str):
        self.label = label
        self._task = None
        self._track = None
        self._stream = None

    def add_track(self, track):
        self._track = track

    async def start(self):
        if not self._track:
            return
        self._task = asyncio.ensure_future(self._run())

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
        if not AUDIO_AVAILABLE:
            # Consume frames silently to prevent unhandled track JitterBuffer warnings
            while True:
                try:
                    await self._track.recv()
                except Exception:
                    break
            return
            
        import queue
        import threading
        
        q = queue.Queue(maxsize=100)
        
        def _audio_thread():
            while True:
                item = q.get()
                if item is None:
                    break
                    
                mono, sample_rate = item
                
                if self._stream is None:
                    try:
                        import sounddevice as sdev
                        self._stream = sdev.OutputStream(
                            samplerate=sample_rate,
                            channels=self.CHANNELS,
                            dtype="float32",
                        )
                        self._stream.start()
                    except Exception as e:
                        logger.error(f"Failed to start audio stream: {e}")
                        break

                if self._stream and self._stream.active:
                    try:
                        self._stream.write(mono)
                    except Exception:
                        pass
                        
        t = threading.Thread(target=_audio_thread, daemon=True)
        t.start()
        
        try:
            while True:
                frame = await self._track.recv()
                audio = frame.to_ndarray()
                mono = audio.mean(axis=0).astype(np.float32)
                if frame.format.name != "fltp":
                    mono = mono / 32768.0
                
                try:
                    q.put_nowait((mono, frame.sample_rate))
                except queue.Full:
                    # If queue is full, drop the oldest frame to make room for the newest.
                    # This prevents memory leaks (by never blocking recv) and minimizes 
                    # audio desync by skipping the oldest data instead of blocking.
                    try:
                        q.get_nowait()
                        q.put_nowait((mono, frame.sample_rate))
                    except (queue.Empty, queue.Full):
                        pass
        except Exception:
            pass
        finally:
            q.put(None)
            t.join(timeout=1.0)

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
                
                def _process_local(f):
                    try:
                        bgr_frame = f.to_ndarray(format="bgr24")
                        self._video_queue.put_nowait(("local", bgr_frame))
                    except Exception:
                        pass
                    finally:
                        self._busy = False
                        
                loop.run_in_executor(executor, _process_local, frame)
            except Exception:
                break
