import { Platform } from 'react-native';

import { loadEnrollment } from './credentials';
import {
  deleteEvent,
  pendingEvents,
  rejectEvent,
  retryEvent,
} from './queue';
import type { Enrollment, TrackerConfiguration } from './types';
import {
  isPermanentStatus,
  parseConfiguration,
  retryDelayMilliseconds,
  shouldHaltTimer,
} from './validation';

const REQUEST_TIMEOUT = 30_000;
const MAX_CONFIGURATION_BYTES = 5 * 1_024 * 1_024;
let flushPromise: Promise<FlushResult> | null = null;

export type FlushResult = {
  sent: number;
  rejected: number;
  waiting: boolean;
  revoked: boolean;
  haltTimerReason: string | null;
};

export class ConfigurationRequestError extends Error {
  constructor(public readonly status: number) {
    super(status === 401 ? 'The enrollment token is invalid or revoked' : `Dayfinch returned ${status}`);
    this.name = 'ConfigurationRequestError';
  }
}

async function request(
  enrollment: Enrollment,
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
  try {
    return await fetch(`${enrollment.serverUrl}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${enrollment.deviceToken}`,
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...init.headers,
      },
      redirect: 'error',
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timeout);
  }
}

export async function fetchConfiguration(
  enrollment: Enrollment,
): Promise<TrackerConfiguration> {
  const response = await request(enrollment, '/api/v1/configuration');
  if (!response.ok) {
    throw new ConfigurationRequestError(response.status);
  }
  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.toLowerCase().startsWith('application/json')) {
    throw new Error('Dayfinch returned an unexpected response');
  }
  const declaredSize = Number(response.headers.get('content-length'));
  if (Number.isFinite(declaredSize) && declaredSize > MAX_CONFIGURATION_BYTES) {
    throw new Error('The Dayfinch project catalogue is too large');
  }
  const body = await response.text();
  if (body.length > MAX_CONFIGURATION_BYTES) {
    throw new Error('The Dayfinch project catalogue is too large');
  }
  try {
    return parseConfiguration(JSON.parse(body));
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error('Dayfinch returned invalid configuration JSON', { cause: error });
    }
    throw error;
  }
}

async function performFlush(): Promise<FlushResult> {
  const enrollment = await loadEnrollment();
  const result: FlushResult = {
    sent: 0,
    rejected: 0,
    waiting: false,
    revoked: false,
    haltTimerReason: null,
  };
  if (!enrollment) return result;
  const rows = await pendingEvents();
  for (const row of rows) {
    let payload: string;
    let payloadValue: Record<string, unknown>;
    try {
      const decoded: unknown = JSON.parse(row.payload);
      if (!decoded || typeof decoded !== 'object' || Array.isArray(decoded)) {
        throw new Error('Event payload is not an object');
      }
      payloadValue = decoded as Record<string, unknown>;
      payload = JSON.stringify(payloadValue);
    } catch {
      await rejectEvent(row.id, 'local-integrity-failure');
      result.rejected += 1;
      continue;
    }
    try {
      const endpoint = row.kind === 'location' ? '/api/v1/location' : '/api/v1/heartbeat';
      const response = await request(enrollment, endpoint, {
        method: 'POST',
        body: payload,
      });
      if (response.ok) {
        await deleteEvent(row.id);
        result.sent += 1;
        continue;
      }
      if (isPermanentStatus(response.status)) {
        let reason = `server-${response.status}`;
        try {
          const problem = (await response.json()) as { detail?: unknown };
          if (typeof problem.detail === 'string') reason = problem.detail;
        } catch {
          // Status is enough to classify the event without trusting the body.
        }
        await rejectEvent(row.id, reason);
        result.rejected += 1;
        if (shouldHaltTimer(response.status, row.kind, payloadValue.status)) {
          result.haltTimerReason = reason;
        }
        if (response.status === 401) {
          result.revoked = true;
          break;
        }
        continue;
      }
      await retryEvent(
        row.id,
        retryDelayMilliseconds(row.attempts, response.headers.get('retry-after')),
      );
      result.waiting = true;
      break;
    } catch {
      await retryEvent(row.id, retryDelayMilliseconds(row.attempts));
      result.waiting = true;
      break;
    }
  }
  return result;
}

export function flushQueue(): Promise<FlushResult> {
  flushPromise ??= performFlush().finally(() => {
    flushPromise = null;
  });
  return flushPromise;
}

export function platformLabel(): string {
  return `Dayfinch Mobile ${Platform.OS}`;
}
