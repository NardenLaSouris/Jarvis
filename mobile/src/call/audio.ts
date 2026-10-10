// Audio d'un appel : WebRTC entre ce téléphone et la passerelle d'ORION (réseau de la maison ou VPN, sans STUN ni
// TURN). Micro du téléphone vers ORION, voix d'ORION vers l'écouteur (écran éteint contre l'oreille) ou le
// haut-parleur, comme un appel téléphonique.

import { PermissionsAndroid, Platform } from 'react-native';
import InCallManager from 'react-native-incall-manager';
import { mediaDevices, MediaStream, RTCPeerConnection, RTCSessionDescription } from 'react-native-webrtc';

export type AudioState = 'connexion' | 'connecté' | 'perdu';

export interface Offer {
  sdp: string;
  type: string;
}

const GATHERING_TIMEOUT_MS = 3000; // candidats du réseau local : quasi immédiats

async function microphoneAllowed(): Promise<boolean> {
  if (Platform.OS !== 'android') {
    return true; // iOS : la demande est faite par le système au premier accès au micro
  }
  const result = await PermissionsAndroid.request(PermissionsAndroid.PERMISSIONS.RECORD_AUDIO, {
    title: 'Micro',
    message: 'ORION a besoin du micro pendant ses appels pour vous entendre.',
    buttonPositive: 'Autoriser',
    buttonNegative: 'Refuser',
  });
  return result === PermissionsAndroid.RESULTS.GRANTED;
}

function gatheringComplete(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === 'complete') {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, GATHERING_TIMEOUT_MS);
    pc.onicegatheringstatechange = () => {
      if (pc.iceGatheringState === 'complete') {
        clearTimeout(timer);
        resolve();
      }
    };
  });
}

export class CallAudio {
  private pc: RTCPeerConnection | null = null;
  private stream: MediaStream | null = null;

  constructor(private onState: (state: AudioState) => void) {}

  /** Ouvre l'audio : ``exchange`` envoie l'offre à la passerelle et rend sa réponse. */
  async start(exchange: (offer: Offer) => Promise<Offer>): Promise<void> {
    if (!(await microphoneAllowed())) {
      throw new Error("Micro refusé : autorisez-le dans les réglages pour parler à ORION.");
    }
    InCallManager.start({ media: 'audio' }); // mode communication : écouteur par défaut, capteur de proximité
    InCallManager.setForceSpeakerphoneOn(false);
    const pc = (this.pc = new RTCPeerConnection({ iceServers: [] }));
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'connected') {
        this.onState('connecté');
      } else if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') {
        this.onState('perdu');
      }
    };
    this.stream = await mediaDevices.getUserMedia({ audio: true, video: false });
    this.stream.getTracks().forEach((track) => pc.addTrack(track, this.stream!));
    await pc.setLocalDescription(await pc.createOffer({}));
    await gatheringComplete(pc); // offre complète (candidats inclus) : un seul échange avec la passerelle
    const local = pc.localDescription;
    if (!local) {
      throw new Error('Offre audio impossible.');
    }
    const answer = await exchange({ sdp: local.sdp, type: local.type ?? 'offer' });
    await pc.setRemoteDescription(new RTCSessionDescription(answer));
  }

  setSpeaker(on: boolean) {
    InCallManager.setForceSpeakerphoneOn(on);
  }

  setMuted(muted: boolean) {
    this.stream?.getAudioTracks().forEach((track) => {
      track.enabled = !muted;
    });
  }

  stop() {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    this.pc?.close();
    this.pc = null;
    InCallManager.stop();
  }
}
