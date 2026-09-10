import * as Notifications from 'expo-notifications';
import { Platform } from 'react-native';
import * as SecureStore from 'expo-secure-store';

const REMINDER_KEY = 'dayfinch.timer-reminder.v1';
const REMINDER_CHANNEL = 'running-timer';

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: false,
    shouldSetBadge: false,
  }),
});

export async function startTimerReminder(): Promise<void> {
  if (Platform.OS === 'android') {
    await Notifications.setNotificationChannelAsync(REMINDER_CHANNEL, {
      name: 'Running timer reminders',
      importance: Notifications.AndroidImportance.DEFAULT,
      vibrationPattern: [0, 200],
      lightColor: '#7B67F0',
    });
  }
  const permission = await Notifications.requestPermissionsAsync();
  if (!permission.granted) return;
  const existing = await SecureStore.getItemAsync(REMINDER_KEY);
  if (existing) return;
  const identifier = await Notifications.scheduleNotificationAsync({
    content: {
      title: 'Dayfinch timer is running',
      body: 'Open Dayfinch to review or stop your current timer.',
      data: { kind: 'timer-reminder' },
    },
    trigger: {
      type: Notifications.SchedulableTriggerInputTypes.TIME_INTERVAL,
      channelId: REMINDER_CHANNEL,
      seconds: 1_800,
      repeats: true,
    },
  });
  await SecureStore.setItemAsync(REMINDER_KEY, identifier);
}

export async function stopTimerReminder(): Promise<void> {
  const identifier = await SecureStore.getItemAsync(REMINDER_KEY);
  if (identifier) {
    await Notifications.cancelScheduledNotificationAsync(identifier).catch(() => {});
  }
  await SecureStore.deleteItemAsync(REMINDER_KEY);
}
