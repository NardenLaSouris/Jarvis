import { Ionicons } from '@expo/vector-icons';
import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, useWindowDimensions, View } from 'react-native';

import type { Pairing } from '../call/gateway';
import type { Call } from '../call/states';
import { Emblem } from './Emblem';
import { colors, fonts, rgba } from './theme';

// --- Éléments communs --------------------------------------------------------------------------------------

function Kicker({ children, color = colors.gold }: { children: string; color?: string }) {
  return <Text style={[styles.kicker, { color }]}>{children.toUpperCase()}</Text>;
}

function OutlineButton({ label, onPress, tone = 'gold' }: { label: string; onPress: () => void; tone?: 'gold' | 'quiet' }) {
  const color = tone === 'gold' ? colors.gold : colors.faint;
  return (
    <Pressable accessibilityRole="button" onPress={onPress}
      style={({ pressed }) => [styles.outline, { borderColor: color, opacity: pressed ? 0.6 : 1 }]}>
      <Text style={[styles.outlineText, { color: tone === 'gold' ? colors.cream : colors.muted }]}>
        {label.toUpperCase()}
      </Text>
    </Pressable>
  );
}

function RoundButton({ icon, label, color, rotate, onPress }: {
  icon: keyof typeof Ionicons.glyphMap;
  label: string;
  color: string;
  rotate?: string;
  onPress: () => void;
}) {
  return (
    <View style={styles.roundWrap}>
      <Pressable accessibilityRole="button" accessibilityLabel={label} onPress={onPress}
        style={({ pressed }) => [styles.round, { backgroundColor: color, opacity: pressed ? 0.75 : 1 }]}>
        <Ionicons name={icon} size={30} color={colors.cream} style={rotate ? { transform: [{ rotate }] } : null} />
      </Pressable>
      <Text style={styles.roundLabel}>{label.toUpperCase()}</Text>
    </View>
  );
}

function Field({ label, ...input }: { label: string } & React.ComponentProps<typeof TextInput>) {
  const [focused, setFocused] = useState(false);
  return (
    <View style={styles.field}>
      <Text style={styles.fieldLabel}>{label.toUpperCase()}</Text>
      <TextInput {...input} onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
        placeholderTextColor={colors.faint} selectionColor={colors.gold}
        style={[styles.input, { borderBottomColor: focused ? colors.gold : colors.line }]} />
    </View>
  );
}

function useEmblemSize(ratio: number) {
  const { width, height } = useWindowDimensions();
  return Math.min(width * ratio, height * 0.48);
}

// --- Écrans ------------------------------------------------------------------------------------------------

export function PairingScreen({ onPaired }: { onPaired: (pairing: Pairing) => void }) {
  const [gateway, setGateway] = useState('');
  const [deviceName, setDeviceName] = useState('Mon téléphone');
  const size = useEmblemSize(0.55);
  // Étape suivante : code de jumelage à usage unique (QR code affiché par ORION), jeton gardé dans le coffre du
  // téléphone. Ici, seule l'adresse est notée, pour tester l'interface.
  return (
    <View style={styles.screen}>
      <View style={styles.hero}>
        <Emblem size={size} />
        <Kicker>Jumelage</Kicker>
        <Text style={styles.lead}>Reliez ce téléphone à votre assistant.</Text>
      </View>
      <View style={styles.form}>
        <Field label="Passerelle d'appel" value={gateway} onChangeText={setGateway}
          placeholder="https://192.168.1.91:8770" autoCapitalize="none" autoCorrect={false} keyboardType="url" />
        <Field label="Nom de cet appareil" value={deviceName} onChangeText={setDeviceName} />
        <OutlineButton label="Jumeler" onPress={() => onPaired({ gateway: gateway.trim(), deviceName: deviceName.trim() })} />
      </View>
    </View>
  );
}

export function HomeScreen({ pairing, onTestCall }: { pairing: Pairing; onTestCall: () => void }) {
  const size = useEmblemSize(0.82);
  return (
    <View style={styles.screen}>
      <View style={styles.hero}>
        <Emblem size={size} />
        <View style={styles.status}>
          <View style={styles.dot} />
          <Kicker color={colors.mint}>En veille</Kicker>
        </View>
        <Text style={styles.lead}>{pairing.deviceName || 'Ce téléphone'} est prêt à recevoir les appels d'ORION.</Text>
        <Text style={styles.small}>{pairing.gateway || 'passerelle non renseignée'}</Text>
      </View>
      <View style={styles.footer}>
        <OutlineButton label="Simuler un appel" tone="quiet" onPress={onTestCall} />
      </View>
    </View>
  );
}

export function IncomingCallScreen({ call, onAccept, onRefuse }: {
  call: Call;
  onAccept: () => void;
  onRefuse: () => void;
}) {
  const size = useEmblemSize(0.82);
  return (
    <View style={styles.screen}>
      <View style={styles.hero}>
        <Kicker>Appel entrant</Kicker>
        <Emblem size={size} mode="sonnerie" />
        <Text style={styles.reason}>{call.reason}</Text>
      </View>
      <View style={styles.actions}>
        <RoundButton icon="call" label="Refuser" color={colors.refuse} rotate="135deg" onPress={onRefuse} />
        <RoundButton icon="call" label="Décrocher" color={colors.green} onPress={onAccept} />
      </View>
    </View>
  );
}

export function InCallScreen({ call, onHangUp }: { call: Call; onHangUp: () => void }) {
  const [seconds, setSeconds] = useState(0);
  const size = useEmblemSize(0.82);
  useEffect(() => {
    const timer = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  const clock = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  const online = call.state === 'connecté';
  return (
    <View style={styles.screen}>
      <View style={styles.hero}>
        <View style={styles.status}>
          <View style={[styles.dot, !online && { backgroundColor: colors.gold }]} />
          <Kicker color={online ? colors.mint : colors.gold}>{online ? 'En ligne' : 'Connexion'}</Kicker>
        </View>
        <Emblem size={size} mode="appel" />
        <Text style={styles.clock}>{clock}</Text>
      </View>
      <View style={styles.actions}>
        <RoundButton icon="call" label="Raccrocher" color={colors.refuse} rotate="135deg" onPress={onHangUp} />
      </View>
    </View>
  );
}

export function EndedBanner({ text }: { text: string }) {
  return <Text style={styles.banner}>{text}</Text>;
}

const styles = StyleSheet.create({
  screen: { flex: 1, justifyContent: 'space-between', paddingHorizontal: 28, paddingVertical: 24 },
  hero: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 18 },
  form: { gap: 22, paddingBottom: 12 },
  footer: { paddingBottom: 12 },
  actions: { flexDirection: 'row', justifyContent: 'space-evenly', paddingBottom: 28 },
  kicker: { fontFamily: fonts.display, fontSize: 12, letterSpacing: 4, paddingLeft: 4 },
  lead: { color: colors.cream, fontSize: 15, lineHeight: 22, textAlign: 'center', opacity: 0.85, maxWidth: 300 },
  small: { color: colors.faint, fontSize: 12, letterSpacing: 0.5 },
  reason: { color: colors.cream, fontSize: 18, lineHeight: 26, textAlign: 'center', maxWidth: 320 },
  clock: {
    fontFamily: fonts.display, color: colors.gold, fontSize: 26, letterSpacing: 4, paddingLeft: 4,
    textShadowColor: rgba(colors.gold, 0.6), textShadowRadius: 10,
  },
  status: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  dot: { width: 7, height: 7, borderRadius: 4, backgroundColor: colors.mint },
  field: { gap: 8 },
  fieldLabel: { fontFamily: fonts.display, color: colors.gold, fontSize: 10, letterSpacing: 3, opacity: 0.85 },
  input: { color: colors.cream, fontSize: 16, paddingVertical: 8, borderBottomWidth: 1 },
  outline: { borderWidth: 1, borderRadius: 999, paddingVertical: 15, alignItems: 'center' },
  outlineText: { fontFamily: fonts.display, fontSize: 12, letterSpacing: 4, paddingLeft: 4 },
  roundWrap: { alignItems: 'center', gap: 12 },
  round: {
    width: 74, height: 74, borderRadius: 37, alignItems: 'center', justifyContent: 'center',
    borderWidth: 1, borderColor: rgba(colors.cream, 0.15),
  },
  roundLabel: { fontFamily: fonts.display, color: colors.muted, fontSize: 10, letterSpacing: 3, paddingLeft: 3 },
  banner: { color: colors.muted, textAlign: 'center', paddingBottom: 10, fontSize: 13, letterSpacing: 0.5 },
});
