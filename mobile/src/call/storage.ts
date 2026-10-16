// Jumelage gardé dans le coffre du téléphone (Keystore Android, Trousseau iOS) : le jeton n'est jamais écrit ailleurs.

import * as SecureStore from 'expo-secure-store';

export interface Credentials {
  gateway: string; // ex. http://192.168.1.91:8770
  deviceId: string;
  token: string;
  name: string;
}

const KEY = 'orion.pairing';

export async function loadCredentials(): Promise<Credentials | null> {
  const raw = await SecureStore.getItemAsync(KEY);
  if (!raw) {
    return null;
  }
  try {
    const data = JSON.parse(raw) as Credentials;
    return data.gateway && data.deviceId && data.token ? data : null;
  } catch {
    return null;
  }
}

export async function saveCredentials(credentials: Credentials): Promise<void> {
  await SecureStore.setItemAsync(KEY, JSON.stringify(credentials));
}

export async function clearCredentials(): Promise<void> {
  await SecureStore.deleteItemAsync(KEY);
}
