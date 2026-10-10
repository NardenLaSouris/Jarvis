// Lien avec la passerelle d'appel d'ORION (orion-call, sur le mini-PC, joint par le VPN de la maison).
// Étape actuelle : une passerelle simulée, pour l'interface. La vraie (jumelage, signalisation, WebRTC) la remplacera
// derrière la même interface.

import type { Call } from './states';

export interface Pairing {
  gateway: string; // ex. https://192.168.1.91:8770 (réseau local ou VPN)
  deviceName: string;
}

export interface CallGateway {
  /** Appel entrant annoncé par la passerelle (plus tard : par une notification FCM). */
  onIncoming(listener: (call: Call) => void): () => void;
  /** La passerelle annonce une fin (expiré, raccroché par ORION, réseau). */
  onEnded(listener: (callId: string, reason: string) => void): () => void;
  accept(callId: string): Promise<void>;
  refuse(callId: string): Promise<void>;
  hangUp(callId: string): Promise<void>;
}

const RING_TIMEOUT_MS = 40_000; // comme le Core ([call] ring_timeout)

/** Passerelle simulée : « simulateIncoming » fait sonner comme le ferait ORION. */
export class MockGateway implements CallGateway {
  private incoming = new Set<(call: Call) => void>();
  private ended = new Set<(callId: string, reason: string) => void>();
  private timers = new Map<string, ReturnType<typeof setTimeout>>();

  onIncoming(listener: (call: Call) => void) {
    this.incoming.add(listener);
    return () => this.incoming.delete(listener);
  }

  onEnded(listener: (callId: string, reason: string) => void) {
    this.ended.add(listener);
    return () => this.ended.delete(listener);
  }

  simulateIncoming(reason = 'Ceci est un appel de test.') {
    const call: Call = { id: `test-${Date.now()}`, reason, state: 'sonnerie' };
    this.timers.set(call.id, setTimeout(() => this.end(call.id, 'sans réponse'), RING_TIMEOUT_MS));
    this.incoming.forEach((listener) => listener(call));
  }

  async accept(callId: string) {
    this.clear(callId);
  }

  async refuse(callId: string) {
    this.clear(callId);
  }

  async hangUp(callId: string) {
    this.clear(callId);
  }

  private end(callId: string, reason: string) {
    this.clear(callId);
    this.ended.forEach((listener) => listener(callId, reason));
  }

  private clear(callId: string) {
    const timer = this.timers.get(callId);
    if (timer) {
      clearTimeout(timer);
      this.timers.delete(callId);
    }
  }
}
