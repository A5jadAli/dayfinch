import * as Network from 'expo-network';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  AppState,
  Linking,
  Pressable,
  SafeAreaView,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  useColorScheme,
  View,
} from 'react-native';

import { ConfigurationRequestError, fetchConfiguration, flushQueue } from './src/api';
import { deleteEnrollment, loadEnrollment, saveEnrollment } from './src/credentials';
import {
  disableLocationTracking,
  enableLocationTracking,
  hasLocationConsent,
  resumeConsentedLocation,
  suspendLocationTracking,
  type LocationMode,
} from './src/location';
import { clearTimerState, eraseLocalDatabase, loadTimerState, queueCounts } from './src/queue';
import { startTimerReminder, stopTimerReminder } from './src/reminders';
import {
  elapsedSeconds,
  pauseTimer,
  queueHeartbeat,
  resumeTimer,
  startTimer,
  stopTimer,
} from './src/tracking';
import type { Enrollment, QueueCounts, TimerState, TrackerConfiguration } from './src/types';
import { parseEnrollment } from './src/validation';

const EMPTY_COUNTS: QueueCounts = { pending: 0, rejected: 0 };

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong';
}

function clock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3_600).toString().padStart(2, '0');
  const minutes = Math.floor((total % 3_600) / 60).toString().padStart(2, '0');
  const remainder = (total % 60).toString().padStart(2, '0');
  return `${hours}:${minutes}:${remainder}`;
}

type Palette = typeof dark;
const dark = {
  background: '#0D0E12', surface: '#15161C', surface2: '#1C1D25', border: '#2B2D38',
  text: '#F7F7FA', muted: '#989AA8', purple: '#8A78F6', purpleDim: '#292441',
  green: '#35D5A1', red: '#FF707D', amber: '#F6C85F', input: '#111218',
};
const light: Palette = {
  background: '#F4F4F8', surface: '#FFFFFF', surface2: '#F0EFF7', border: '#DDDDE7',
  text: '#171820', muted: '#656776', purple: '#6957DA', purpleDim: '#E9E5FF',
  green: '#087F60', red: '#C93A4A', amber: '#8A6500', input: '#FAFAFC',
};

function Button({ label, onPress, palette, tone = 'primary', disabled = false }: {
  label: string; onPress: () => void; palette: Palette; tone?: 'primary' | 'secondary' | 'danger'; disabled?: boolean;
}) {
  const color = tone === 'primary' ? palette.purple : tone === 'danger' ? palette.red : palette.surface2;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled }}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [styles.button, { backgroundColor: color, opacity: disabled ? 0.45 : pressed ? 0.75 : 1 }]}
    >
      <Text style={[styles.buttonText, { color: tone === 'secondary' ? palette.text : '#FFFFFF' }]}>{label}</Text>
    </Pressable>
  );
}

function EnrollmentScreen({ palette, onConnected }: { palette: Palette; onConnected: (enrollment: Enrollment, configuration: TrackerConfiguration) => void }) {
  const [payload, setPayload] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const connect = async () => {
    setBusy(true); setMessage('');
    try {
      const enrollment = parseEnrollment(payload);
      const configuration = await fetchConfiguration(enrollment);
      await saveEnrollment(enrollment);
      onConnected(enrollment, configuration);
    } catch (error) {
      setMessage(errorMessage(error));
    } finally { setBusy(false); }
  };
  return (
    <ScrollView contentContainerStyle={styles.centered} keyboardShouldPersistTaps="handled">
      <View style={[styles.logo, { backgroundColor: palette.purple }]}><Text style={styles.logoMark}>D</Text></View>
      <Text style={[styles.brand, { color: palette.text }]}>Dayfinch</Text>
      <Text style={[styles.kicker, { color: palette.purple }]}>MOBILE TRACKER</Text>
      <View style={[styles.card, { backgroundColor: palette.surface, borderColor: palette.border }]}>
        <Text style={[styles.title, { color: palette.text }]}>Connect this device</Text>
        <Text style={[styles.copy, { color: palette.muted }]}>Create a device from your Dayfinch project, then paste its one-time mobile enrollment JSON here.</Text>
        <TextInput
          accessibilityLabel="Mobile enrollment JSON"
          autoCapitalize="none"
          autoCorrect={false}
          multiline
          onChangeText={setPayload}
          placeholder={'{\n  "server_url": "https://…",\n  "device_token": "…"\n}'}
          placeholderTextColor={palette.muted}
          secureTextEntry={false}
          style={[styles.enrollmentInput, { color: palette.text, backgroundColor: palette.input, borderColor: palette.border }]}
          value={payload}
        />
        {message ? <Text accessibilityRole="alert" style={[styles.error, { color: palette.red }]}>{message}</Text> : null}
        <Button disabled={busy || !payload.trim()} label={busy ? 'Connecting…' : 'Connect securely'} onPress={() => void connect()} palette={palette} />
        <Text style={[styles.privacy, { color: palette.muted }]}>The device token is stored in the OS keychain/keystore. Dayfinch mobile records time and, only with your consent, work-session location. Mobile OS security prevents screenshots of other apps.</Text>
      </View>
    </ScrollView>
  );
}

export default function App() {
  const palette = useColorScheme() === 'light' ? light : dark;
  const [ready, setReady] = useState(false);
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [configuration, setConfiguration] = useState<TrackerConfiguration | null>(null);
  const [timer, setTimer] = useState<TimerState | null>(null);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [counts, setCounts] = useState<QueueCounts>(EMPTY_COUNTS);
  const [locationMode, setLocationMode] = useState<LocationMode>('off');
  const [locationConsented, setLocationConsented] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const timerRef = useRef<TimerState | null>(null);
  timerRef.current = timer;

  const refreshCounts = useCallback(async () => setCounts(await queueCounts()), []);
  const sync = useCallback(async () => {
    const result = await flushQueue();
    await refreshCounts();
    if (result.haltTimerReason) {
      await clearTimerState();
      setTimer(null);
      await suspendLocationTracking();
      setLocationMode('off');
      await stopTimerReminder();
      setNotice(result.revoked
        ? 'This device was revoked. Tracking has stopped; reconnect with a new enrollment.'
        : `The server rejected active tracking (${result.haltTimerReason}). Tracking has stopped.`);
    }
  }, [refreshCounts]);

  const loadConfiguration = useCallback(async (value: Enrollment) => {
    const next = await fetchConfiguration(value);
    setConfiguration(next);
    const requested = value.projectId && next.projects.some((project) => project.id === value.projectId) ? value.projectId : next.projects[0]?.id ?? null;
    setProjectId((current) => current && next.projects.some((project) => project.id === current) ? current : requested);
    return next;
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const saved = await loadEnrollment();
        const savedTimer = await loadTimerState();
        let resumableTimer = savedTimer;
        if (saved) {
          setEnrollment(saved);
          try {
            await loadConfiguration(saved);
          } catch (error) {
            setNotice(errorMessage(error));
            if (error instanceof ConfigurationRequestError && error.status === 401) {
              resumableTimer = null;
              await clearTimerState();
            }
          }
        }
        setTimer(resumableTimer);
        if (resumableTimer) { setProjectId(resumableTimer.projectId); setTaskId(resumableTimer.taskId); }
        await refreshCounts();
        const consented = await hasLocationConsent();
        setLocationConsented(consented);
        if (resumableTimer?.status === 'active' && consented) {
          setLocationMode(await resumeConsentedLocation());
          await startTimerReminder();
        } else if (resumableTimer?.status === 'active') {
          await startTimerReminder();
        } else {
          await suspendLocationTracking();
          await stopTimerReminder();
        }
      } finally { setReady(true); }
    })();
  }, [loadConfiguration, refreshCounts]);

  useEffect(() => {
    const second = setInterval(() => setNow(Date.now()), 1_000);
    const heartbeat = setInterval(() => {
      const current = timerRef.current;
      if (current?.status === 'active') void queueHeartbeat(current).then(sync).catch(() => {});
    }, 60_000);
    const network = Network.addNetworkStateListener((state) => {
      if (state.isConnected && state.isInternetReachable !== false) void sync();
    });
    const appState = AppState.addEventListener('change', (state) => {
      if (state === 'active') void sync();
    });
    return () => { clearInterval(second); clearInterval(heartbeat); network.remove(); appState.remove(); };
  }, [sync]);

  const projects = configuration?.projects ?? [];
  const selectedProject = projects.find((project) => project.id === projectId) ?? null;
  const selectedTask = selectedProject?.tasks.find((task) => task.id === taskId) ?? null;
  const elapsed = timer ? elapsedSeconds(timer, now) : 0;
  const locked = timer !== null;

  const action = async (work: () => Promise<void>) => {
    setBusy(true); setNotice('');
    try { await work(); await sync(); } catch (error) { setNotice(errorMessage(error)); } finally { setBusy(false); }
  };

  const start = () => action(async () => {
    if (!projectId) throw new Error('Choose a project first');
    if (configuration?.allowedApps === 'desktop_only') throw new Error('Desktop tracking is required by your organization policy');
    const next = await startTimer(projectId, taskId);
    setTimer(next);
    await startTimerReminder();
    if (locationConsented) setLocationMode(await resumeConsentedLocation());
  });
  const pause = () => action(async () => {
    if (!timer) return;
    const next = await pauseTimer(timer);
    setTimer(next); await suspendLocationTracking(); setLocationMode('off'); await stopTimerReminder();
  });
  const resume = () => action(async () => {
    if (!timer) return;
    if (configuration?.allowedApps === 'desktop_only') throw new Error('Desktop tracking is required by your organization policy');
    const next = await resumeTimer(timer);
    setTimer(next); await startTimerReminder();
    if (locationConsented) setLocationMode(await resumeConsentedLocation());
  });
  const stop = () => action(async () => {
    if (!timer) return;
    await stopTimer(timer); setTimer(null); await stopTimerReminder();
    await suspendLocationTracking(); setLocationMode('off');
  });
  const toggleLocation = () => action(async () => {
    if (locationConsented) {
      await disableLocationTracking(); setLocationMode('off'); setLocationConsented(false); return;
    }
    setLocationMode(await enableLocationTracking()); setLocationConsented(true);
  });
  const disconnect = () => {
    if (timer) { setNotice('Stop the timer before disconnecting this device.'); return; }
    Alert.alert('Disconnect this device?', 'Pending events and the local encrypted database will be erased. This does not revoke the device in Dayfinch.', [
      { text: 'Cancel', style: 'cancel' },
      { text: 'Disconnect', style: 'destructive', onPress: () => void action(async () => {
        await disableLocationTracking(); await stopTimerReminder(); await eraseLocalDatabase(); await deleteEnrollment();
        setEnrollment(null); setConfiguration(null); setProjectId(null); setTaskId(null); setCounts(EMPTY_COUNTS); setLocationMode('off'); setLocationConsented(false);
      }) },
    ]);
  };

  const projectButtons = useMemo(() => projects.map((project) => (
    <Pressable key={project.id} disabled={locked} onPress={() => { setProjectId(project.id); setTaskId(null); }} style={[styles.choice, { borderColor: project.id === projectId ? palette.purple : palette.border, backgroundColor: project.id === projectId ? palette.purpleDim : palette.surface2, opacity: locked && project.id !== projectId ? 0.5 : 1 }]}>
      <Text style={[styles.choiceText, { color: palette.text }]} numberOfLines={1}>{project.name}</Text>
    </Pressable>
  )), [locked, palette, projectId, projects]);

  if (!ready) return <SafeAreaView style={[styles.safe, styles.loading, { backgroundColor: palette.background }]}><ActivityIndicator color={palette.purple} size="large" /></SafeAreaView>;
  if (!enrollment) return <SafeAreaView style={[styles.safe, { backgroundColor: palette.background }]}><StatusBar barStyle={palette === dark ? 'light-content' : 'dark-content'} /><EnrollmentScreen palette={palette} onConnected={(value, next) => { setEnrollment(value); setConfiguration(next); setProjectId(value.projectId ?? next.projects[0]?.id ?? null); }} /></SafeAreaView>;

  return (
    <SafeAreaView style={[styles.safe, { backgroundColor: palette.background }]}>
      <StatusBar barStyle={palette === dark ? 'light-content' : 'dark-content'} />
      <ScrollView contentContainerStyle={styles.page}>
        <View style={styles.header}><View><Text style={[styles.brandSmall, { color: palette.text }]}>Dayfinch</Text><Text style={[styles.kicker, { color: palette.purple }]}>MOBILE TRACKER</Text></View><Pressable onPress={disconnect}><Text style={[styles.link, { color: palette.muted }]}>Disconnect</Text></Pressable></View>
        <View style={[styles.timerCard, { backgroundColor: palette.surface, borderColor: timer?.status === 'active' ? palette.purple : palette.border }]}>
          <View style={styles.statusRow}><View style={[styles.statusDot, { backgroundColor: timer?.status === 'active' ? palette.green : palette.muted }]} /><Text style={[styles.status, { color: palette.muted }]}>{timer?.status === 'active' ? 'TRACKING' : timer ? 'PAUSED' : 'READY'}</Text></View>
          <Text accessibilityLabel={`Elapsed time ${clock(elapsed)}`} style={[styles.clock, { color: palette.text }]}>{clock(elapsed)}</Text>
          <Text style={[styles.current, { color: palette.muted }]}>{selectedProject?.name ?? 'Choose a project'}{selectedTask ? ` · ${selectedTask.name}` : ''}</Text>
          <View style={styles.actions}>
            {!timer ? <Button disabled={busy || !projectId || configuration?.allowedApps === 'desktop_only'} label="Start timer" onPress={start} palette={palette} /> : timer.status === 'active' ? <><Button disabled={busy} label="Pause" onPress={pause} palette={palette} tone="secondary" /><Button disabled={busy} label="Stop" onPress={stop} palette={palette} tone="danger" /></> : <><Button disabled={busy || configuration?.allowedApps === 'desktop_only'} label="Resume" onPress={resume} palette={palette} /><Button disabled={busy} label="Stop" onPress={stop} palette={palette} tone="danger" /></>}
          </View>
        </View>

        {notice ? <Text accessibilityRole="alert" style={[styles.notice, { color: palette.amber, backgroundColor: palette.surface }]}>{notice}</Text> : null}
        {configuration?.allowedApps === 'desktop_only' ? <Text style={[styles.notice, { color: palette.amber, backgroundColor: palette.surface }]}>Your organization currently requires the Dayfinch desktop tracker. Mobile timers cannot start or resume.</Text> : null}
        <View style={[styles.syncCard, { backgroundColor: palette.surface, borderColor: palette.border }]}><View><Text style={[styles.sectionTitle, { color: palette.text }]}>Sync status</Text><Text style={[styles.copy, { color: palette.muted }]}>{counts.pending ? `${counts.pending} waiting to sync` : 'All events synced'}{counts.rejected ? ` · ${counts.rejected} need review` : ''}</Text></View><Pressable disabled={busy} onPress={() => void sync()}><Text style={[styles.link, { color: palette.purple }]}>Sync now</Text></Pressable></View>

        <Text style={[styles.sectionTitle, { color: palette.text }]}>Project</Text>
        <View style={styles.choiceGrid}>{projectButtons.length ? projectButtons : <Text style={[styles.copy, { color: palette.muted }]}>No trackable projects are assigned to this account.</Text>}</View>
        {selectedProject?.tasks.length ? <><Text style={[styles.sectionTitle, { color: palette.text }]}>Task</Text><View style={styles.choiceGrid}><Pressable disabled={locked} onPress={() => setTaskId(null)} style={[styles.choice, { borderColor: taskId === null ? palette.purple : palette.border, backgroundColor: taskId === null ? palette.purpleDim : palette.surface2 }]}><Text style={[styles.choiceText, { color: palette.text }]}>No task</Text></Pressable>{selectedProject.tasks.map((task) => <Pressable key={task.id} disabled={locked} onPress={() => setTaskId(task.id)} style={[styles.choice, { borderColor: task.id === taskId ? palette.purple : palette.border, backgroundColor: task.id === taskId ? palette.purpleDim : palette.surface2 }]}><Text numberOfLines={1} style={[styles.choiceText, { color: palette.text }]}>{task.name}</Text></Pressable>)}</View></> : null}

        <View style={[styles.card, { backgroundColor: palette.surface, borderColor: palette.border }]}><View style={styles.syncCardInner}><View style={styles.flex}><Text style={[styles.sectionTitle, { color: palette.text }]}>Work-session location</Text><Text style={[styles.copy, { color: palette.muted }]}>{locationMode === 'background' ? 'Background location is on while the timer runs.' : locationMode === 'foreground' ? 'Foreground-only location is on. Keep Dayfinch open.' : locationConsented ? 'Enabled. Collection resumes with your active timer.' : 'Off. Dayfinch does not collect location.'}</Text></View><Pressable onPress={toggleLocation} disabled={busy || timer?.status !== 'active'} style={{ opacity: timer?.status === 'active' ? 1 : 0.45 }}><Text style={[styles.link, { color: locationConsented ? palette.red : palette.purple }]}>{locationConsented ? 'Disable' : 'Enable'}</Text></Pressable></View>{timer?.status !== 'active' ? <Text style={[styles.privacy, { color: palette.muted }]}>Start or resume the timer before enabling location.</Text> : null}{locationMode === 'foreground' ? <Pressable onPress={() => void Linking.openSettings()}><Text style={[styles.settingsLink, { color: palette.purple }]}>Open settings to allow background access</Text></Pressable> : null}</View>

        <Pressable disabled={busy} onPress={() => void action(async () => { await loadConfiguration(enrollment); })}><Text style={[styles.refresh, { color: palette.purple }]}>Refresh projects and policy</Text></Pressable>
        <Text style={[styles.footer, { color: palette.muted }]}>Time transitions sync reliably after an outage. Screenshots and keyboard/mouse activity require the Dayfinch desktop tracker; mobile operating systems do not permit cross-app screen capture.</Text>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1 }, loading: { alignItems: 'center', justifyContent: 'center' },
  centered: { flexGrow: 1, justifyContent: 'center', padding: 24 }, page: { padding: 20, paddingBottom: 48, gap: 16 },
  logo: { width: 52, height: 52, borderRadius: 16, alignItems: 'center', justifyContent: 'center', alignSelf: 'center' }, logoMark: { color: '#FFFFFF', fontSize: 26, fontWeight: '900' },
  brand: { fontSize: 29, fontWeight: '800', textAlign: 'center', marginTop: 12 }, brandSmall: { fontSize: 22, fontWeight: '800' },
  kicker: { fontSize: 10, letterSpacing: 2, fontWeight: '800', textAlign: 'center' },
  header: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  card: { borderWidth: 1, borderRadius: 20, padding: 18, gap: 14, marginTop: 24 },
  timerCard: { borderWidth: 1, borderRadius: 24, padding: 22, alignItems: 'center', gap: 10 },
  title: { fontSize: 23, fontWeight: '700' }, sectionTitle: { fontSize: 15, fontWeight: '700' }, copy: { fontSize: 13, lineHeight: 19 }, privacy: { fontSize: 11, lineHeight: 16 },
  enrollmentInput: { minHeight: 180, borderWidth: 1, borderRadius: 14, padding: 14, fontFamily: 'monospace', fontSize: 12, textAlignVertical: 'top' }, error: { fontSize: 13 },
  button: { minHeight: 48, minWidth: 116, borderRadius: 14, paddingHorizontal: 18, alignItems: 'center', justifyContent: 'center' }, buttonText: { fontSize: 14, fontWeight: '700' },
  statusRow: { flexDirection: 'row', alignItems: 'center', gap: 7 }, statusDot: { width: 8, height: 8, borderRadius: 4 }, status: { fontSize: 10, letterSpacing: 1.8, fontWeight: '800' },
  clock: { fontSize: 45, fontVariant: ['tabular-nums'], fontWeight: '300', letterSpacing: 1 }, current: { fontSize: 13 }, actions: { flexDirection: 'row', gap: 10, marginTop: 10 },
  notice: { padding: 14, borderRadius: 14, fontSize: 13, lineHeight: 18 },
  syncCard: { borderWidth: 1, borderRadius: 16, padding: 16, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }, syncCardInner: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12 }, flex: { flex: 1 },
  link: { fontSize: 13, fontWeight: '700' }, settingsLink: { fontSize: 12, fontWeight: '600' },
  choiceGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 9 }, choice: { maxWidth: '100%', borderWidth: 1, borderRadius: 12, paddingVertical: 10, paddingHorizontal: 13 }, choiceText: { fontSize: 13, fontWeight: '600' },
  refresh: { textAlign: 'center', fontSize: 13, fontWeight: '700', paddingVertical: 8 }, footer: { fontSize: 11, lineHeight: 16, textAlign: 'center' },
});
