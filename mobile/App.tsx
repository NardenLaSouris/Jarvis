// Application mobile ORION — étape actuelle : interface d'appel avec une passerelle simulée (aucun module natif).
// Suite : jumelage réel, passerelle orion-call (signalisation, WebRTC), puis sonnerie native (FCM, écran d'appel).

import { StatusBar } from 'expo-status-bar';
import { useEffect, useMemo, useState } from 'react';
import { StyleSheet } from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';

import { MockGateway, type Pairing } from './src/call/gateway';
import { move, type Call } from './src/call/states';
import { EndedBanner, HomeScreen, IncomingCallScreen, InCallScreen, PairingScreen } from './src/ui/screens';
import { colors } from './src/ui/theme';

export default function App() {
  const gateway = useMemo(() => new MockGateway(), []);
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [call, setCall] = useState<Call | null>(null);
  const [lastEnd, setLastEnd] = useState('');

  useEffect(() => {
    const offIncoming = gateway.onIncoming((incoming) => {
      setLastEnd('');
      setCall(incoming);
    });
    const offEnded = gateway.onEnded((callId, reason) => {
      setCall((current) => {
        if (!current || current.id !== callId) {
          return current;
        }
        setLastEnd(`Appel ${reason}`);
        return null;
      });
    });
    return () => {
      offIncoming();
      offEnded();
    };
  }, [gateway]);

  const finish = (text: string) => {
    setLastEnd(text);
    setCall(null);
  };

  let screen;
  if (!pairing) {
    screen = <PairingScreen onPaired={setPairing} />;
  } else if (call && call.state === 'sonnerie') {
    screen = (
      <IncomingCallScreen
        call={call}
        onAccept={async () => {
          await gateway.accept(call.id);
          setCall(move(move(call, 'accepté'), 'connecté'));
        }}
        onRefuse={async () => {
          await gateway.refuse(call.id);
          finish('Appel refusé');
        }}
      />
    );
  } else if (call) {
    screen = (
      <InCallScreen
        call={call}
        onHangUp={async () => {
          await gateway.hangUp(call.id);
          finish('Appel terminé');
        }}
      />
    );
  } else {
    screen = <HomeScreen pairing={pairing} onTestCall={() => gateway.simulateIncoming()} />;
  }

  return (
    <SafeAreaProvider>
      <SafeAreaView style={styles.root}>
        {screen}
        {lastEnd ? <EndedBanner text={lastEnd} /> : null}
        <StatusBar style="light" />
      </SafeAreaView>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.background },
});
