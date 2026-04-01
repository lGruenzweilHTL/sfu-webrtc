import logging
import numpy as np

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

logger = logging.getLogger("webrtc-client")
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

        if not remote:
            canvas = np.full((H, W, 3), (20, 20, 20), dtype=np.uint8)
            _centered(canvas, "Waiting for participants...", W // 2, H // 2)
        else:
            canvas = _grid(remote, W, H)

        # Local preview overlay
        lf = local_frame_store.get("local")
        if lf is not None:
            thumb_w = int(W * 0.22)
            thumb_h = int(thumb_w * lf.shape[0] / max(lf.shape[1], 1))
            thumb = cv2.resize(lf, (thumb_w, thumb_h))
            if mute_state.get("cam"):
                thumb[:] = (25, 25, 25)
                _centered(thumb, "CAM OFF", thumb_w // 2, thumb_h // 2, scale=0.5)
            
            thumb = cv2.copyMakeBorder(thumb, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=(80, 80, 200))
            pad = 12
            y1, x1 = H - thumb.shape[0] - pad, pad
            y2, x2 = y1 + thumb.shape[0], x1 + thumb.shape[1]
            if 0 <= y1 and y2 <= H and 0 <= x1 and x2 <= W:
                roi = canvas[y1:y2, x1:x2]
                canvas[y1:y2, x1:x2] = cv2.addWeighted(thumb, 0.88, roi, 0.12, 0)
            cv2.putText(canvas, "YOU", (x1 + 6, y1 + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1, cv2.LINE_AA)

        # Mute indicators
        if mute_state.get("mic"):
            cv2.putText(canvas, "MIC MUTED", (W - 160, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (80, 80, 255), 2, cv2.LINE_AA)
        if mute_state.get("cam"):
            cv2.putText(canvas, "CAM OFF", (W - 140, 58),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (80, 80, 255), 2, cv2.LINE_AA)

        # Hint bar
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
