# How WebRTC Works

WebRTC (Web Real-Time Communication) is an open standard that lets browsers and applications send audio, video, and arbitrary data directly between peers — with no plugins required. This document explains the full stack, from ICE candidates to SRTP encryption, and covers the SFU (Selective Forwarding Unit) architecture used in this project.

---

## Table of Contents

1. [The Big Picture](#the-big-picture)
2. [Signaling — Negotiating a Connection](#signaling)
3. [ICE — Finding a Path Between Peers](#ice)
4. [DTLS — Securing the Channel](#dtls)
5. [SRTP — Encrypting Media](#srtp)
6. [RTP and RTCP — Sending Media](#rtp-and-rtcp)
7. [SDP — The Session Blueprint](#sdp)
8. [Codec Negotiation](#codec-negotiation)
9. [P2P vs SFU vs MCU](#p2p-vs-sfu-vs-mcu)
10. [This Project's Architecture](#this-projects-architecture)
11. [Full Connection Walkthrough](#full-connection-walkthrough)
12. [Common Gotchas](#common-gotchas)

---

## The Big Picture

A WebRTC call involves three distinct problems that the spec solves separately:

| Problem | Solved by |
|---|---|
| How do peers find each other? | ICE + STUN + TURN |
| How do they agree on what to send? | SDP + signaling |
| How do they send it securely? | DTLS + SRTP |

WebRTC intentionally does **not** define signaling — how peers exchange connection information is left to the application. This project uses plain HTTP POST requests, but WebSocket, SIP, XMPP, or carrier pigeons all work.

---

## Signaling

Before any audio or video flows, the two sides need to exchange a description of what they want to send and how to reach each other. This exchange is called **signaling**.

### Offer / Answer

WebRTC uses **SDP** (Session Description Protocol) as the format for this exchange. The flow is:

```
Caller                          Callee
  │                               │
  │── createOffer() ──────────────▶ (via your signaling channel)
  │                               │
  │◀─ createAnswer() ─────────────│
  │                               │
  │   Both call setLocalDescription() and setRemoteDescription()
```

- **Offer** — "I can send H.264 video at 30 fps, Opus audio at 48 kHz. My ICE candidates are ..."
- **Answer** — "OK, I accept H.264 and Opus. My ICE candidates are ..."

Once both sides have called `setLocalDescription` and `setRemoteDescription`, ICE and DTLS negotiation can begin in parallel.

### Trickle ICE

Finding ICE candidates takes time. Modern WebRTC uses **trickle ICE**: candidates are sent incrementally as they are discovered, rather than waiting for all of them before sending the offer. This cuts connection time significantly.

---

## ICE

**ICE** (Interactive Connectivity Establishment, RFC 8445) is the framework that figures out how two peers can actually reach each other on the internet, even behind NATs and firewalls.

### Candidate Types

ICE collects several types of candidate addresses:

| Type | What it is | Example |
|---|---|---|
| **host** | A local IP address on the machine | `192.168.1.42:54321` |
| **srflx** (server reflexive) | Your public IP as seen by a STUN server | `203.0.113.5:54321` |
| **relay** | An address on a TURN server | `198.51.100.99:3478` |
| **prflx** (peer reflexive) | Discovered during connectivity checks | rare |

### STUN

**STUN** (Session Traversal Utilities for NAT, RFC 5389) is a simple protocol: the client sends a request to a public STUN server, which replies with the client's public IP and port as observed from the internet. This is how you discover your `srflx` candidate.

```
Client ──── "what is my public IP?" ────▶ stun.l.google.com:19302
Client ◀─── "203.0.113.5:54321" ─────── STUN server
```

STUN is cheap and stateless — the server just reflects packets. But it fails if the NAT is symmetric (the port changes per destination).

### TURN

**TURN** (Traversal Using Relays around NAT, RFC 5766) is the fallback. A TURN server actively relays all media between the peers. It always works, but costs bandwidth on the relay server and adds latency. TURN is only used when direct connectivity fails.

### ICE Connectivity Checks

Once both sides have candidate lists, ICE performs **connectivity checks** — it tries every combination of local candidate ↔ remote candidate and picks the best pair that actually works. "Best" is defined by a priority formula that prefers host > srflx > relay.

---

## DTLS

Once ICE selects a candidate pair and the UDP path is established, **DTLS** (Datagram Transport Layer Security, RFC 6347) runs over it. DTLS is essentially TLS adapted for unreliable, unordered datagrams.

WebRTC mandates DTLS for two reasons:

1. **Key exchange** — DTLS derives the symmetric keys used by SRTP (below)
2. **Certificate fingerprint verification** — the SDP contains a fingerprint of each peer's self-signed certificate; if they don't match, the connection is rejected, preventing man-in-the-middle attacks

No certificate authority is needed — WebRTC peers use self-signed certs and verify the fingerprint in the SDP instead.

---

## SRTP

All media in WebRTC is encrypted with **SRTP** (Secure Real-time Transport Protocol, RFC 3711). SRTP adds authentication tags and encryption (AES-CM or AES-GCM) to RTP packets without the overhead of a full TLS record layer — critical for low-latency streaming.

The keys are derived from the DTLS handshake via **DTLS-SRTP** (RFC 5764). This means:

- ICE establishes the path
- DTLS authenticates the peers and hands off keys
- SRTP uses those keys to protect every packet

---

## RTP and RTCP

### RTP

**RTP** (Real-time Transport Protocol, RFC 3550) is the actual packet format for audio and video. Each RTP packet contains:

- **Payload type** — which codec (e.g. 96 = VP8, 111 = Opus)
- **Sequence number** — for detecting loss and reordering
- **Timestamp** — for synchronizing playback
- **SSRC** — identifies which stream (each track has a unique SSRC)
- **Payload** — the compressed audio/video data

### RTCP

**RTCP** (RTP Control Protocol) runs alongside RTP and carries feedback about the stream:

- **Receiver Reports (RR)** — tell the sender about packet loss, jitter, and round-trip time
- **NACK** (Negative Acknowledgement) — "I'm missing packet #4, please resend"
- **PLI** (Picture Loss Indication) — "Send me a keyframe, I'm lost"
- **REMB / TWCC** — bandwidth estimation for congestion control

The sender uses RTCP feedback to adapt bitrate and quality dynamically.

---

## SDP

**SDP** (Session Description Protocol, RFC 4566) is a text format that describes a media session. A WebRTC SDP contains several `m=` sections, one per track:

```
v=0
o=- 4611731400430051336 2 IN IP4 127.0.0.1
s=-
t=0 0
a=group:BUNDLE 0 1          ← Both tracks share one UDP socket (BUNDLE)
a=ice-lite

m=audio 9 UDP/TLS/RTP/SAVPF 111
a=rtpmap:111 opus/48000/2   ← Opus codec, 48 kHz, stereo
a=fmtp:111 minptime=10;useinbandfec=1
a=ice-ufrag:abc123
a=ice-pwd:supersecretpassword
a=fingerprint:sha-256 AA:BB:CC:...
a=sendrecv

m=video 9 UDP/TLS/RTP/SAVPF 96 97
a=rtpmap:96 VP8/90000        ← VP8 video
a=rtpmap:97 rtx/90000        ← RTX for retransmission
a=fmtp:97 apt=96
a=sendrecv
```

Key attributes:

- `a=ice-ufrag` / `a=ice-pwd` — credentials for ICE authentication
- `a=fingerprint` — DTLS certificate fingerprint
- `a=sendrecv` / `a=sendonly` / `a=recvonly` — direction
- `a=group:BUNDLE` — all tracks use one socket (reduces ports needed)
- `a=rtcp-mux` — RTCP and RTP share the same port

---

## Codec Negotiation

The offer lists all codecs the sender supports; the answer selects a subset. After the handshake both sides use only the agreed codecs.

Common WebRTC codecs:

| Media | Codec | Notes |
|---|---|---|
| Audio | **Opus** | Mandatory for WebRTC. Variable bitrate, 6–510 kbps, handles packet loss gracefully |
| Audio | G.711 (PCMU/PCMA) | Legacy PSTN codec, always included for interop |
| Video | **VP8** | Google's codec, royalty-free, mandatory baseline |
| Video | **VP9** | Better compression than VP8, also royalty-free |
| Video | **H.264** | Common in hardware encoders, used by most mobile devices |
| Video | **AV1** | Newest, best compression, still gaining hardware support |

---

## P2P vs SFU vs MCU

There are three main server architectures for multi-party WebRTC:

### Peer-to-Peer (Mesh)

Every participant connects directly to every other participant.

```
A ◀──▶ B
A ◀──▶ C
B ◀──▶ C
```

- **Pros:** No server cost, lowest latency
- **Cons:** Upload bandwidth scales with N–1 participants. Falls apart above ~4 people.

### SFU — Selective Forwarding Unit ✓ **(this project)**

Every participant sends one stream to the SFU. The SFU forwards it to everyone else, without decoding or re-encoding.

```
A ──▶ SFU ──▶ B
              ──▶ C
B ──▶ SFU ──▶ A
              ──▶ C
```

- **Pros:** Each publisher uploads once. Scalable to hundreds of participants. Server CPU is low (just forwarding packets).
- **Cons:** Each subscriber downloads N–1 streams. The server sees all traffic (can't end-to-end encrypt in the traditional sense without special extensions like SFrame).

Real-world SFUs: **mediasoup**, **Janus**, **Jitsi Videobridge**, **LiveKit**, **Pion**.

### MCU — Multipoint Control Unit

The server decodes all streams, composites them into a single video (like a grid), and re-encodes it for each participant.

```
A ──▶ MCU ──▶ composed stream ──▶ B
B ──▶      ──▶ composed stream ──▶ A
C ──▶      ──▶ composed stream ──▶ C
```

- **Pros:** Each participant uploads once and downloads once, regardless of how many others are in the call.
- **Cons:** Extremely CPU-intensive on the server. Highest latency. Rarely used today.

---

## This Project's Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    SFU Server (server.py)                │
│                                                         │
│  /publish  ← receives SDP offer from publisher          │
│  /subscribe → sends SDP offer to subscriber             │
│  /subscribe/answer ← receives SDP answer from sub       │
│                                                         │
│  MediaRelay ──── copies tracks to each subscriber       │
└─────────────────────────────────────────────────────────┘
         ▲                          │
         │ publish (SDP offer)      │ subscribe (SDP offer from server)
         │                          ▼
┌─────────────┐             ┌─────────────┐
│  client.py  │             │  client.py  │
│  (publisher)│             │ (subscriber)│
│             │             │             │
│  camera/mic │             │  output file│
│  ──────────▶│             │  or sink    │
└─────────────┘             └─────────────┘
```

### Signaling Protocol (HTTP)

| Endpoint | Method | Who calls it | Purpose |
|---|---|---|---|
| `/publish` | POST | Publisher | Send SDP offer, receive SDP answer |
| `/subscribe` | POST | Subscriber | Request subscription, receive SDP offer |
| `/subscribe/answer` | POST | Subscriber | Send SDP answer back |
| `/ice-candidate` | POST | Either | Send trickle ICE candidate |
| `/ice-candidates/{id}` | GET | Either | Poll for server's candidates |
| `/publishers` | GET | Anyone | List active publishers |
| `/disconnect` | POST | Either | Clean up a session |

### Key Library: aiortc

**aiortc** is a pure-Python WebRTC implementation. It handles the full stack:
- ICE (via `aioice`)
- DTLS (via PyOpenSSL)
- SRTP (via cryptography)
- RTP/RTCP packetization
- Codec support via ffmpeg (through `av`)

**MediaRelay** is an aiortc helper that copies a single `MediaStreamTrack` to multiple consumers without re-encoding. This is the core of the SFU: one publisher's track is relayed to N subscribers efficiently.

---

## Full Connection Walkthrough

Here is the complete sequence for a publisher joining:

```
Publisher Client                    SFU Server
     │                                  │
     │  1. Open camera/mic              │
     │     MediaPlayer → video+audio    │
     │     tracks                       │
     │                                  │
     │  2. pc.addTrack(video)           │
     │     pc.addTrack(audio)           │
     │                                  │
     │  3. createOffer()                │
     │     → generates SDP with        │
     │       codec preferences +       │
     │       local ICE candidates       │
     │                                  │
     │  4. setLocalDescription(offer)   │
     │                                  │
     │──── POST /publish {sdp, type} ──▶│
     │                                  │  5. setRemoteDescription(offer)
     │                                  │  6. createAnswer()
     │                                  │  7. setLocalDescription(answer)
     │◀─── {session_id, sdp, type} ────│
     │                                  │
     │  8. setRemoteDescription(answer) │
     │                                  │
     │  ~~~ ICE connectivity checks ~~~│
     │  ~~~ DTLS handshake          ~~~│
     │  ~~~ SRTP keys derived       ~~~│
     │                                  │
     │════ encrypted RTP video/audio ══▶│
     │                                  │
```

A subscriber runs a mirror-image flow: the server creates the offer (since it already has the tracks), and the client answers.

---

## Common Gotchas

**NAT traversal fails without STUN/TURN.** If your client and server are on different networks (or behind strict NATs), you need to configure ICE servers. In aiortc, pass them to `RTCPeerConnection`:

```python
from aiortc import RTCConfiguration, RTCIceServer
config = RTCConfiguration(iceServers=[
    RTCIceServer(urls="stun:stun.l.google.com:19302"),
    RTCIceServer(urls="turn:your-turn-server.example.com", username="user", credential="pass"),
])
pc = RTCPeerConnection(configuration=config)
```

**BUNDLE reduces ports.** Without `a=group:BUNDLE`, each track needs its own UDP port. Modern WebRTC bundles all tracks onto one port.

**Linux camera access requires v4l2.** Make sure `ffmpeg` is compiled with `--enable-libv4l2` and your user is in the `video` group (`sudo usermod -aG video $USER`).

**Symmetric NAT blocks srflx candidates.** If both peers are behind symmetric NAT, host and srflx candidates will fail and you must use a TURN relay.

**SDP is stateful.** You cannot re-use a `RTCPeerConnection` after it is closed. Create a new one for each session.

**aiortc's MediaPlayer loops.** When using a file as a source, `loop=True` keeps sending frames indefinitely — useful for testing. Remove it for one-shot playback.

**Subscribers must connect after publishers.** The SFU adds tracks from currently active publishers at subscribe time. If a new publisher joins after a subscriber connected, the subscriber won't see the new publisher without re-subscribing. Production SFUs handle this with server-side re-negotiation.

---

## Further Reading

- [WebRTC for the Curious](https://webrtcforthecurious.com/) — free book covering the full protocol stack in depth
- [RFC 8825](https://datatracker.ietf.org/doc/html/rfc8825) — WebRTC overview
- [RFC 8445](https://datatracker.ietf.org/doc/html/rfc8445) — ICE
- [RFC 3550](https://datatracker.ietf.org/doc/html/rfc3550) — RTP/RTCP
- [aiortc documentation](https://aiortc.readthedocs.io/)
- [mediasoup](https://mediasoup.org/) — production-grade SFU in Node.js/Rust
