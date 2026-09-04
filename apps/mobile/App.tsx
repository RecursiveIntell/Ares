import React, {useState} from 'react';
import {
  NativeModules,
  Pressable,
  StatusBar,
  StyleSheet,
  Text,
  useColorScheme,
  View,
} from 'react-native';
import {MOBILE_PROTOCOL_REVISION} from '@hermes/shared/protocol';
import {SafeAreaProvider, SafeAreaView} from 'react-native-safe-area-context';

export function refreshStatus(state: 'idle' | 'requested'): string {
  return state === 'idle'
    ? 'No network request has been issued.'
    : 'Refresh requested — host enrollment is required before connection.';
}

type EnrollmentKeyStore = {publicKeyFingerprint(): Promise<string>};
const enrollmentKeyStore = NativeModules.EnrollmentKeyStore as EnrollmentKeyStore | undefined;

function App() {
  const dark = useColorScheme() !== 'light';
  const [refreshState, setRefreshState] = useState<'idle' | 'requested'>('idle');
  const [deviceKeyState, setDeviceKeyState] = useState('Not provisioned');

  const provisionDeviceKey = async () => {
    if (!enrollmentKeyStore) {
      setDeviceKeyState('Keystore bridge unavailable');
      return;
    }
    try {
      const fingerprint = await enrollmentKeyStore.publicKeyFingerprint();
      setDeviceKeyState(`Ready: ${fingerprint.slice(0, 12)}…`);
    } catch {
      setDeviceKeyState('Keystore provisioning failed');
    }
  };

  return (
    <SafeAreaProvider>
      <SafeAreaView style={[styles.safe, dark ? styles.dark : styles.light]}>
      <StatusBar barStyle={dark ? 'light-content' : 'dark-content'} />
      <View style={styles.screen}>
        <View style={styles.header}>
          <Text style={[styles.eyebrow, dark ? styles.mutedDark : styles.mutedLight]}>
            ARES MOBILE · M0
          </Text>
          <Text style={[styles.title, dark ? styles.textDark : styles.textLight]}>
            Observe-only operator console
          </Text>
          <Text style={[styles.subtitle, dark ? styles.mutedDark : styles.mutedLight]}>
            Protocol revision {MOBILE_PROTOCOL_REVISION} · controller actions remain disabled
          </Text>
        </View>

        <View style={[styles.card, dark ? styles.cardDark : styles.cardLight]}>
          <Text style={[styles.cardLabel, dark ? styles.mutedDark : styles.mutedLight]}>HOST</Text>
          <Text style={[styles.cardTitle, dark ? styles.textDark : styles.textLight]}>No host enrolled</Text>
          <Text style={[styles.cardBody, dark ? styles.mutedDark : styles.mutedLight]}>
            This prototype never infers a host from a session ID and never queues offline writes.
          </Text>
        </View>

        <View style={[styles.card, dark ? styles.cardDark : styles.cardLight]}>
          <Text style={[styles.cardLabel, dark ? styles.mutedDark : styles.mutedLight]}>SESSION STATE</Text>
          <Text style={[styles.cardTitle, dark ? styles.textDark : styles.textLight]}>Disconnected / stale until snapshot</Text>
          <Text style={[styles.cardBody, dark ? styles.mutedDark : styles.mutedLight]}>
            The app will only render authoritative session data after host identity, capability negotiation, and a typed snapshot succeed.
          </Text>
        </View>

        <View style={[styles.card, dark ? styles.cardDark : styles.cardLight]}>
          <Text style={[styles.cardLabel, dark ? styles.mutedDark : styles.mutedLight]}>DEVICE KEY</Text>
          <Text testID="device-key-state" style={[styles.cardBody, dark ? styles.mutedDark : styles.mutedLight]}>
            {deviceKeyState}
          </Text>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Provision device enrollment key"
            onPress={provisionDeviceKey}
            style={({pressed}) => [styles.button, pressed && styles.buttonPressed]}>
            <Text style={styles.buttonText}>Provision device key</Text>
          </Pressable>
        </View>

        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Request host refresh"
          onPress={() => setRefreshState('requested')}
          style={({pressed}) => [styles.button, pressed && styles.buttonPressed]}>
          <Text style={styles.buttonText}>Request host refresh</Text>
        </Pressable>
        <Text testID="refresh-state" style={[styles.status, dark ? styles.mutedDark : styles.mutedLight]}>
          {refreshStatus(refreshState)}
        </Text>

        <View style={styles.boundary}>
          <Text style={[styles.boundaryText, dark ? styles.mutedDark : styles.mutedLight]}>
            No embedded runtime · no Electron IPC · no bearer storage · no offline mutation queue
          </Text>
        </View>
      </View>
      </SafeAreaView>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  safe: {flex: 1},
  light: {backgroundColor: '#f7f8fa'},
  dark: {backgroundColor: '#101216'},
  screen: {flex: 1, gap: 16, padding: 24},
  header: {gap: 6, marginBottom: 8},
  eyebrow: {fontSize: 12, fontWeight: '700', letterSpacing: 1.4},
  title: {fontSize: 28, fontWeight: '700'},
  subtitle: {fontSize: 14, lineHeight: 20},
  card: {borderRadius: 14, gap: 8, padding: 18},
  cardLight: {backgroundColor: '#ffffff'},
  cardDark: {backgroundColor: '#1a1e25'},
  cardLabel: {fontSize: 11, fontWeight: '700', letterSpacing: 1.2},
  cardTitle: {fontSize: 18, fontWeight: '600'},
  cardBody: {fontSize: 14, lineHeight: 20},
  textLight: {color: '#151923'},
  textDark: {color: '#f5f7fa'},
  mutedLight: {color: '#596273'},
  mutedDark: {color: '#aeb8c8'},
  button: {alignItems: 'center', backgroundColor: '#8f293b', borderRadius: 10, marginTop: 8, padding: 14},
  buttonPressed: {opacity: 0.78},
  buttonText: {color: '#ffffff', fontSize: 15, fontWeight: '700'},
  status: {fontSize: 13, lineHeight: 18},
  boundary: {marginTop: 'auto', paddingTop: 16},
  boundaryText: {fontSize: 12, lineHeight: 18},
});

export default App;
