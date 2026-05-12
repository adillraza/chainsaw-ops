"""RingCentral Engage Voice / RingCX Call Streaming receiver + browser fan-out.

Two WebSocket endpoints on the same port:

  /voice-stream  ← RingCentral PUSHES audio here (per-call connection)
  /listen        → Browser SUBSCRIBES here to hear a live call's audio

Per the RC docs, RC sends Connect → Start → Media (every ~20ms) → Stop.
Audio is base64-encoded μ-law @ 8 kHz, stereo (left=agent, right=customer).
We decode it once to 16-bit linear PCM and fan it out to any browser
that's connected to /listen for the same phone number.

By design there is no disk persistence. Nothing is written. Audio
exists only in-flight between RC, our process, and the listening
browser. The moment a Stop arrives or the WS closes, the call's
buffer is dropped.

Auth on /listen:
  Browsers join with ``?phone=<digits>&token=<hmac>``. Token is HMAC-
  SHA256 of ``<phone>:<expiry_ts>`` using RCX_LISTEN_SECRET, truncated
  to 16 hex chars. Flask issues these via /customer/api/listen-token.

Run locally:
    python3 scripts/rcx_stream_server.py --port 3333

For prod, nginx terminates TLS and proxies /voice-stream + /listen
to 127.0.0.1:3333 (see deploy/nginx/voice-stream.location.conf).
"""
from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.exceptions import ConnectionClosed

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
log = logging.getLogger("rcx-stream")

LISTEN_SECRET = (os.environ.get("RCX_LISTEN_SECRET")
                 or os.environ.get("SECRET_KEY")  # fall back to Flask secret
                 or "").encode() or None

PRODUCER_SECRET = os.environ.get("RCX_STREAM_SECRET")  # set on the RC streaming profile


# ---------------------------------------------------------------------------
# Phone normalisation — match how the rest of the app stores numbers so
# /listen lookups by phone match the Customer 360 page's URL slug.
# ---------------------------------------------------------------------------

def normalise_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if digits.startswith("61") and len(digits) == 11:
        digits = "0" + digits[2:]
    return digits


# ---------------------------------------------------------------------------
# In-memory state — one CallSession per active /voice-stream connection
# ---------------------------------------------------------------------------

# call_id → CallSession
active_by_call: dict[str, "CallSession"] = {}
# phone (ANI or DNIS) → CallSession — for /listen lookups
active_by_phone: dict[str, "CallSession"] = {}


class CallSession:
    """One in-flight call. Owns the producer WS, decodes μ-law, fans the
    PCM bytes out to any browser subscriber queues attached to it."""

    def __init__(self, query: dict, conn_id: str):
        self.query = query
        self.conn_id = conn_id
        self.call_id: str | None = query.get("Call ID")
        self.ani: str | None = normalise_phone(query.get("Caller ANI"))
        self.dnis: str | None = normalise_phone(query.get("DNIS"))
        self.agent_id: str | None = query.get("Agent ID")
        self.product_type: str | None = query.get("Product Type")
        self.sample_rate = 8000
        self.channels = 2
        self.subscribers: set[asyncio.Queue] = set()
        self.media_count = 0
        self.bytes_pushed = 0
        self.started_at = time.perf_counter()
        # Self-register under whatever ids we have from the handshake.
        self._register()

    # --- producer-side hooks ---------------------------------------------

    def on_connect(self, evt: dict) -> None:
        log.info("[%s] Connect %s", self.conn_id, evt)

    def on_start(self, evt: dict) -> None:
        md = evt.get("metadata") or {}
        # Prefer Start metadata over handshake query — Start always has
        # canonical values per the spec.
        self.call_id = str(md.get("callId") or self.call_id or "unknown")
        self.ani = normalise_phone(md.get("ani")) or self.ani
        self.dnis = normalise_phone(md.get("dnis")) or self.dnis
        self.agent_id = str(md.get("agentId") or self.agent_id or "")
        self.product_type = str(md.get("productType") or self.product_type or "")
        self.sample_rate = int(md.get("sampleRateHertz") or 8000)
        self._register()
        log.info("[%s] Start  call_id=%s  ani=%s  dnis=%s  agent=%s  rate=%d  subscribers=%d",
                 self.conn_id, self.call_id, self.ani, self.dnis,
                 self.agent_id, self.sample_rate, len(self.subscribers))

    def on_media(self, evt: dict) -> None:
        b64 = evt.get("media")
        if not b64:
            return
        try:
            mulaw = base64.b64decode(b64)
        except Exception as exc:
            log.warning("[%s] media decode failed: %s", self.conn_id, exc)
            return
        # Interleaved stereo μ-law → 16-bit linear PCM, still interleaved.
        try:
            pcm = audioop.ulaw2lin(mulaw, 2)
        except Exception as exc:
            log.warning("[%s] ulaw2lin failed: %s", self.conn_id, exc)
            return
        self.media_count += 1
        self.bytes_pushed += len(pcm)
        # Fan-out to every subscriber. Drop frames on slow subscribers
        # (queue full) rather than buffering forever — better to glitch
        # than to deadlock.
        for q in list(self.subscribers):
            try:
                q.put_nowait(pcm)
            except asyncio.QueueFull:
                pass

    def on_stop(self, evt: dict) -> None:
        secs = (evt.get("metadata") or {}).get("duration")
        elapsed = time.perf_counter() - self.started_at
        log.info("[%s] Stop   call_id=%s  duration=%s  media_msgs=%d  pcm_bytes=%d  elapsed=%.1fs  subscribers=%d",
                 self.conn_id, self.call_id, secs, self.media_count,
                 self.bytes_pushed, elapsed, len(self.subscribers))
        self.close()

    def on_unknown(self, evt: dict) -> None:
        log.warning("[%s] unknown event %r", self.conn_id, evt.get("event"))

    # --- consumer-side hooks ---------------------------------------------

    def add_subscriber(self) -> asyncio.Queue:
        # Bounded queue so a slow browser can't grow it unboundedly.
        # At 8kHz stereo 16-bit, 50 packets/s × 320 bytes ≈ 16 KB/s.
        # 100 entries ≈ 2 seconds of latency tolerance.
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.subscribers.add(q)
        return q

    def remove_subscriber(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    # --- lifecycle -------------------------------------------------------

    def close(self) -> None:
        # Unregister
        if self.call_id and active_by_call.get(self.call_id) is self:
            del active_by_call[self.call_id]
        for p in (self.ani, self.dnis):
            if p and active_by_phone.get(p) is self:
                del active_by_phone[p]
        # Wake every subscriber with a sentinel so they can disconnect
        # cleanly. Use None as the sentinel.
        for q in list(self.subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self.subscribers.clear()

    def _register(self) -> None:
        if self.call_id:
            active_by_call[self.call_id] = self
        for p in (self.ani, self.dnis):
            if p:
                active_by_phone[p] = self


# ---------------------------------------------------------------------------
# Producer handler — RC connects, sends Connect/Start/Media/Stop
# ---------------------------------------------------------------------------

_conn_counter = 0


def _next_conn_id() -> str:
    global _conn_counter
    _conn_counter += 1
    return f"c{_conn_counter:04d}"


def _request_path(ws) -> str:
    req = getattr(ws, "request", None)
    if req is not None:
        return getattr(req, "path", "/") or "/"
    return getattr(ws, "path", "/") or "/"


async def handle_producer(ws):
    """RC → us. One WS = one call."""
    conn_id = _next_conn_id()
    parsed = urlparse(_request_path(ws))
    qs = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}

    if PRODUCER_SECRET:
        offered = qs.get("secret") or qs.get("Secret")
        if offered != PRODUCER_SECRET:
            log.warning("[%s] producer secret mismatch — refusing", conn_id)
            await ws.close(code=4401, reason="secret mismatch")
            return

    log.info("[%s] producer connected  path=%s  qs_keys=%s",
             conn_id, parsed.path, sorted(qs.keys()))
    session = CallSession(query=qs, conn_id=conn_id)

    try:
        async for raw in ws:
            try:
                evt = json.loads(raw)
            except Exception:
                continue
            e = evt.get("event")
            if e == "Connected":  session.on_connect(evt)
            elif e == "Start":    session.on_start(evt)
            elif e == "Media":    session.on_media(evt)
            elif e == "Stop":     session.on_stop(evt)
            else:                 session.on_unknown(evt)
    except ConnectionClosed as exc:
        log.info("[%s] producer WS closed: code=%s reason=%s",
                 conn_id, exc.code, exc.reason)
    except Exception as exc:
        log.exception("[%s] producer handler error: %s", conn_id, exc)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Consumer handler — browser subscribes for a phone, gets streamed PCM
# ---------------------------------------------------------------------------

def _validate_listen_token(phone: str, token: str) -> bool:
    """Token format: ``<expiry_ts>:<hmac16>`` where hmac is HMAC-SHA256
    of ``<phone>:<expiry_ts>`` using LISTEN_SECRET, truncated to 16 hex.
    Returns True if signature + expiry both pass."""
    if not LISTEN_SECRET or not token:
        return False
    try:
        expiry_str, sig = token.split(":", 1)
        expiry = int(expiry_str)
    except (ValueError, AttributeError):
        return False
    if time.time() > expiry:
        return False
    expected = hmac.new(LISTEN_SECRET, f"{phone}:{expiry}".encode(),
                        hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(sig, expected)


async def handle_consumer(ws):
    """Browser → us. Stream PCM bytes for the call active on this phone."""
    parsed = urlparse(_request_path(ws))
    qs = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
    phone = normalise_phone(qs.get("phone"))
    token = qs.get("token") or ""

    if not phone or not _validate_listen_token(phone, token):
        log.warning("listen: refusing — phone=%r token_ok=%s",
                    phone, _validate_listen_token(phone or "", token))
        await ws.close(code=4401, reason="invalid token")
        return

    session = active_by_phone.get(phone)
    if session is None:
        # No active call for this phone right now — send a small JSON
        # status frame and close. The browser can retry shortly.
        log.info("listen: no active call for phone=%s", phone)
        try:
            await ws.send(json.dumps({"event": "no_active_call", "phone": phone}))
        except Exception: pass
        await ws.close(code=4404, reason="no active call")
        return

    log.info("listen: attached phone=%s call_id=%s (subscribers=%d → %d)",
             phone, session.call_id, len(session.subscribers), len(session.subscribers) + 1)
    q = session.add_subscriber()

    try:
        # Tell the browser the format so it can configure its AudioContext.
        # Mirrors the Start message shape for consistency.
        await ws.send(json.dumps({
            "event": "Start",
            "metadata": {
                "callId": session.call_id,
                "ani": session.ani, "dnis": session.dnis,
                "agentId": session.agent_id,
                "sampleRateHertz": session.sample_rate,
                "channels": session.channels,
                "encoding": "pcm_s16le",
            },
        }))

        while True:
            pcm = await q.get()
            if pcm is None:
                # Call ended — tell the browser then close.
                try:
                    await ws.send(json.dumps({"event": "Stop"}))
                except Exception: pass
                break
            try:
                await ws.send(pcm)
            except ConnectionClosed:
                break
    except Exception as exc:
        log.exception("listen: consumer error phone=%s: %s", phone, exc)
    finally:
        session.remove_subscriber(q)
        log.info("listen: detached phone=%s (subscribers now %d)",
                 phone, len(session.subscribers))


# ---------------------------------------------------------------------------
# Dispatch — pick producer or consumer handler based on path
# ---------------------------------------------------------------------------

async def handle_connection(ws):
    parsed = urlparse(_request_path(ws))
    if parsed.path == "/voice-stream":
        await handle_producer(ws)
    elif parsed.path == "/listen":
        await handle_consumer(ws)
    else:
        log.warning("unknown path %s — closing", parsed.path)
        await ws.close(code=4404, reason="unknown path")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main(host: str, port: int) -> None:
    if LISTEN_SECRET is None:
        log.warning("RCX_LISTEN_SECRET not set and SECRET_KEY missing — "
                    "/listen endpoint will reject all browser subscribers")
    log.info("starting rcx-stream-server on %s:%d  (producer_secret=%s, listen_secret=%s)",
             host, port, "set" if PRODUCER_SECRET else "not set",
             "set" if LISTEN_SECRET else "not set")
    async with websockets.serve(handle_connection, host, port,
                                ping_interval=20, ping_timeout=20,
                                max_size=2 * 1024 * 1024):
        await asyncio.Future()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=3333)
    args = ap.parse_args()
    try:
        asyncio.run(main(args.host, args.port))
    except KeyboardInterrupt:
        log.info("stopped by user")
