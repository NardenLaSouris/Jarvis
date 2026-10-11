"""Son des appels Linphone (jarvis/call/rtp.py) : description du son (SDP), choix du codec, son chiffré de bout en
bout en local (deux sessions SRTP sur la même machine), en Opus puis en G.711.

Environnement de la passerelle seulement (pylibsrtp, PyAV) ; ignoré dans celui d'ORION :
    ~/orion-call-venv/bin/python -m pytest tests/test_call_rtp.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("pylibsrtp")
pytest.importorskip("av")

from jarvis.call.rtp import (  # noqa: E402
    OFFER_PAYLOADS, OPUS, PCMU, MediaOffer, SipMedia, build_sdp, choose_codec, new_key, open_media_socket, parse_sdp,
    ulaw_decode, ulaw_encode,
)

IPHONE_ANSWER = ("v=0\r\no=jules 1 1 IN IP6 ::1\r\ns=Talk\r\nc=IN IP4 203.0.113.5\r\nt=0 0\r\n"
                 "m=audio 18912 RTP/SAVP 0 101\r\na=rtpmap:101 telephone-event/8000\r\n"
                 "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:kPhcb+mOGyLyd6DSSESoxhzsf66dJWNji7jof7I3\r\n"
                 "a=rtcp:18913\r\n")


def test_g711_round_trip_is_close():
    pcm = (np.sin(np.arange(8000) / 8000 * 2 * np.pi * 440) * 12000).astype(np.int16)
    assert np.abs(ulaw_decode(ulaw_encode(pcm)).astype(int) - pcm).max() < 400


def test_orion_offer_prefers_opus_and_parses_back():
    key = new_key()
    sdp = build_sdp("192.0.2.1", 40000, key, OFFER_PAYLOADS)
    offer = parse_sdp(sdp)
    assert offer.address == "192.0.2.1" and offer.port == 40000 and offer.key == key
    assert list(offer.payloads) == [96, 0] and offer.payloads[96] == (OPUS, 48000) and offer.events == 101
    assert choose_codec(offer) == (96, OPUS, 48000)


def test_iphone_answer_seen_during_the_real_test_is_understood():
    answer = parse_sdp(IPHONE_ANSWER)
    assert answer.address == "203.0.113.5" and answer.port == 18912 and answer.events == 101
    assert choose_codec(answer) == (0, PCMU, 8000)  # PCMU statique, sans rtpmap


def test_unencrypted_audio_is_refused():
    assert parse_sdp(IPHONE_ANSWER.replace("RTP/SAVP", "RTP/AVP")) is None
    assert parse_sdp(IPHONE_ANSWER.replace("AES_CM_128_HMAC_SHA1_80", "AES_256_CM_HMAC_SHA1_80")) is None


@pytest.mark.parametrize("payload, codec, rate", [(96, OPUS, 48000), (0, PCMU, 8000)])
def test_encrypted_audio_flows_both_ways_and_reaches_the_core_at_16_khz(payload, codec, rate):
    a_sock, b_sock = open_media_socket("127.0.0.1"), open_media_socket("127.0.0.1")
    a_key, b_key = new_key(), new_key()
    a = SipMedia(a_sock, a_key, MediaOffer("127.0.0.1", b_sock.getsockname()[1], b_key, {payload: (codec, rate)}),
                 payload, codec, rate)
    b = SipMedia(b_sock, b_key, MediaOffer("127.0.0.1", a_sock.getsockname()[1], a_key, {payload: (codec, rate)}),
                 payload, codec, rate)
    try:
        tone = (np.sin(np.arange(22050) / 22050 * 2 * np.pi * 440) * 12000).astype(np.int16)  # 1 s de voix d'ORION
        a.play(tone, 22050)
        time.sleep(1.4)
        heard = b""  # durée fixe : le silence entre les phrases arrive aussi (un paquet toutes les 20 ms, sans fin)
        for _ in range(14):  # 14 blocs de 0,1 s à 16 kHz
            heard += b.mic.read(3200, timeout=0.5) or b""
        pcm = np.frombuffer(heard, dtype="<i2")
        assert b.received > 30 and b.rejected == 0 and a.sent > 30
        assert len(pcm) > 8000 and np.abs(pcm).max() > 4000  # le son est arrivé, à 16 kHz (côté Core)
        assert a.codec_name == codec
    finally:
        a.close()
        b.close()
