// Application mobile ORION — étape actuelle : jumelage réel avec la passerelle d'appel et réception des appels tant
// que l'application est ouverte (sur le Wi-Fi de la maison). Audio simulé côté passerelle : pas encore de son.
// Suite : audio (WebRTC), puis sonnerie application fermée (FCM, écran d'appel natif).

import { Michroma_400Regular, useFonts } from '@expo-google-fonts/michroma';
import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useState } from 'react';
import { StyleSheet, View } from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';
import Svg, { Defs, RadialGradient, Rect, Stop } from 'react-native-svg';

import { HttpGateway, pairDevice } from './src/call/gateway';
import { move, type Call } from './src/call/states';
import { clearCredentials, loadCredentials, saveCredentials, type Credentials } from './src/call/storage';
import { EndedBanner, HomeScreen, IncomingCallScreen, InCallScreen, PairingScreen } from './src/ui/screens';
import { colors } from './src/ui/theme';

function Backdrop() {
  // Fond du visage d'ORION : vert très sombre au centre, noir profond sur les bords.
  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="none">
      <Svg width="100%" height="100%">
        <Defs>
          <RadialGradient id="backdrop" cx="50%" cy="42%" r="75%">
            <Stop offset="0" stopColor={colors.backgroundTop} />
            <Stop offset="1" stopColor={colors.background} />
          </RadialGradient>
        </Defs>
        <Rect width="100%" height="100%" fill="url(#backdrop)" />
      </Svg>
    </View>
  );
}

const END_TEXT: Record<string, string> = {
  annulé: "ORION a annulé l'appel",
  'raccroché par ORION': 'ORION a raccroché',
};

export default function App() {
  const [fontsLoaded] = useFonts({ Michroma_400Regular });
  const [credentials, setCredentials] = useState<Credentials | null | undefined>(undefined);
  const [gateway, setGateway] = useState<HttpGateway | null>(null);
  const [online, setOnline] = useState<boolean | null>(null);
  const [call, setCall] = useState<Call | null>(null);
  const [lastEnd, setLastEnd] = useState('');

  useEffect(() => {
    loadCredentials().then(setCredentials, () => setCredentials(null));
  }, []);

  const forget = useCallback(async () => {
    await clearCredentials();
    setCredentials(null);
    setCall(null);
  }, []);

  useEffect(() => {
    if (!credentials) {
      setGateway(null);
      return;
    }
    const client = new HttpGateway(credentials, () => {
      setLastEnd('Jumelage refusé par ORION : jumelez de nouveau ce téléphone.');
      void forget();
    });
    const offIncoming = client.onIncoming((incoming) => {
      setLastEnd('');
      setCall(incoming);
    });
    const offEnded = client.onEnded((callId, reason) => {
      setCall((current) => {
        if (!current || current.id !== callId) {
          return current;
        }
        setLastEnd(END_TEXT[reason] ?? `Appel ${reason}`);
        return null;
      });
    });
    const offStatus = client.onStatus(setOnline);
    client.start();
    setGateway(client);
    return () => {
      offIncoming();
      offEnded();
      offStatus();
      client.stop();
    };
  }, [credentials, forget]);

  const pair = async (url: string, code: string, name: string) => {
    try {
      const paired = await pairDevice(url, code, name);
      await saveCredentials(paired);
      setOnline(null);
      setLastEnd('');
      setCredentials(paired);
      return null;
    } catch (error) {
      return error instanceof Error ? error.message : 'Jumelage impossible.';
    }
  };

  const act = async (action: () => Promise<void>, after: () => void) => {
    try {
      await action();
      after();
    } catch {
      setCall(null);
      setLastEnd("L'appel n'est plus actif.");
    }
  };

  let screen = null;
  if (fontsLoaded && credentials !== undefined) {
    if (!credentials || !gateway) {
      screen = <PairingScreen onPair={pair} />;
    } else if (call && call.state === 'sonnerie') {
      screen = (
        <IncomingCallScreen
          call={call}
          onAccept={() => act(() => gateway.accept(call.id), () => setCall(move(move(call, 'accepté'), 'connecté')))}
          onRefuse={() =>
            act(() => gateway.refuse(call.id), () => {
              setCall(null);
              setLastEnd('Appel refusé');
            })
          }
        />
      );
    } else if (call) {
      screen = (
        <InCallScreen
          call={call}
          onHangUp={() =>
            act(() => gateway.hangUp(call.id), () => {
              setCall(null);
              setLastEnd('Appel terminé');
            })
          }
        />
      );
    } else {
      screen = <HomeScreen credentials={credentials} online={online} onForget={forget} />;
    }
  }

  return (
    <SafeAreaProvider>
      <Backdrop />
      <SafeAreaView style={styles.root}>
        {screen}
        {lastEnd ? <EndedBanner text={lastEnd} /> : null}
        <StatusBar style="light" />
      </SafeAreaView>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
});
