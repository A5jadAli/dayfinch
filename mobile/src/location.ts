import * as Location from 'expo-location';
import * as SecureStore from 'expo-secure-store';
import * as TaskManager from 'expo-task-manager';

import { flushQueue } from './api';
import { LOCATION_TASK } from './background';
import { loadTimerState } from './queue';
import { queueLocation } from './tracking';

const LOCATION_CONSENT_KEY = 'dayfinch.location-consent.v1';
let foregroundSubscription: Location.LocationSubscription | null = null;

export type LocationMode = 'off' | 'foreground' | 'background';

export async function hasLocationConsent(): Promise<boolean> {
  return (await SecureStore.getItemAsync(LOCATION_CONSENT_KEY)) === 'yes';
}

async function setLocationConsent(enabled: boolean): Promise<void> {
  if (enabled) {
    await SecureStore.setItemAsync(LOCATION_CONSENT_KEY, 'yes');
  } else {
    await SecureStore.deleteItemAsync(LOCATION_CONSENT_KEY);
  }
}

export async function currentLocationMode(): Promise<LocationMode> {
  if (!(await hasLocationConsent())) return 'off';
  if (
    (await TaskManager.isAvailableAsync()) &&
    (await Location.hasStartedLocationUpdatesAsync(LOCATION_TASK).catch(() => false))
  ) {
    return 'background';
  }
  return foregroundSubscription ? 'foreground' : 'off';
}

async function watchForeground(): Promise<LocationMode> {
  foregroundSubscription?.remove();
  foregroundSubscription = await Location.watchPositionAsync(
    {
      accuracy: Location.Accuracy.Balanced,
      distanceInterval: 25,
      timeInterval: 60_000,
    },
    (location) => {
      void (async () => {
        const timer = await loadTimerState();
        if (timer?.status !== 'active') return;
        await queueLocation(
          location.coords.latitude,
          location.coords.longitude,
          location.coords.accuracy,
          location.timestamp,
        );
        await flushQueue();
      })().catch(() => {});
    },
  );
  return 'foreground';
}

export async function enableLocationTracking(): Promise<LocationMode> {
  const servicesEnabled = await Location.hasServicesEnabledAsync();
  if (!servicesEnabled) throw new Error('Turn on Location Services and try again');
  const foreground = await Location.requestForegroundPermissionsAsync();
  if (!foreground.granted) {
    throw new Error('Location permission was not granted');
  }
  await setLocationConsent(true);

  if (await TaskManager.isAvailableAsync()) {
    const background = await Location.requestBackgroundPermissionsAsync();
    if (background.granted) {
      foregroundSubscription?.remove();
      foregroundSubscription = null;
      if (!(await Location.hasStartedLocationUpdatesAsync(LOCATION_TASK))) {
        await Location.startLocationUpdatesAsync(LOCATION_TASK, {
          accuracy: Location.Accuracy.Balanced,
          distanceInterval: 25,
          timeInterval: 60_000,
          deferredUpdatesDistance: 50,
          deferredUpdatesInterval: 60_000,
          pausesUpdatesAutomatically: true,
          showsBackgroundLocationIndicator: true,
          foregroundService: {
            notificationTitle: 'Dayfinch location tracking',
            notificationBody: 'Location is recorded while your work timer is running.',
            notificationColor: '#7B67F0',
            killServiceOnDestroy: false,
          },
        });
      }
      return 'background';
    }
  }
  return watchForeground();
}

export async function resumeConsentedLocation(): Promise<LocationMode> {
  if (!(await hasLocationConsent())) return 'off';
  const foreground = await Location.getForegroundPermissionsAsync();
  if (!foreground.granted) return 'off';
  const background = await Location.getBackgroundPermissionsAsync();
  if ((await TaskManager.isAvailableAsync()) && background.granted) {
    if (!(await Location.hasStartedLocationUpdatesAsync(LOCATION_TASK))) {
      await Location.startLocationUpdatesAsync(LOCATION_TASK, {
        accuracy: Location.Accuracy.Balanced,
        distanceInterval: 25,
        timeInterval: 60_000,
        deferredUpdatesDistance: 50,
        deferredUpdatesInterval: 60_000,
        pausesUpdatesAutomatically: true,
        showsBackgroundLocationIndicator: true,
        foregroundService: {
          notificationTitle: 'Dayfinch location tracking',
          notificationBody: 'Location is recorded while your work timer is running.',
          notificationColor: '#7B67F0',
          killServiceOnDestroy: false,
        },
      });
    }
    return 'background';
  }
  return watchForeground();
}

export async function suspendLocationTracking(): Promise<void> {
  foregroundSubscription?.remove();
  foregroundSubscription = null;
  if (await Location.hasStartedLocationUpdatesAsync(LOCATION_TASK).catch(() => false)) {
    await Location.stopLocationUpdatesAsync(LOCATION_TASK);
  }
}

export async function disableLocationTracking(): Promise<void> {
  await suspendLocationTracking();
  await setLocationConsent(false);
}
