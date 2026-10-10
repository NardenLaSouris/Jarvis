import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import type { Call } from '../call/states';
import type { Pairing } from '../call/gateway';
import { colors } from './theme';

function Button({ label, color, onPress }: { label: string; color: string; onPress: () => void }) {
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.button, { backgroundColor: color, opacity: pressed ? 0.7 : 1 }]}>
      <Text style={styles.buttonText}>{label}</Text>
    </Pressable>
  );
}

export function PairingScreen({ onPaired }: { onPaired: (pairing: Pairing) => void }) {
  const [gateway, setGateway] = useState('');
  const [deviceName, setDeviceName] = useState('Mon téléphone');
  // Étape suivante : code de jumelage à usage unique (QR code affiché par ORION), jeton gardé dans le coffre du
  // téléphone. Ici, seule l'adresse est notée, pour tester l'interface.
  return (
    <View style={styles.screen}>
      <Text style={styles.title}>ORION</Text>
      <Text style={styles.subtitle}>Jumelage avec votre assistant</Text>
      <Text style={styles.label}>Adresse de la passerelle d'appel</Text>
      <TextInput
        style={styles.input}
        value={gateway}
        onChangeText={setGateway}
        placeholder="https://192.168.1.91:8770"
        placeholderTextColor={colors.muted}
        autoCapitalize="none"
        autoCorrect={false}
        keyboardType="url"
      />
      <Text style={styles.label}>Nom de cet appareil</Text>
      <TextInput style={styles.input} value={deviceName} onChangeText={setDeviceName} />
      <Button label="Jumeler" color={colors.accent} onPress={() => onPaired({ gateway: gateway.trim(), deviceName })} />
    </View>
  );
}

export function HomeScreen({ pairing, onTestCall }: { pairing: Pairing; onTestCall: () => void }) {
  return (
    <View style={styles.screen}>
      <Text style={styles.title}>ORION</Text>
      <Text style={styles.subtitle}>{pairing.deviceName} — en attente d'un appel</Text>
      <Text style={styles.muted}>{pairing.gateway || 'passerelle non renseignée'}</Text>
      <Button label="Simuler un appel d'ORION" color={colors.surface} onPress={onTestCall} />
    </View>
  );
}

export function IncomingCallScreen({ call, onAccept, onRefuse }: {
  call: Call;
  onAccept: () => void;
  onRefuse: () => void;
}) {
  return (
    <View style={styles.screen}>
      <Text style={styles.muted}>Appel entrant</Text>
      <Text style={styles.title}>ORION</Text>
      <Text style={styles.reason}>{call.reason}</Text>
      <View style={styles.row}>
        <Button label="Refuser" color={colors.refuse} onPress={onRefuse} />
        <Button label="Décrocher" color={colors.accept} onPress={onAccept} />
      </View>
    </View>
  );
}

export function InCallScreen({ call, onHangUp }: { call: Call; onHangUp: () => void }) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  const clock = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  return (
    <View style={styles.screen}>
      <Text style={styles.muted}>{call.state === 'connecté' ? 'En ligne' : 'Connexion…'}</Text>
      <Text style={styles.title}>ORION</Text>
      <Text style={styles.clock}>{clock}</Text>
      <Button label="Raccrocher" color={colors.refuse} onPress={onHangUp} />
    </View>
  );
}

export function EndedBanner({ text }: { text: string }) {
  return <Text style={styles.banner}>{text}</Text>;
}

const styles = StyleSheet.create({
  screen: { flex: 1, justifyContent: 'center', padding: 24, gap: 16 },
  title: { color: colors.text, fontSize: 40, fontWeight: '700', letterSpacing: 6, textAlign: 'center' },
  subtitle: { color: colors.text, fontSize: 16, textAlign: 'center' },
  muted: { color: colors.muted, fontSize: 14, textAlign: 'center' },
  reason: { color: colors.text, fontSize: 18, textAlign: 'center', marginVertical: 24 },
  clock: { color: colors.accent, fontSize: 28, textAlign: 'center', marginVertical: 24 },
  label: { color: colors.muted, fontSize: 13 },
  input: {
    backgroundColor: colors.surface, borderColor: colors.border, borderWidth: 1, borderRadius: 10,
    color: colors.text, paddingHorizontal: 14, paddingVertical: 12, fontSize: 16,
  },
  row: { flexDirection: 'row', justifyContent: 'space-between', gap: 16 },
  button: { flexGrow: 1, borderRadius: 999, paddingVertical: 16, alignItems: 'center', borderWidth: 1,
    borderColor: colors.border },
  buttonText: { color: colors.text, fontSize: 16, fontWeight: '600' },
  banner: { color: colors.muted, textAlign: 'center', padding: 12 },
});
