// Lien avec la passerelle d'appel d'ORION (orion-call, sur le mini-PC, jointe par le réseau de la maison ou le VPN).
// Étape actuelle : l'application reçoit les appels tant qu'elle est ouverte (attente longue sur /app/events) ; la
// sonnerie application fermée (FCM, écran d'appel natif) et l'audio (WebRTC) viendront ensuite, derrière la même
// interface.

import type { Call } from './states';
import type { Credentials } from './storage';

export interface CallGateway {
  onIncoming(listener: (call: Call) => void): () => void;
  onEnded(listener: (callId: string, reason: string) => void): () => void;
  onStatus(listener: (online: boolean) => void): () => void;
  accept(callId: string): Promise<void>;
  refuse(callId: string): Promise<void>;
  hangUp(callId: string): Promise<void>;
  start(): void;
  stop(): void;
}

export class GatewayError extends Error {}

const WAIT_S = 25;
const RETRY_MS = 5000;

function normalizeUrl(url: string): string {
  const trimmed = url.trim().replace(/\/+$/, '');
  return /^https?:\/\//i.test(trimmed) ? trimmed : `http://${trimmed}`;
}

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    return body.message || body.error || `erreur ${response.status}`;
  } catch {
    return `erreur ${response.status}`;
  }
}

/** Échange un code de jumelage (affiché par ORION) contre l'identité de ce téléphone. */
export async function pairDevice(gatewayUrl: string, code: string, name: string): Promise<Credentials> {
  const gateway = normalizeUrl(gatewayUrl);
  let response: Response;
  try {
    response = await fetch(`${gateway}/app/pair`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, name }),
    });
  } catch {
    throw new GatewayError("Passerelle injoignable : vérifiez l'adresse et le Wi-Fi de la maison.");
  }
  if (!response.ok) {
    throw new GatewayError(response.status === 403 ? await readError(response) : `Jumelage refusé (${response.status}).`);
  }
  const body = await response.json();
  return { gateway, deviceId: body.device_id, token: body.token, name: body.name };
}

type Listener<T extends unknown[]> = (...args: T) => void;

export class HttpGateway implements CallGateway {
  private incoming = new Set<Listener<[Call]>>();
  private ended = new Set<Listener<[string, string]>>();
  private status = new Set<Listener<[boolean]>>();
  private running = false;
  private abort: AbortController | null = null;
  private online: boolean | null = null;

  constructor(private credentials: Credentials, private onUnauthorized: () => void = () => {}) {}

  onIncoming(listener: Listener<[Call]>) {
    this.incoming.add(listener);
    return () => this.incoming.delete(listener);
  }

  onEnded(listener: Listener<[string, string]>) {
    this.ended.add(listener);
    return () => this.ended.delete(listener);
  }

  onStatus(listener: Listener<[boolean]>) {
    this.status.add(listener);
    return () => this.status.delete(listener);
  }

  private headers() {
    return {
      'Content-Type': 'application/json',
      'X-Device': this.credentials.deviceId,
      Authorization: `Bearer ${this.credentials.token}`,
    };
  }

  private setOnline(online: boolean) {
    if (online !== this.online) {
      this.online = online;
      this.status.forEach((listener) => listener(online));
    }
  }

  start() {
    if (this.running) {
      return;
    }
    this.running = true;
    void this.loop();
  }

  stop() {
    this.running = false;
    this.abort?.abort();
  }

  private async loop() {
    while (this.running) {
      this.abort = new AbortController();
      try {
        const response = await fetch(`${this.credentials.gateway}/app/events?timeout=${WAIT_S}`, {
          headers: this.headers(),
          signal: this.abort.signal,
        });
        if (response.status === 401) {
          this.running = false;
          this.onUnauthorized();
          return;
        }
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        this.setOnline(true);
        const { events } = (await response.json()) as { events: Array<Record<string, string>> };
        for (const event of events) {
          if (event.type === 'incoming') {
            const call: Call = { id: event.call_id, reason: event.reason ?? '', state: 'sonnerie' };
            this.incoming.forEach((listener) => listener(call));
          } else if (event.type === 'ended') {
            this.ended.forEach((listener) => listener(event.call_id, event.reason ?? 'terminé'));
          }
        }
      } catch {
        if (!this.running) {
          return;
        }
        this.setOnline(false);
        await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
      }
    }
  }

  private async act(callId: string, action: 'accept' | 'refuse' | 'hangup') {
    const response = await fetch(`${this.credentials.gateway}/app/calls/${encodeURIComponent(callId)}/${action}`, {
      method: 'POST',
      headers: this.headers(),
      body: '{}',
    });
    if (!response.ok) {
      throw new GatewayError(await readError(response));
    }
  }

  accept(callId: string) {
    return this.act(callId, 'accept');
  }

  refuse(callId: string) {
    return this.act(callId, 'refuse');
  }

  hangUp(callId: string) {
    return this.act(callId, 'hangup');
  }
}
