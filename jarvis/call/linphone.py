"""Linphone comme appareil de la passerelle d'appel : l'appareil « linphone » est votre adresse Linphone.

- Le Core appelle « linphone » : la passerelle compose votre adresse (jarvis/call/sip.py) ; sonnerie, décroché,
  refus ou silence sont rendus au Core comme pour l'application (accepted, connected, refused, failed).
- Vous appelez ORION depuis Linphone : la passerelle prévient le Core (« dialed ») ; il décroche (``answer``) ou
  raccroche, comme pour un appel de l'application.
- Le son est chiffré (SRTP, jarvis/call/rtp.py) et le Core le lit par le même protocole que l'agent audio.
Seule votre adresse peut être appelée ou appeler (vérifié par le compte SIP, avant la passerelle et le Core).
"""

from __future__ import annotations

import logging
import threading

from jarvis.call.rtp import OFFER_PAYLOADS, SipMedia, build_sdp, choose_codec, new_key, open_media_socket, parse_sdp
from jarvis.call.sip import DEVICE, Dialog, SipAccount

log = logging.getLogger(__name__)

__all__ = ["DEVICE", "LinphoneChannel"]
REFUSED_CODES = (486, 600, 603)  # occupé, refusé partout, refusé


class LinphoneChannel:
    def __init__(self, account: SipAccount, gateway):
        self.account = account
        self.gateway = gateway
        account.on_incoming = self._incoming
        account.on_ended = self._remote_ended

    # --- Le Core appelle votre adresse ------------------------------------------------------------------

    def ring(self, call) -> bool:
        if not self.account.registered:
            log.warning("Passerelle : compte Linphone non enregistré, appel %s impossible", call.id)
            return False
        call.cancel = threading.Event()
        threading.Thread(target=self._dial_out, args=(call,), name="appel-linphone", daemon=True).start()
        return True

    def _dial_out(self, call) -> None:
        sock, key = open_media_socket(), new_key()
        sdp = build_sdp(self.account.local_ip, sock.getsockname()[1], key, OFFER_PAYLOADS)
        dialog, code = self.account.invite(sdp, cancel=call.cancel)
        if call.state == "terminé":  # le Core a annulé ou raccroché pendant la sonnerie
            sock.close()
            if dialog is not None:
                self.account.hang_up(dialog)
            return
        if dialog is None:
            sock.close()
            if code in REFUSED_CODES:
                self.gateway.sip_refused(call)
            else:
                self.gateway.sip_failed(call, "pas de réponse" if code in (0, 408, 480) else f"refus SIP {code}")
            return
        remote = parse_sdp(dialog.remote_sdp)
        chosen = choose_codec(remote) if remote is not None else None
        if chosen is None:
            sock.close()
            self.account.hang_up(dialog)
            self.gateway.sip_failed(call, "son non chiffré ou codec inconnu")
            return
        payload, codec, rate = chosen
        call.sip = dialog
        call.media = SipMedia(sock, key, remote, payload, codec, rate)
        log.info("Passerelle : appel %s décroché sur Linphone (%s)", call.id, codec)
        self.gateway.sip_answered(call)

    # --- Vous appelez ORION -------------------------------------------------------------------------------

    def _incoming(self, dialog: Dialog) -> None:
        call, refusal = self.gateway.sip_dialed(dialog)
        if call is None:
            log.info("Passerelle : appel Linphone refusé (%s)", refusal)
            self.account.reject(dialog, 486, "Busy Here")

    def answer(self, call) -> bool:
        """Le Core décroche : ORION répond avec son son chiffré (codec choisi parmi ceux de votre offre)."""
        dialog: Dialog = call.sip
        remote = parse_sdp(dialog.remote_sdp)
        chosen = choose_codec(remote) if remote is not None else None
        if chosen is None:
            self.account.reject(dialog, 488, "Not Acceptable Here")
            return False
        payload, codec, rate = chosen
        sock, key = open_media_socket(), new_key()
        sdp = build_sdp(self.account.local_ip, sock.getsockname()[1], key, {payload: (codec, rate)}, remote.events)
        call.media = SipMedia(sock, key, remote, payload, codec, rate)
        self.account.answer(dialog, sdp)
        log.info("Passerelle : appel %s de Linphone décroché par ORION (%s)", call.id, codec)
        return True

    # --- Fin ----------------------------------------------------------------------------------------------

    def end(self, call) -> None:
        """Le Core (ou un délai) met fin à l'appel : annulation pendant la sonnerie, raccrochage sinon."""
        cancel = getattr(call, "cancel", None)
        if cancel is not None:
            cancel.set()
        dialog = getattr(call, "sip", None)
        if dialog is not None and not dialog.ended:
            if dialog.outgoing or dialog.local_sdp:
                self.account.hang_up(dialog)
            else:  # appel entrant pas encore décroché
                self.account.reject(dialog, 480, "Temporarily Unavailable")
        media = getattr(call, "media", None)
        if media is not None:
            media.close()

    def _remote_ended(self, dialog: Dialog, why: str) -> None:
        self.gateway.sip_ended(dialog, why)
