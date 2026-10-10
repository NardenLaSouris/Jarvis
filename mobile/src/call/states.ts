// États d'un appel d'ORION, alignés sur le Core (jarvis/call/session.py) : mêmes noms, mêmes transitions.
// L'application ne décide jamais seule d'un état final côté serveur : elle annonce « accepté », « refusé » ou
// « raccroché », et le Core tranche.

export type CallState =
  | 'demandé'
  | 'sonnerie'
  | 'accepté'
  | 'connecté'
  | 'terminé'
  | 'refusé'
  | 'expiré'
  | 'échec';

export const FINAL_STATES: readonly CallState[] = ['terminé', 'refusé', 'expiré', 'échec'];

const TRANSITIONS: Record<CallState, readonly CallState[]> = {
  demandé: ['sonnerie', 'échec', 'terminé'],
  sonnerie: ['accepté', 'refusé', 'expiré', 'échec', 'terminé'],
  accepté: ['connecté', 'échec', 'terminé'],
  connecté: ['terminé', 'échec'],
  terminé: [],
  refusé: [],
  expiré: [],
  échec: [],
};

export interface Call {
  id: string;
  reason: string;
  state: CallState;
  endReason?: string;
}

export function canMove(from: CallState, to: CallState): boolean {
  return TRANSITIONS[from].includes(to);
}

export function isFinal(state: CallState): boolean {
  return FINAL_STATES.includes(state);
}

/** Nouvel état, ou l'appel inchangé si la transition n'est pas prévue (message en retard ou rejoué). */
export function move(call: Call, to: CallState, endReason?: string): Call {
  if (!canMove(call.state, to)) {
    return call;
  }
  return isFinal(to) ? { ...call, state: to, endReason: endReason ?? to } : { ...call, state: to };
}
