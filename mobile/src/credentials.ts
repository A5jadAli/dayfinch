import * as SecureStore from 'expo-secure-store';

import type { Enrollment } from './types';

const ENROLLMENT_KEY = 'dayfinch.enrollment.v1';
const OPTIONS: SecureStore.SecureStoreOptions = {
  keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
};

export async function loadEnrollment(): Promise<Enrollment | null> {
  const encoded = await SecureStore.getItemAsync(ENROLLMENT_KEY, OPTIONS);
  if (!encoded) return null;
  try {
    const value = JSON.parse(encoded) as Enrollment;
    if (!value.serverUrl || !value.deviceToken) return null;
    return value;
  } catch {
    return null;
  }
}

export async function saveEnrollment(enrollment: Enrollment): Promise<void> {
  await SecureStore.setItemAsync(
    ENROLLMENT_KEY,
    JSON.stringify(enrollment),
    OPTIONS,
  );
}

export async function deleteEnrollment(): Promise<void> {
  await SecureStore.deleteItemAsync(ENROLLMENT_KEY, OPTIONS);
}
