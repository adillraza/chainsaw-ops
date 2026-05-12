"""RingCentral Engage Voice / RingCX Call Streaming receiver.

A minimal WebSocket Secure server matching the protocol documented at:
  https://developers.ringcentral.com/engage/voice/guide/workforce/call-streaming/getting-started

RC connects to us per agent-answered call, then sends:
  Connect → Start → Media (every ~20ms) → Stop

Audio is base64-encoded μ-law @ 8 kHz, stereo (left = agent,
right = customer). We decode each Media chunk to 16-bit linear PCM and
write a stereo WAV to ``/tmp/rcx-call-<call_id>.wav`` so the test loop
can ``ffplay`` it.

JSON event lines are also dumped to ``/tmp/rcx-call-<call_id>.jsonl``
for forensic debugging.

This script is intentionally dependency-light: stdlib + ``websockets``.
Phase 1 deliverable — proven receiver, plug into Deepgram in Phase 2.

Run locally:
    python3 scripts/rcx_stream_server.py --port 3333

For prod, terminate TLS at nginx (the ``ws://`` server listens locally
on port 3333; nginx's ``wss://ops.jonoandjohno.com.au/voice-stream``
location proxies through). See deploy/systemd/rcx-stream.service.
"""
from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import json
import logging
import os
import re
import struct
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.exceptions import ConnectionClosed

LOG_FMT = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FMT)
log = logging.getLogger("rcx-stream")

OUTPUT_DIR = Path(os.environ.get("RCX_STREAM_OUTPUT_DIR", "/tmp"))
SHARED_SECRET = os.environ.get("RCX_STREAM_SECRET")  # match the "secret" on the streaming profile


# ---------------------------------------------------------------------------
# Per-connection state — one CallSession per WebSocket from RC
# ---------------------------------------------------------------------------

class CallSession:
    """Tracks one in-flight call. Opens a WAV on Start, appends μ-law-decoded
    audio on each Media, closes on Stop. All inputs are best-effort so an
    unexpected payload doesn't drop the connection mid-call."""

    def __init__(self, query: dict, conn_id: str):
        self.query = query
        self.conn_id = conn_id
        self.call_id: str | None = None
        self.session_id: str | None = None
        self.ani: str | None = None
        self.dnis: str | None = None
        self.agent_id: str | None = None
        self.product_type: str | None = None
        self.metadata: dict = {}

        # Audio format — set on Start. The spec is fixed (μ-law/8kHz/stereo)
        # but read from the message so we trust RC's stated values.
        self.sample_rate = 8000
        self.channels    = 2

        # Output handles — opened on first Media message so we can name
        # them with the real call_id from Start.
        self._wav: wave.Wave_write | None = None
        self._wav_path: Path | None = None
        self._jsonl_fh = None
        self._jsonl_path: Path | None = None
        self._last_seq: int = -1
        self._media_count = 0
        self._bytes_written = 0
        self._started_at = time.perf_counter()

    # --- public API used by the connection handler -----------------------

    def on_connect(self, evt: dict) -> None:
        self._record(evt)
        log.info("[%s] Connect %s", self.conn_id, evt)

    def on_start(self, evt: dict) -> None:
        self._record(evt)
        md = evt.get("metadata") or {}
        self.metadata     = md
        self.call_id      = str(md.get("callId") or self.query.get("Call ID") or "unknown")
        self.session_id   = str(md.get("sessionId") or self.query.get("Session ID") or "")
        self.ani          = str(md.get("ani")  or self.query.get("Caller ANI") or "")
        self.dnis         = str(md.get("dnis") or self.query.get("DNIS") or "")
        self.agent_id     = str(md.get("agentId") or self.query.get("Agent ID") or "")
        self.product_type = str(md.get("productType") or self.query.get("Product Type") or "")
        self.sample_rate  = int(md.get("sampleRateHertz") or 8000)

        content_type = (md.get("contentType") or "audio/x-mulaw").lower()
        if "mulaw" not in content_type and "ulaw" not in content_type:
            log.warning("[%s] unexpected contentType: %s — assuming μ-law anyway",
                        self.conn_id, content_type)

        self._open_wav()
        log.info("[%s] Start  call_id=%s  ani=%s  dnis=%s  agent=%s  type=%s  rate=%d",
                 self.conn_id, self.call_id, self.ani, self.dnis,
                 self.agent_id, self.product_type, self.sample_rate)

    def on_media(self, evt: dict) -> None:
        # We DO record media events to JSONL — but with the audio field
        # truncated, so the file isn't a multi-GB blob.
        rec = dict(evt)
        if "media" in rec:
            rec["media"] = f"<base64 {len(rec['media'])} chars>"
        self._record(rec)

        seq = self._try_int(evt.get("sequenceId"))
        if seq is not None:
            if 0 < seq <= self._last_seq:
                # Out-of-order chunk; spec says we can ignore safely.
                log.debug("[%s] dropping out-of-order seq %d (last=%d)",
                          self.conn_id, seq, self._last_seq)
                return
            self._last_seq = seq

        audio_b64 = evt.get("media")
        if not audio_b64 or not self._wav:
            return
        try:
            mulaw = base64.b64decode(audio_b64)
        except Exception as exc:
            log.warning("[%s] media decode failed: %s", self.conn_id, exc)
            return

        # μ-law byte sequence is interleaved stereo (L R L R …) — ulaw2lin
        # converts each μ-law byte to a 2-byte linear PCM sample, preserving
        # byte order, so the resulting PCM is also interleaved stereo.
        try:
            pcm = audioop.ulaw2lin(mulaw, 2)
        except Exception as exc:
            log.warning("[%s] ulaw2lin failed: %s", self.conn_id, exc)
            return

        self._wav.writeframes(pcm)
        self._bytes_written += len(pcm)
        self._media_count += 1

    def on_stop(self, evt: dict) -> None:
        self._record(evt)
        secs = (evt.get("metadata") or {}).get("duration")
        log.info("[%s] Stop   call_id=%s  duration=%ss  media_msgs=%d  pcm_bytes=%d",
                 self.conn_id, self.call_id, secs, self._media_count, self._bytes_written)
        self._close()

    def on_unknown(self, evt: dict) -> None:
        self._record(evt)
        log.warning("[%s] unknown event %r in %r", self.conn_id, evt.get("event"), evt)

    def close(self) -> None:
        """Called when the connection drops. Flushes whatever audio we
        managed to capture."""
        self._close()
        elapsed = time.perf_counter() - self._started_at
        log.info("[%s] connection closed  call_id=%s  elapsed=%.1fs  media_msgs=%d  pcm_bytes=%d  wav=%s",
                 self.conn_id, self.call_id, elapsed, self._media_count,
                 self._bytes_written, self._wav_path)

    # --- internals -------------------------------------------------------

    def _safe_filename(self, base: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", base or "unknown")

    def _open_wav(self) -> None:
        if self._wav is not None:
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem = f"rcx-call-{ts}-{self._safe_filename(self.call_id or 'unknown')}"
        self._wav_path = OUTPUT_DIR / f"{stem}.wav"
        w = wave.open(str(self._wav_path), "wb")
        w.setnchannels(self.channels)
        w.setsampwidth(2)         # 16-bit linear PCM
        w.setframerate(self.sample_rate)
        self._wav = w

        self._jsonl_path = OUTPUT_DIR / f"{stem}.jsonl"
        self._jsonl_fh = open(self._jsonl_path, "w")
        # Pre-write the query-string metadata as the first record so we
        # have everything in one file.
        self._jsonl_fh.write(json.dumps({"_meta": "connect_query", "query": self.query}) + "\n")

    def _close(self) -> None:
        if self._wav is not None:
            try: self._wav.close()
            except Exception: pass
            self._wav = None
        if self._jsonl_fh is not None:
            try: self._jsonl_fh.close()
            except Exception: pass
            self._jsonl_fh = None

    def _record(self, evt: dict) -> None:
        if self._jsonl_fh is None:
            return
        try:
            self._jsonl_fh.write(json.dumps(evt, default=str) + "\n")
            self._jsonl_fh.flush()
        except Exception:
            pass

    @staticmethod
    def _try_int(v) -> int | None:
        try: return int(v)
        except (TypeError, ValueError): return None


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

_conn_counter = 0


def _next_conn_id() -> str:
    global _conn_counter
    _conn_counter += 1
    return f"c{_conn_counter:04d}"


async def handle_connection(ws):
    """One WS connection = one call.

    The websockets library accepts a single-arg handler in newer versions;
    we read the request path off ``ws.request`` for query parameters.
    """
    conn_id = _next_conn_id()

    # Pull query string from the handshake request — RC passes Call ID,
    # Session ID, Caller ANI, DNIS, Account IDs, Agent ID, Product Type
    # and Product ID here per the spec.
    try:
        path = getattr(ws, "request", None).path if getattr(ws, "request", None) else getattr(ws, "path", "/")
    except Exception:
        path = "/"
    parsed = urlparse(path)
    qs = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}

    # Optional shared secret check — RC includes the "secret" we set on the
    # streaming profile in the handshake (typically as a query param or
    # custom header). If RCX_STREAM_SECRET is set we enforce a match.
    if SHARED_SECRET:
        offered = qs.get("secret") or qs.get("Secret")
        if offered != SHARED_SECRET:
            log.warning("[%s] secret mismatch — refusing connection (path=%s)",
                        conn_id, parsed.path)
            await ws.close(code=4401, reason="secret mismatch")
            return

    log.info("[%s] connected  path=%s  qs_keys=%s", conn_id, parsed.path, sorted(qs.keys()))
    session = CallSession(query=qs, conn_id=conn_id)

    try:
        async for raw in ws:
            try:
                evt = json.loads(raw)
            except Exception:
                log.warning("[%s] non-JSON frame, %d bytes — ignored", conn_id, len(raw or b""))
                continue
            event_type = evt.get("event")
            if event_type == "Connected":   session.on_connect(evt)
            elif event_type == "Start":     session.on_start(evt)
            elif event_type == "Media":     session.on_media(evt)
            elif event_type == "Stop":      session.on_stop(evt)
            else:                           session.on_unknown(evt)
    except ConnectionClosed as exc:
        log.info("[%s] WebSocket closed: code=%s reason=%s",
                 conn_id, exc.code, exc.reason)
    except Exception as exc:
        log.exception("[%s] unexpected handler error: %s", conn_id, exc)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main(host: str, port: int) -> None:
    log.info("starting rcx-stream-server on %s:%d  (output_dir=%s, secret=%s)",
             host, port, OUTPUT_DIR, "set" if SHARED_SECRET else "not set")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    async with websockets.serve(handle_connection, host, port,
                                ping_interval=20, ping_timeout=20,
                                max_size=2 * 1024 * 1024):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=3333)
    args = ap.parse_args()
    try:
        asyncio.run(main(args.host, args.port))
    except KeyboardInterrupt:
        log.info("stopped by user")
