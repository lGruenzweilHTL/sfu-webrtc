# WebRTC SFU Project

A complete WebRTC **Selective Forwarding Unit** (SFU) in Python. Each client publishes its camera/microphone to the server, which forwards the streams to all other connected clients — no peer-to-peer connections required.

```
Publisher A ──▶ SFU Server ──▶ Subscriber B
                           ──▶ Subscriber C
Publisher B ──▶ SFU Server ──▶ Subscriber A
                           ──▶ Subscriber C
```

## Project Structure

```
webrtc-project/
├── server/
│   └── server.py          # SFU server (aiohttp + aiortc)
├── client/
│   └── client.py          # Python client (camera/mic + signaling)
├── docs/
│   └── how-webrtc-works.md  # Full WebRTC explainer
└── requirements.txt
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

**System requirements (ffmpeg):**
```bash
# Linux
sudo apt install ffmpeg v4l-utils

# macOS
brew install ffmpeg

# Windows
# Download from https://ffmpeg.org/download.html
```

### 2. Start the server

```bash
python server/server.py
# Listening on http://0.0.0.0:8080
```

### 3. Run a publisher client

```bash
# Using your webcam (Linux)
python client/client.py --mode publish

# Using a video file (works everywhere, great for testing)
python client/client.py --mode publish --video-source path/to/video.mp4
```

### 4. Run a subscriber client

```bash
# Subscribe and save received video to a file
python client/client.py --mode subscribe --output received.mp4

# Or run as a full participant (publish + subscribe)
python client/client.py --mode both --video-source test.mp4
```

## Client Options

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | SFU server host |
| `--port` | `8080` | SFU server port |
| `--mode` | `both` | `publish`, `subscribe`, or `both` |
| `--camera-index` | `0` | Camera device index |
| `--audio-device` | system default | Audio device name |
| `--video-source` | *(webcam)* | Use a video file instead of webcam |
| `--no-video` | false | Publish audio only |
| `--no-audio` | false | Publish video only |
| `--output` | *(discard)* | Save received media to this file |

## Server API

| Endpoint | Method | Description |
|---|---|---|
| `/publish` | POST | Publisher sends SDP offer, receives answer |
| `/subscribe` | POST | Server sends SDP offer to subscriber |
| `/subscribe/answer` | POST | Subscriber sends SDP answer |
| `/publishers` | GET | List active publishers |
| `/disconnect` | POST | Close a session |

## How It Works

See [docs/how-webrtc-works.md](docs/how-webrtc-works.md) for a full explanation of ICE, DTLS, SRTP, SDP, RTP/RTCP, and the SFU architecture.

## Key Design Decisions

**Why an SFU and not P2P?**
Each client only uploads once, regardless of how many others are watching. The server forwards packets without decoding — low CPU cost, good scalability.

**Why HTTP signaling?**
Simple and stateless. A real production system would use WebSocket for lower-latency trickle ICE, but HTTP polling works fine for a demonstration.

**Why aiortc?**
It's a pure-Python, full-featured WebRTC stack. It handles ICE, DTLS, SRTP, and codec packetization. The `MediaRelay` class makes SFU forwarding trivial.

## Extending This

- **Add STUN/TURN** for clients behind NAT — pass `RTCConfiguration` with `iceServers` to `RTCPeerConnection`
- **WebSocket signaling** — replace the HTTP polling with a WebSocket channel for true trickle ICE
- **Browser client** — the server API is HTTP+JSON; a browser using the standard `RTCPeerConnection` API can connect with minimal changes
- **Dynamic subscriptions** — re-negotiate subscriber connections when new publishers join
- **Recording** — replace `MediaBlackhole` with `MediaRecorder` to save streams to disk
