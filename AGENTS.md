# AI Agent Onboarding Guide (AGENTS.md)

Welcome to the WebRTC SFU (Selective Forwarding Unit) project. This document is designed specifically to onboard AI coding agents, providing a deep dive into the architecture, technologies, and crucial context needed to safely and effectively modify the codebase.

## 1. Project Overview & Architecture

This project is a complete WebRTC SFU built in pure Python. It allows multiple clients to join a room, publish their audio and video tracks to a central server, and subscribe to all other participants' tracks.

Unlike a Peer-to-Peer (Mesh) architecture where every client connects to every other client (which scales poorly), this SFU architecture means each client uploads exactly once to the server, and the server duplicates and forwards the media packets to all subscribers.

**Core Stack:**
*   **WebRTC:** `aiortc` (handles ICE, DTLS, SRTP, and SDP negotiation)
*   **Networking:** `aiohttp` (for HTTP REST endpoints and WebSockets)
*   **Client UI/Media:** `opencv-python` (`cv2` for video rendering), `sounddevice` (for audio playback)
*   **Concurrency:** `asyncio` for networking, `multiprocessing` and `threading` for media processing to bypass the GIL.

---

## 2. Directory Structure & File Responsibilities

### Server (`server/`)
The server acts as the WebRTC Selective Forwarding Unit.
*   **`server.py`**: The main entry point. Initializes the `aiohttp` web server, configures CORS, registers HTTP/WebSocket routes, and handles graceful shutdown.
*   **`manager.py`**: The core WebRTC state engine (`SFUManager`). Manages `RTCPeerConnection` instances, tracks, and ICE candidates. It uses `aiortc.contrib.media.MediaRelay` to efficiently proxy and duplicate incoming media tracks to multiple subscribers.
*   **`handlers.py`**: The REST/WebSocket controllers (`SFUHandlers`). Translates incoming HTTP requests (e.g., `/publish`, `/disconnect`) and WebSocket messages into actions on the `SFUManager`.

### Client (`client/`)
The client is a desktop application that captures local media, communicates with the SFU, and renders remote streams.
*   **`client.py`**: The main orchestrator. It parses CLI arguments and uses Python's `multiprocessing` to isolate the networking/WebRTC logic from the heavy GUI rendering logic.
*   **`webrtc.py`**: Contains the core `aiortc` publishing and subscribing logic. It handles SDP negotiation, ICE candidate gathering, and maintains a WebSocket listener loop for dynamic server renegotiations.
*   **`signaling.py`**: An `aiohttp` wrapper (`SFUClient`) that manages the REST API calls and the long-lived WebSocket connection to the server.
*   **`media.py`**: Cross-platform hardware abstraction. Uses `aiortc`'s `MediaPlayer` to capture camera and microphone feeds, applying OS-specific format drivers (v4l2/alsa for Linux, avfoundation for macOS, dshow for Windows).
*   **`sinks.py`**: Handles incoming media. The `AudioSink` plays sound via `sounddevice`. The `VideoSink` offloads YUV-to-BGR conversion to a thread pool and pushes the decoded frames to a multiprocessing queue for the GUI.
*   **`gui.py`**: Runs in a completely separate OS process. It constantly pulls frames from the multiprocessing queue and uses OpenCV to draw a dynamic grid of participants (including a local picture-in-picture preview).

---

## 3. Key Architectural Patterns

### A. Bypassing the Python GIL (Client)
Because real-time video decoding and UI rendering are both highly CPU-bound, running them in the same process as the `asyncio` WebRTC networking loop would cause severe stuttering due to Python's Global Interpreter Lock (GIL). 

**Solution:** The client uses a dual-process architecture:
1.  **Main Process:** Handles network I/O, WebRTC state, and media demuxing.
2.  **GUI Process:** Dedicated solely to OpenCV rendering (`gui.py`).
Communication between them relies on `multiprocessing.Queue` (for video frames) and `multiprocessing.Manager.dict` (for shared UI state like mute toggles). **Always respect this boundary when adding features.**

### B. Dynamic Renegotiation via WebSockets (Server/Client)
When a new publisher joins the room, existing subscribers need to receive the new media tracks.
1.  **Server side (`manager.py`)**: Detects new tracks, debounces the update, and pushes a new SDP Offer over the active WebSocket connection to all existing subscribers.
2.  **Client side (`webrtc.py`)**: The `handle_messages` async loop intercepts the `"offer"`, applies it via `setRemoteDescription`, generates an `"answer"`, and sends it back over the WebSocket.

---

## 4. ⚠️ CRITICAL CONTEXT: Gotchas ⚠️

1.  **Video Format Conversion:**
    `aiortc` provides video frames in YUV format. OpenCV requires BGR format. The conversion `frame.to_ndarray(format="bgr24")` is CPU-intensive and is deliberately offloaded to a `ThreadPoolExecutor` inside `client/sinks.py` before being sent to the IPC queue.
2.  **Audio Playback:**
    Incoming audio is pushed to a `sounddevice.OutputStream` via a buffer in `client/sinks.py`. Audio drops or stuttering usually indicate the buffer is starving due to the main event loop blocking for too long.

## 5. Development Workflows

*   **Modifying the UI:** Look at `client/gui.py`. Remember this runs in an isolated process. To send data back to the network layer (like a button click), use the shared `multiprocessing.Manager` dictionary or add a dedicated command queue.
*   **Adding a Server Endpoint:** Add the HTTP handler to `server/handlers.py` and implement the underlying state logic in `server/manager.py`.
*   **Modifying WebRTC Logic:** The core setup is in `client/webrtc.py` and `server/manager.py`. Pay attention to the `asyncio` locks used during negotiation.
*   **Testing:** It is highly recommended to use the `--video-source path/to/video.mp4` flag when testing the client to avoid tying up the developer machine's physical webcam.