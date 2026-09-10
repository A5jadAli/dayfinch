import * as Crypto from 'expo-crypto';
import * as SecureStore from 'expo-secure-store';
import * as SQLite from 'expo-sqlite';

import type { QueueCounts, QueuedEvent, TimerState } from './types';

const DATABASE_NAME = 'dayfinch-mobile.db';
const DATABASE_KEY = 'dayfinch.database-key.v1';
const MAX_PENDING = 10_000;
const MAX_REJECTED = 2_000;
const MAX_PAYLOAD_BYTES = 32_768;
const KEY_OPTIONS: SecureStore.SecureStoreOptions = {
  keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
};

type Database = SQLite.SQLiteDatabase;
type StoredEvent = {
  id: string;
  kind: 'heartbeat' | 'location';
  observed_at: string;
  payload: string;
  attempts: number;
};

let databasePromise: Promise<Database> | null = null;

function bytesToHex(bytes: Uint8Array): string {
  return [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('');
}

async function databaseKey(): Promise<string> {
  const current = await SecureStore.getItemAsync(DATABASE_KEY, KEY_OPTIONS);
  if (current && /^[0-9a-f]{64}$/.test(current)) return current;
  const generated = bytesToHex(await Crypto.getRandomBytesAsync(32));
  await SecureStore.setItemAsync(DATABASE_KEY, generated, KEY_OPTIONS);
  return generated;
}

async function openDatabase(): Promise<Database> {
  const key = await databaseKey();
  const database = await SQLite.openDatabaseAsync(DATABASE_NAME);
  await database.execAsync(`
    PRAGMA key = "x'${key}'";
    PRAGMA cipher_memory_security = ON;
    PRAGMA journal_mode = WAL;
    PRAGMA foreign_keys = ON;
    CREATE TABLE IF NOT EXISTS event_queue (
      id TEXT PRIMARY KEY NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('heartbeat','location')),
      observed_at TEXT NOT NULL,
      payload TEXT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt_at INTEGER NOT NULL DEFAULT 0,
      state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','rejected')),
      rejection_reason TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_mobile_queue_ready
      ON event_queue(state,next_attempt_at,observed_at);
    CREATE TABLE IF NOT EXISTS local_state (
      key TEXT PRIMARY KEY NOT NULL,
      value TEXT NOT NULL
    );
  `);
  const cutoff = Date.now() - 90 * 86_400_000;
  const rejectedCutoff = Date.now() - 30 * 86_400_000;
  await database.runAsync(
    "DELETE FROM event_queue WHERE (state='pending' AND created_at < ?) OR (state='rejected' AND created_at < ?)",
    cutoff,
    rejectedCutoff,
  );
  return database;
}

export function database(): Promise<Database> {
  databasePromise ??= openDatabase().catch((error) => {
    databasePromise = null;
    throw error;
  });
  return databasePromise;
}

export async function enqueueEvent(
  event: QueuedEvent,
  timerState?: TimerState | null,
): Promise<void> {
  const payload = JSON.stringify(event.payload);
  if (payload.length > MAX_PAYLOAD_BYTES) throw new Error('Event payload is too large');
  const db = await database();
  await db.withExclusiveTransactionAsync(async (transaction) => {
    const count = await transaction.getFirstAsync<{ count: number }>(
      "SELECT COUNT(*) count FROM event_queue WHERE state='pending'",
    );
    if ((count?.count ?? 0) >= MAX_PENDING) {
      const removed = await transaction.runAsync(
        "DELETE FROM event_queue WHERE id=(SELECT id FROM event_queue WHERE state='pending' AND kind='location' ORDER BY observed_at LIMIT 1)",
      );
      if (removed.changes !== 1) {
        throw new Error('The offline timer queue is full; reconnect before continuing');
      }
    }
    await transaction.runAsync(
      `INSERT INTO event_queue(id,kind,observed_at,payload,created_at)
       VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING`,
      event.id,
      event.kind,
      event.observedAt,
      payload,
      Date.now(),
    );
    if (timerState !== undefined) {
      if (timerState === null) {
        await transaction.runAsync("DELETE FROM local_state WHERE key='timer'");
      } else {
        await transaction.runAsync(
          `INSERT INTO local_state(key,value) VALUES('timer',?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value`,
          JSON.stringify(timerState),
        );
      }
    }
  });
}

export async function pendingEvents(limit = 100): Promise<StoredEvent[]> {
  const db = await database();
  return db.getAllAsync<StoredEvent>(
    `SELECT id,kind,observed_at,payload,attempts FROM event_queue
     WHERE state='pending' AND next_attempt_at <= ?
     ORDER BY observed_at,id LIMIT ?`,
    Date.now(),
    Math.min(500, Math.max(1, limit)),
  );
}

export async function deleteEvent(id: string): Promise<void> {
  const db = await database();
  await db.runAsync('DELETE FROM event_queue WHERE id=?', id);
}

export async function rejectEvent(id: string, reason: string): Promise<void> {
  const db = await database();
  await db.withExclusiveTransactionAsync(async (transaction) => {
    await transaction.runAsync(
      `UPDATE event_queue SET state='rejected',rejection_reason=? WHERE id=?`,
      reason.slice(0, 300),
      id,
    );
    await transaction.runAsync(
      `DELETE FROM event_queue WHERE id IN (
         SELECT id FROM event_queue WHERE state='rejected'
         ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET ?
       )`,
      MAX_REJECTED,
    );
  });
}

export async function retryEvent(id: string, delayMilliseconds: number): Promise<void> {
  const db = await database();
  await db.runAsync(
    `UPDATE event_queue SET attempts=attempts+1,next_attempt_at=? WHERE id=?`,
    Date.now() + Math.max(1_000, delayMilliseconds),
    id,
  );
}

export async function queueCounts(): Promise<QueueCounts> {
  const db = await database();
  const rows = await db.getAllAsync<{ state: string; count: number }>(
    'SELECT state,COUNT(*) count FROM event_queue GROUP BY state',
  );
  return rows.reduce<QueueCounts>(
    (counts, row) => ({ ...counts, [row.state]: row.count }),
    { pending: 0, rejected: 0 },
  );
}

export async function loadTimerState(): Promise<TimerState | null> {
  const db = await database();
  const row = await db.getFirstAsync<{ value: string }>(
    "SELECT value FROM local_state WHERE key='timer'",
  );
  if (!row) return null;
  try {
    const value = JSON.parse(row.value) as Partial<TimerState>;
    if (
      (value.status !== 'active' && value.status !== 'paused') ||
      typeof value.projectId !== 'string' ||
      (value.taskId !== null && typeof value.taskId !== 'string') ||
      typeof value.startedAt !== 'string' ||
      !Number.isFinite(Date.parse(value.startedAt)) ||
      typeof value.accumulatedSeconds !== 'number' ||
      !Number.isFinite(value.accumulatedSeconds) ||
      value.accumulatedSeconds < 0
    ) {
      throw new Error('Invalid timer state');
    }
    return value as TimerState;
  } catch {
    await db.runAsync("DELETE FROM local_state WHERE key='timer'");
    return null;
  }
}

export async function clearTimerState(): Promise<void> {
  const db = await database();
  await db.runAsync("DELETE FROM local_state WHERE key='timer'");
}

export async function eraseLocalDatabase(): Promise<void> {
  const current = databasePromise;
  databasePromise = null;
  if (current) {
    try {
      await (await current).closeAsync();
    } catch {
      // Continue removing credentials even if the cached connection already closed.
    }
  }
  await SQLite.deleteDatabaseAsync(DATABASE_NAME);
  await SecureStore.deleteItemAsync(DATABASE_KEY, KEY_OPTIONS);
}
