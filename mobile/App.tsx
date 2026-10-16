// Application mobile ORION — étape actuelle : jumelage réel, appels reçus tant que l'application est ouverte (Wi-Fi
// de la maison), audio WebRTC avec la passerelle. Suite : sonnerie application fermée (FCM, écran d'appel natif).

import { Michroma_400Regular, useFonts } from '@expo-google-fonts/michroma';
import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useRef, useState } from 'react';
import { StyleSheet, View } from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';
import Svg, { Defs, RadialGradient, Rect, Stop } from 'react-native-svg';

import { CallAudio } from './src/call/audio';
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
  const [muted, setMuted] = useState(false);
  const [speaker, setSpeaker] = useState(false);
  const audio = useRef<CallAudio | null>(null);
  const callRef = useRef<Call | null>(null); // appel en cours, lu par les écouteurs de la passerelle
  useEffect(() => {
    callRef.current = call;
  }, [call]);

  const stopAudio = useCallback(() => {
    audio.current?.stop();
    audio.current = null;
    setMuted(false);
    setSpeaker(false);
  }, []);

  useEffect(() => {
    loadCredentials().then(setCredentials, () => setCredentials(null));
  }, []);

  const forget = useCallback(async () => {
    stopAudio();
    await clearCredentials();
    setCredentials(null);
    setCall(null);
  }, [stopAudio]);

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
        stopAudio();
        setLastEnd(END_TEXT[reason] ?? `Appel ${reason}`);
        return null;
      });
    });
    const offStatus = client.onStatus(setOnline);
    const offAnswered = client.onAnswered((callId) => {
      const current = callRef.current;
      if (current && current.id === callId && current.state === 'demandé') {
        setCall(move(current, 'accepté'));
        void startAudio(client, callId); // ORION a décroché : le son peut s'ouvrir
      }
    });
    client.start();
    setGateway(client);
    return () => {
      offIncoming();
      offEnded();
      offStatus();
      offAnswered();
      client.stop();
    };
  }, [credentials, forget, stopAudio]);

  const startAudio = async (client: HttpGateway, callId: string) => {
    const callAudio = new CallAudio((state) => {
      if (state === 'connecté') {
        setCall((current) => (current && current.id === callId ? move(current, 'connecté') : current));
      } else if (state === 'perdu') {
        stopAudio(); // réseau perdu (4G, VPN) : l'appel se termine au lieu de rester « en ligne »
        setCall((current) => (current && current.id === callId ? null : current));
        setLastEnd('Connexion perdue');
        client.hangUp(callId).catch(() => {});
      }
    });
    audio.current = callAudio;
    try {
      await callAudio.start((offer) => client.offer(callId, offer));
    } catch (error) {
      stopAudio();
      setCall(null);
      setLastEnd(error instanceof Error ? error.message : 'Audio impossible.');
      client.hangUp(callId).catch(() => {});
    }
  };

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
          onAccept={() =>
            act(() => gateway.accept(call.id), () => {
              setCall(move(call, 'accepté'));
              void startAudio(gateway, call.id);
            })
          }
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
          muted={muted}
          speaker={speaker}
          onToggleMute={() => {
            audio.current?.setMuted(!muted);
            setMuted(!muted);
          }}
          onToggleSpeaker={() => {
            audio.current?.setSpeaker(!speaker);
            setSpeaker(!speaker);
          }}
          onHangUp={() => {
            stopAudio();
            void act(() => gateway.hangUp(call.id), () => {
              setCall(null);
              setLastEnd('Appel terminé');
            });
          }}
        />
      );
    } else {
      screen = (
        <HomeScreen
          credentials={credentials}
          online={online}
          onForget={forget}
          onDial={async () => {
            try {
              const callId = await gateway.dial();
              setLastEnd('');
              setCall({ id: callId, reason: '', state: 'demandé' }); // le son s'ouvre quand ORION décroche
            } catch (error) {
              setLastEnd(error instanceof Error ? error.message : 'Appel impossible.');
            }
          }}
        />
      );
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
