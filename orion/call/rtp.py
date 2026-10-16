"""Son d'un appel SIP (Linphone) : RTP sur UDP, chiffré par SDES-SRTP, en Opus (bande large) ou G.711 (secours).

Même interface que le son WebRTC (orion/call/media.py), pour la passerelle et le Core :
- ``mic`` : micro de l'appel, en PCM int16 mono 16 kHz (``ByteStream``, lu par le Core sur ``/audio/input``) ;
- ``play(pcm, rate)``, ``drain()``, ``stop_voice()`` : voix d'ORION vers l'appelé ;
- ``close()``.
Un paquet toutes les 20 ms, même en silence : le premier paquet envoyé ouvre aussi le passage du retour dans la box
(RTP symétrique ; avec sip.linphone.org, le son passe par son relais). Environnement de la passerelle seulement
(pylibsrtp, PyAV, numpy).
"""

from __future__ import annotations

import base64
import fractions
import logging
import random
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass

import av
import numpy as np
from pylibsrtp import Policy, Session

from orion.call.media import ByteStream

log = logging.getLogger(__name__)

MIC_RATE = 16000
FRAME_SECONDS = 0.02
OPUS, PCMU, EVENTS = "opus", "PCMU", "telephone-event"
OFFER_PAYLOADS = {96: (OPUS, 48000), 0: (PCMU, 8000)}  # ordre de préférence : Opus d'abord
CRYPTO_SUITE = "AES_CM_128_HMAC_SHA1_80"


# --- G.711 μ-law -------------------------------------------------------------------------------------------

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


# --- Description du son (SDP) ------------------------------------------------------------------------------

def new_key() -> str:
    """Clé maîtresse SRTP (16 octets de clé + 14 de sel), en base 64, pour l'attribut a=crypto."""
    return base64.b64encode(secrets.token_bytes(30)).decode()


@dataclass
class MediaOffer:
    """Ce qu'un côté propose ou accepte : adresse et port du son, clé SRTP, codecs (payload -> (nom, fréquence))."""
    address: str
    port: int
    key: str
    payloads: dict[int, tuple[str, int]]
    events: int | None = None  # payload des touches (telephone-event), s'il y en a


def build_sdp(ip: str, port: int, key: str, payloads: dict[int, tuple[str, int]], events: int | None = 101,
              session: str = "ORION") -> str:
    order = " ".join(str(p) for p in [*payloads, *([events] if events is not None else [])])
    lines = ["v=0", f"o=orion {random.randrange(1, 2 ** 31)} 1 IN IP4 {ip}", f"s={session}", f"c=IN IP4 {ip}",
             "t=0 0", f"m=audio {port} RTP/SAVP {order}"]
    for payload, (name, rate) in payloads.items():
        lines.append(f"a=rtpmap:{payload} {name}/{rate}" + ("/2" if name == OPUS else ""))
        if name == OPUS:
            lines.append(f"a=fmtp:{payload} useinbandfec=1")
    if events is not None:
        lines.append(f"a=rtpmap:{events} telephone-event/8000")
    lines += [f"a=crypto:1 {CRYPTO_SUITE} inline:{key}", "a=sendrecv"]
    return "\r\n".join(lines) + "\r\n"


def parse_sdp(body: str) -> MediaOffer | None:
    """Son chiffré proposé par l'autre côté ; None sans son SRTP exploitable (son en clair : refusé)."""
    lines = [line.strip() for line in body.splitlines()]
    address = next((line.split()[2] for line in lines if line.startswith("c=IN IP4 ")), None)
    media = next((line for line in lines if line.startswith("m=audio ")), None)
    key = next((line.split("inline:", 1)[1].split("|")[0].split()[0] for line in lines
                if line.startswith("a=crypto:") and CRYPTO_SUITE in line and "inline:" in line), None)
    if not (address and media and key) or media.split()[2] != "RTP/SAVP":
        return None
    port, order = int(media.split()[1]), [int(p) for p in media.split()[3:] if p.isdigit()]
    names = {}
    for line in lines:
        if line.startswith("a=rtpmap:"):
            payload, _, rest = line[len("a=rtpmap:"):].partition(" ")
            name, _, rate = rest.partition("/")
            if payload.isdigit():
                names[int(payload)] = (name, int(rate.split("/")[0] or 8000))
    for payload in order:  # payloads statiques sans rtpmap (0 = PCMU)
        if payload == 0 and 0 not in names:
            names[0] = (PCMU, 8000)
    payloads = {p: names[p] for p in order if p in names and names[p][0].lower() in (OPUS, PCMU.lower())}
    events = next((p for p in order if names.get(p, ("",))[0] == EVENTS), None)
    return MediaOffer(address, port, key, {p: (OPUS if n.lower() == OPUS else PCMU, r) for p, (n, r) in payloads.items()},
                      events)


def choose_codec(theirs: MediaOffer) -> tuple[int, str, int] | None:
    """Premier codec commun dans l'ordre de l'autre côté (Opus préféré s'il le propose)."""
    for payload, (name, rate) in theirs.payloads.items():
        if name in (OPUS, PCMU):
            return payload, name, rate
    return None


# --- Codecs ------------------------------------------------------------------------------------------------

class Codec:
    """Codage d'un sens et d'un codec : PCM int16 (fréquence du codec) <-> charge utile RTP."""

    def __init__(self, name: str, rate: int):
        self.name, self.rate = name, rate
        self.samples = int(rate * FRAME_SECONDS)
        self._enc = self._dec = None
        if name == OPUS:
            self._enc = av.CodecContext.create("libopus", "w")
            self._enc.sample_rate, self._enc.layout, self._enc.format = 48000, "mono", "s16"
            self._enc.bit_rate = 32000
            self._enc.open()
            self._dec = av.CodecContext.create("libopus", "r")
            self._dec.sample_rate, self._dec.layout = 48000, "mono"
            self._dec.open()
            self._pts = 0

    def encode(self, pcm: np.ndarray) -> bytes:
        if self.name == PCMU:
            return ulaw_encode(pcm)
        frame = av.AudioFrame.from_ndarray(pcm.astype(np.int16).reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = 48000, self._pts, fractions.Fraction(1, 48000)
        self._pts += pcm.size
        return b"".join(bytes(packet) for packet in self._enc.encode(frame))

    def decode(self, payload: bytes) -> np.ndarray:
        if self.name == PCMU:
            return ulaw_decode(payload)
        packet = av.Packet(payload)
        frames = [f.to_ndarray() for f in self._dec.decode(packet)]
        if not frames:
            return np.zeros(0, np.int16)
        pcm = np.concatenate([f.reshape(f.shape[0], -1) for f in frames], axis=1)
        return (pcm.mean(axis=0) if pcm.shape[0] > 1 else pcm[0]).astype(np.int16)  # stéréo -> mono


class Resampler:
    """Rééchantillonneur PyAV gardé d'un morceau à l'autre (pas de clics aux jointures)."""

    def __init__(self, source: int, target: int):
        self.source, self.target = source, target
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=target) if source != target else None

    def __call__(self, pcm: np.ndarray) -> np.ndarray:
        if self._resampler is None or pcm.size == 0:
            return pcm.astype(np.int16)
        frame = av.AudioFrame.from_ndarray(pcm.astype(np.int16).reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = self.source
        out = [f.to_ndarray().reshape(-1) for f in self._resampler.resample(frame)]
        return np.concatenate(out).astype(np.int16) if out else np.zeros(0, np.int16)


def srtp(key_b64: str, outbound: bool) -> Session:
    return Session(policy=Policy(key=base64.b64decode(key_b64),
                                 ssrc_type=Policy.SSRC_ANY_OUTBOUND if outbound else Policy.SSRC_ANY_INBOUND,
                                 srtp_profile=Policy.SRTP_PROFILE_AES128_CM_SHA1_80))


# --- Son d'un appel ----------------------------------------------------------------------------------------

class SipMedia:
    """Son chiffré d'un appel SIP établi (codec choisi à la négociation)."""

    def __init__(self, sock: socket.socket, local_key: str, remote: MediaOffer, payload: int, codec: str, rate: int,
                 on_first_packet=None):
        self.sock, self.remote = sock, (remote.address, remote.port)
        self.payload = payload
        self.codec = Codec(codec, rate)
        self._tx, self._rx = srtp(local_key, True), srtp(remote.key, False)
        self._remote_key = remote.key
        self._to_codec_rate: Resampler | None = None
        self._from_codec = Resampler(rate if codec == PCMU else 48000, MIC_RATE)
        self.mic = ByteStream()
        self._out = np.zeros(0, np.int16)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._seq, self._ts = random.randrange(65536), random.randrange(2 ** 32)
        self._ssrc = random.randrange(2 ** 32)
        self._on_first = on_first_packet
        self.sent = self.received = self.rejected = 0
        self.first_received: float | None = None
        self.started = time.monotonic()
        self._threads = [threading.Thread(target=self._send_loop, name="rtp-envoi", daemon=True),
                         threading.Thread(target=self._recv_loop, name="rtp-reception", daemon=True)]
        for thread in self._threads:
            thread.start()

    @property
    def codec_name(self) -> str:
        return self.codec.name

    # --- Voix d'ORION ----------------------------------------------------------------------------------------

    def play(self, pcm: np.ndarray, rate: int) -> None:
        target = 48000 if self.codec.name == OPUS else self.codec.rate
        if self._to_codec_rate is None or self._to_codec_rate.source != rate:
            self._to_codec_rate = Resampler(rate, target)
        converted = self._to_codec_rate(np.asarray(pcm, dtype=np.int16))
        with self._lock:
            self._out = np.concatenate([self._out, converted])

    def pending(self) -> int:
        with self._lock:
            return int(self._out.size)

    def drain(self, timeout: float = 120.0) -> None:
        until = time.monotonic() + timeout
        while self.pending() and time.monotonic() < until and not self._stop.is_set():
            time.sleep(0.05)

    def stop_voice(self) -> None:
        with self._lock:
            self._out = np.zeros(0, np.int16)

    def update_remote(self, offer: "MediaOffer") -> bool:
        """L'autre côté relance l'appel (nouvelle adresse ou nouvelle clé du son) : on le suit. True si changé."""
        changed = False
        with self._lock:
            if (offer.address, offer.port) != self.remote:
                self.remote, changed = (offer.address, offer.port), True
            if offer.key != self._remote_key:
                self._rx, self._remote_key, changed = srtp(offer.key, False), offer.key, True
        if changed:
            log.info("Son de l'appel : adresse ou clé de l'autre côté mise à jour (relance de l'appel)")
        return changed

    def close(self) -> None:
        self._stop.set()
        self.mic.close()
        try:
            self.sock.close()
        except OSError:
            pass

    # --- Fils ------------------------------------------------------------------------------------------------

    def _send_loop(self) -> None:
        samples = int((48000 if self.codec.name == OPUS else self.codec.rate) * FRAME_SECONDS)
        start, n = time.monotonic(), 0
        while not self._stop.is_set():
            with self._lock:
                chunk, self._out = self._out[:samples], self._out[samples:]
            if chunk.size < samples:
                chunk = np.concatenate([chunk, np.zeros(samples - chunk.size, np.int16)])
            header = struct.pack("!BBHII", 0x80, (0x80 if n == 0 else 0) | self.payload, self._seq, self._ts,
                                 self._ssrc)
            try:
                self.sock.sendto(self._tx.protect(header + self.codec.encode(chunk)), self.remote)
                self.sent += 1
            except OSError:
                if self._stop.is_set():
                    return
            # Horodatage RTP : 48 000 Hz pour Opus (RFC 7587), 8 000 Hz pour G.711.
            self._seq, self._ts = (self._seq + 1) & 0xFFFF, (self._ts + samples) & 0xFFFFFFFF
            n += 1
            wait = start + n * FRAME_SECONDS - time.monotonic()
            if wait > 0:
                self._stop.wait(wait)

    def _recv_loop(self) -> None:
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, source = self.sock.recvfrom(4096)
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
            if source != self.remote:
                # Son symétrique : un paquet authentifié (notre clé SRTP) vient d'une autre adresse (relais ou
                # chemin réseau changé) : c'est là qu'on renvoie la voix d'ORION, sinon le téléphone n'entend plus
                # rien et raccroche au bout de 30 s.
                with self._lock:
                    self.remote = source
                log.info("Son de l'appel : l'autre côté a changé d'adresse, la voix d'ORION suit")
            if packet[1] & 0x7F != self.payload:
                continue  # touches, bruit de confort…
            if self.first_received is None:
                self.first_received = time.monotonic()
                if self._on_first is not None:
                    self._on_first()
            self.received += 1
            offset = 12 + 4 * (packet[0] & 0x0F)
            if packet[0] & 0x10 and len(packet) >= offset + 4:  # extension d'en-tête
                offset += 4 + 4 * struct.unpack("!H", packet[offset + 2:offset + 4])[0]
            try:
                pcm = self.codec.decode(packet[offset:])
            except Exception:  # noqa: BLE001 - trame illisible : sautée
                continue
            self.mic.write(self._from_codec(pcm).astype("<i2").tobytes())


def open_media_socket(bind_ip: str = "0.0.0.0") -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind_ip, 0))
    return sock
