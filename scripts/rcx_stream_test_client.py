"""Synthetic RCX Call Streaming client — pretends to be RingCentral.

Connects to the local rcx_stream_server.py and replays the documented
Connect → Start → Media (with real synthetic μ-law audio) → Stop flow.
Used to validate the receiver end-to-end without needing a real call
or any RC platform access.

The synthetic audio is a stereo sine pair: 440 Hz (A4) on the agent's
channel and 880 Hz (A5) on the customer's, so a) the resulting WAV
sounds like a clear two-tone chord, b) it's obvious in the spectrogram
whether stereo separation came through correctly.

Usage:
    python3 scripts/rcx_stream_server.py --port 3333    # in terminal 1
    python3 scripts/rcx_stream_test_client.py           # in terminal 2

Then:
    ffplay /tmp/rcx-call-*.wav
"""
from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import json
import math
import struct
from urllib.parse import urlencode

import websockets

SAMPLE_RATE = 8000
CHANNELS    = 2
CHUNK_MS    = 20
SAMPLES_PER_CHUNK = SAMPLE_RATE * CHUNK_MS // 1000   # 160 frames per channel per chunk
DURATION_S  = 5

# Identifying metadata — purely synthetic; mirrors the shape RC sends.
CALL_ID    = "synthetic-test-001"
SESSION_ID = 1
ANI        = "0412345678"
DNIS       = "0353030263"
AGENT_ID   = 42
PRODUCT_ID = 100
ACCT_ID    = "99990000"
SUBACCT_ID = "99990001"
RC_ACCT_ID = "123456789"


def make_stereo_mulaw_chunk(t_start: float, left_hz: float, right_hz: float) -> bytes:
    """One 20ms stereo μ-law chunk at the requested tones.

    Frames are interleaved (L R L R …) — matches the format the receiver
    expects in the Media payload.
    """
    pcm = bytearray()
    for i in range(SAMPLES_PER_CHUNK):
        t = t_start + i / SAMPLE_RATE
        l = int(0.4 * 32767 * math.sin(2 * math.pi * left_hz  * t))
        r = int(0.4 * 32767 * math.sin(2 * math.pi * right_hz * t))
        pcm += struct.pack("<hh", l, r)
    # ulaw2lin's inverse is lin2ulaw — same byte-by-byte mapping so the
    # interleaved order is preserved.
    return audioop.lin2ulaw(bytes(pcm), 2)


async def run(host: str, port: int) -> None:
    # The handshake URL carries the same query params RC documents:
    # Call ID, Session ID, Caller ANI, DNIS, EV Account ID,
    # EV Subaccount ID, RingCentral Office Account ID, Agent ID,
    # Product Type, Product ID.
    qs = urlencode({
        "Call ID":                       CALL_ID,
        "Session ID":                    str(SESSION_ID),
        "Caller ANI":                    ANI,
        "DNIS":                          DNIS,
        "EV Account ID":                 ACCT_ID,
        "EV Subaccount ID":              SUBACCT_ID,
        "RingCentral Office Account ID": RC_ACCT_ID,
        "Agent ID":                      str(AGENT_ID),
        "Product Type":                  "Queue",
        "Product ID":                    str(PRODUCT_ID),
    })
    url = f"ws://{host}:{port}/voice-stream?{qs}"
    print(f"connecting → {url}")

    async with websockets.connect(url, max_size=2 * 1024 * 1024) as ws:
        # Connect message
        await ws.send(json.dumps({
            "event": "Connected",
            "protocol": "AgentSession",
            "version": "1.0.0",
        }))

        # Start message — full metadata block per spec
        await ws.send(json.dumps({
            "event": "Start",
            "metadata": {
                "callId":          CALL_ID,
                "sessionId":       SESSION_ID,
                "ani":             ANI,
                "dnis":            DNIS,
                "accountId":       ACCT_ID,
                "subaccountId":    SUBACCT_ID,
                "rcAccountId":     RC_ACCT_ID,
                "agentId":         AGENT_ID,
                "productType":     "Queue",
                "productId":       PRODUCT_ID,
                "contentType":     "audio/x-mulaw",
                "sampleRateHertz": SAMPLE_RATE,
            },
        }))

        # Media — DURATION_S seconds of synthetic audio at 20 ms per chunk
        total_chunks = DURATION_S * 1000 // CHUNK_MS
        for seq in range(1, total_chunks + 1):
            t = (seq - 1) * CHUNK_MS / 1000.0
            chunk = make_stereo_mulaw_chunk(t, left_hz=440.0, right_hz=880.0)
            await ws.send(json.dumps({
                "event": "Media",
                "perspective": "Conference",
                "sequenceId": str(seq),
                "media": base64.b64encode(chunk).decode(),
            }))
            # Pace ~real-time so we exercise the receiver's flow
            await asyncio.sleep(CHUNK_MS / 1000.0)

        # Stop
        await ws.send(json.dumps({
            "event": "Stop",
            "metadata": {
                "duration":  DURATION_S,
                "end_time": "2026-05-12T00:00:00Z",
            },
        }))
        # Give the server a beat to flush its final write before we close.
        await asyncio.sleep(0.2)
    print("done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=3333)
    args = ap.parse_args()
    asyncio.run(run(args.host, args.port))
