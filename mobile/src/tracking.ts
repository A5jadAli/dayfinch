import * as Crypto from 'expo-crypto';

import { platformLabel } from './api';
import { enqueueEvent } from './queue';
import type { QueuedEvent, TimerState } from './types';

function iso(timestamp = Date.now()): string {
  return new Date(timestamp).toISOString();
}

function heartbeatEvent(
  state: TimerState,
  transition: boolean,
  timestamp = Date.now(),
): QueuedEvent {
  const eventId = Crypto.randomUUID();
  const status = state.status;
  return {
    id: eventId,
    kind: 'heartbeat',
    observedAt: iso(timestamp),
    payload: {
      platform: platformLabel(),
      event_id: eventId,
      observed_at: iso(timestamp),
      status,
      project_id: state.projectId,
      task_id: state.taskId,
      note: '',
      idle_seconds: 0,
      heartbeat_interval_seconds: 60,
      transition,
    },
  };
}

export async function startTimer(
  projectId: string,
  taskId: string | null,
  timestamp = Date.now(),
): Promise<TimerState> {
  const state: TimerState = {
    status: 'active',
    projectId,
    taskId,
    startedAt: iso(timestamp),
    accumulatedSeconds: 0,
  };
  await enqueueEvent(heartbeatEvent(state, true, timestamp), state);
  return state;
}

export function elapsedSeconds(state: TimerState, timestamp = Date.now()): number {
  if (state.status === 'paused') return state.accumulatedSeconds;
  return (
    state.accumulatedSeconds +
    Math.max(0, Math.floor((timestamp - Date.parse(state.startedAt)) / 1_000))
  );
}

export async function pauseTimer(
  state: TimerState,
  timestamp = Date.now(),
): Promise<TimerState> {
  const paused: TimerState = {
    ...state,
    status: 'paused',
    accumulatedSeconds: elapsedSeconds(state, timestamp),
  };
  await enqueueEvent(heartbeatEvent(paused, true, timestamp), paused);
  return paused;
}

export async function resumeTimer(
  state: TimerState,
  timestamp = Date.now(),
): Promise<TimerState> {
  const active: TimerState = {
    ...state,
    status: 'active',
    startedAt: iso(timestamp),
  };
  await enqueueEvent(heartbeatEvent(active, true, timestamp), active);
  return active;
}

export async function stopTimer(
  state: TimerState,
  timestamp = Date.now(),
): Promise<void> {
  const stopped: TimerState = { ...state, status: 'paused' };
  const event = heartbeatEvent(stopped, true, timestamp);
  event.payload.status = 'stopped';
  await enqueueEvent(event, null);
}

export async function queueHeartbeat(
  state: TimerState,
  timestamp = Date.now(),
): Promise<void> {
  await enqueueEvent(heartbeatEvent(state, false, timestamp));
}

export async function queueLocation(
  latitude: number,
  longitude: number,
  accuracy: number | null,
  timestamp: number,
): Promise<void> {
  if (
    !Number.isFinite(latitude) ||
    !Number.isFinite(longitude) ||
    !Number.isFinite(timestamp) ||
    latitude < -90 || latitude > 90 ||
    longitude < -180 || longitude > 180
  ) {
    throw new Error('The location sample is invalid');
  }
  const eventId = Crypto.randomUUID();
  const recordedAt = iso(timestamp);
  await enqueueEvent({
    id: eventId,
    kind: 'location',
    observedAt: recordedAt,
    payload: {
      event_id: eventId,
      recorded_at: recordedAt,
      latitude,
      longitude,
      accuracy_meters: Math.min(
        100_000,
        Math.max(0, accuracy !== null && Number.isFinite(accuracy) ? accuracy : 0),
      ),
      event_type: 'position',
    },
  });
}
