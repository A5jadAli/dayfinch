import * as Location from 'expo-location';
import * as TaskManager from 'expo-task-manager';

import { flushQueue } from './api';
import { loadEnrollment } from './credentials';
import { clearTimerState, loadTimerState } from './queue';
import { stopTimerReminder } from './reminders';
import { queueHeartbeat, queueLocation } from './tracking';

export const LOCATION_TASK = 'dayfinch-consented-location-v1';

type LocationTaskData = { locations?: Location.LocationObject[] };

TaskManager.defineTask<LocationTaskData>(LOCATION_TASK, async ({ data, error }) => {
  if (error || !(await loadEnrollment())) return;
  const timer = await loadTimerState();
  // Location collection is purposefully scoped to a running timer. A registered
  // OS task must never turn Dayfinch into an always-on location recorder.
  if (timer?.status !== 'active') return;
  const locations = data?.locations ?? [];
  for (const location of locations.slice(0, 100)) {
    await queueLocation(
      location.coords.latitude,
      location.coords.longitude,
      location.coords.accuracy,
      location.timestamp,
    );
  }
  const latest = locations.at(-1);
  if (latest) {
    await queueHeartbeat(timer, latest.timestamp);
  }
  const result = await flushQueue();
  if (result.haltTimerReason) {
    await clearTimerState();
    await stopTimerReminder();
    if (await Location.hasStartedLocationUpdatesAsync(LOCATION_TASK).catch(() => false)) {
      await Location.stopLocationUpdatesAsync(LOCATION_TASK);
    }
  }
});
