"""Son d'un appel SIP pour la sonde (prototype) : RTP sur UDP, chiffré par SDES-SRTP, en G.711 μ-law à 8 kHz.

Environnement de la passerelle (pylibsrtp, numpy, PyAV). Un paquet toutes les 20 ms, même en silence : le premier
paquet envoyé ouvre aussi le passage du retour dans la box (RTP symétrique).
"""

from __future__ import annotations

import base64
import random
import socket
import struct
import threading
import time

import numpy as np
from pylibsrtp import Policy, Session

RATE = 8000
SAMPLES = 160  # 20 ms
PCMU = 0


def ulaw_encode(pcm: np.ndarray) -> bytes:
    x = pcm.astype(np.int32)
    sign = np.where(x < 0, 0x80, 0)
    x = np.minimum(np.abs(x), 32635) + 0x84
    exponent = np.clip(np.floor(np.log2(x)).astype(np.int32) - 7, 0, 7)
    mantissa = (x >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()


def ulaw_decode(data: bytes) -> np.ndarray:
    u = ~np.frombuffer(data, dtype=np.uint8).astype(np.int32) & 0xFF
    exponent, mantissa = (u >> 4) & 0x07, u & 0x0F
    x = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return np.where(u & 0x80, -x, x).astype(np.int16)


def srtp(key_b64: str, outbound: bool) -> Session:
    policy = Policy(key=base64.b64decode(key_b64),
                    ssrc_type=Policy.SSRC_ANY_OUTBOUND if outbound else Policy.SSRC_ANY_INBOUND,
                    srtp_profile=Policy.SRTP_PROFILE_AES128_CM_SHA1_80)
    return Session(policy=policy)


class RtpLink:
    """Envoie ``outgoing`` (PCM 8 kHz) au rythme réel ; ``on_audio(pcm)`` pour chaque paquet reçu et déchiffré."""

    def __init__(self, sock: socket.socket, remote: tuple[str, int], local_key: str, remote_key: str, on_audio):
        self.sock, self.remote, self.on_audio = sock, remote, on_audio
        self._tx, self._rx = srtp(local_key, True), srtp(remote_key, False)
        self._seq, self._ts, self._ssrc = random.randrange(65536), random.randrange(2 ** 32), random.randrange(2 ** 32)
        self._out = np.zeros(0, np.int16)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.sent = self.received = self.rejected = 0
        self.first_received: float | None = None
        self.started = time.monotonic()

    def play(self, pcm8k: np.ndarray) -> None:
        with self._lock:
            self._out = np.concatenate([self._out, pcm8k.astype(np.int16)])

    def pending(self) -> int:
        with self._lock:
            return int(self._out.size)

    def start(self) -> None:
        threading.Thread(target=self._send_loop, daemon=True).start()
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _send_loop(self) -> None:
        start, n = time.monotonic(), 0
        while not self._stop.is_set():
            with self._lock:
                chunk, self._out = self._out[:SAMPLES], self._out[SAMPLES:]
            if chunk.size < SAMPLES:
                chunk = np.concatenate([chunk, np.zeros(SAMPLES - chunk.size, np.int16)])
            header = struct.pack("!BBHII", 0x80, (0x80 if n == 0 else 0) | PCMU, self._seq, self._ts, self._ssrc)
            try:
                self.sock.sendto(self._tx.protect(header + ulaw_encode(chunk)), self.remote)
                self.sent += 1
            except OSError:
                pass
            self._seq, self._ts, n = (self._seq + 1) & 0xFFFF, (self._ts + SAMPLES) & 0xFFFFFFFF, n + 1
            wait = start + n * 0.02 - time.monotonic()
            if wait > 0:
                time.sleep(wait)

    def _recv_loop(self) -> None:
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            if len(data) < 12 or data[0] >> 6 != 2 or 72 <= (data[1] & 0x7F) <= 76:
                continue  # pas du RTP (RTCP multiplexé, STUN…)
            try:
                packet = self._rx.unprotect(data)
            except Exception:  # noqa: BLE001 - paquet non authentifié : ignoré
                self.rejected += 1
                continue
            if self.first_received is None:
                self.first_received = time.monotonic()
            self.received += 1
            if packet[1] & 0x7F == PCMU:
                cc = packet[0] & 0x0F
                self.on_audio(ulaw_decode(packet[12 + 4 * cc:]))
