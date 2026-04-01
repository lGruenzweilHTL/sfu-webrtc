import argparse
import asyncio
import logging
import signal
import threading
import multiprocessing
import aiohttp

from aiortc.contrib.media import MediaRelay
from signaling import SFUClient
from media import open_camera_and_mic
from sinks import LocalPreviewSink
from gui import render_loop, CV2_AVAILABLE
from webrtc import do_publish, do_subscribe

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("webrtc-client")

async def discard_track(track):
    """Continuously consume frames from an ignored track to prevent memory bloat."""
    try:
        while True:
            await track.recv()
    except Exception:
        pass

async def run_async(args, video_queue, mute_state, stop_event):
    url = f"http://{args.host}:{args.port}"
    logger.info(f"SFU: {url}")

    pub_pc = sub_pc = None
    pub_sid = sub_sid = None
    sub_ws = None
    preview = None
    all_sinks = []

    async with aiohttp.ClientSession() as http:
        sfu = SFUClient(url, http)
        media = None

        if args.mode in ("publish", "both"):
            try:
                media = open_camera_and_mic(args.camera_index, args.audio_device, args.video_source)
            except Exception as e:
                logger.error(f"Cannot open media: {e}")
                stop_event.set()
                return

            relay = MediaRelay()
            video_track = None
            audio_track = None

            # Only subscribe to the relay if we actually need the track.
            # If we don't need it (e.g. --no-video), we MUST explicitly consume the base
            # track via discard_track, otherwise MediaPlayer pushes to an infinite queue in RAM.
            if media.video:
                if args.no_video:
                    asyncio.ensure_future(discard_track(media.video))
                else:
                    video_track = relay.subscribe(media.video)
                    
            if media.audio:
                if args.no_audio:
                    asyncio.ensure_future(discard_track(media.audio))
                else:
                    audio_track = relay.subscribe(media.audio)

            # Create a mock media object to pass to do_publish
            class _MockMedia:
                def __init__(self, v, a):
                    self.video = v
                    self.audio = a
            
            pub_pc, pub_sid = await do_publish(sfu, _MockMedia(video_track, audio_track), args.no_video, args.no_audio)

            if video_track and media.video and not args.no_gui:
                preview_track = relay.subscribe(media.video)
                preview = LocalPreviewSink(preview_track, video_queue)
                await preview.start()

        if args.mode in ("subscribe", "both"):
            sub_pc, sub_sid, vsinks, asinks, sub_ws = await do_subscribe(
                sfu, video_queue, exclude=pub_sid)
            all_sinks = vsinks + asinks

        logger.info("Live — Q in window or Ctrl+C to quit")

        try:
            loop = asyncio.get_event_loop()
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        except (NotImplementedError, ValueError):
            pass

        while not stop_event.is_set():
            await asyncio.sleep(0.1)

        logger.info("Cleaning up...")
        if preview:
            await preview.stop()
        for s in all_sinks:
            await s.stop()
        if pub_sid:
            await sfu.disconnect(pub_sid)
        if sub_ws:
            await sub_ws.close()
        for pc in (pub_pc, sub_pc):
            if pc:
                await pc.close()
        if media:
            if media.video:
                media.video.stop()
            if media.audio:
                media.audio.stop()

def main():
    parser = argparse.ArgumentParser(description="WebRTC SFU client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--mode", choices=["publish", "subscribe", "both"], default="both")
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument("--audio-device", default=None)
    parser.add_argument("--video-source", default=None)
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--no-gui", action="store_true")
    args = parser.parse_args()

    if args.no_gui or not CV2_AVAILABLE:
        args.no_gui = True

    # Multi-process communication primitives
    video_queue = multiprocessing.Queue(maxsize=30)
    stop_event = multiprocessing.Event()
    
    # Manager for sharing simple state across processes
    manager = multiprocessing.Manager()
    mute_state = manager.dict({"mic": False, "cam": False})

    # Start WebRTC thread in the main process
    def _webrtc_thread():
        asyncio.run(run_async(args, video_queue, mute_state, stop_event))

    webrtc_t = threading.Thread(target=_webrtc_thread, daemon=True)
    webrtc_t.start()

    if args.no_gui:
        try:
            while webrtc_t.is_alive():
                webrtc_t.join(1)
        except KeyboardInterrupt:
            stop_event.set()
    else:
        # Launch OpenCV in its own OS process to bypass the GIL
        gui_p = multiprocessing.Process(
            target=render_loop,
            args=(video_queue, stop_event, mute_state)
        )
        gui_p.start()
        
        try:
            gui_p.join()
        except KeyboardInterrupt:
            stop_event.set()
        
        if gui_p.is_alive():
            gui_p.terminate()

    stop_event.set()
    webrtc_t.join(timeout=5)

if __name__ == "__main__":
    main()
