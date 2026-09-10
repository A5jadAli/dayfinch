import { describe, expect, it, vi } from 'vitest';

import {
  isPermanentStatus,
  parseConfiguration,
  parseEnrollment,
  retryDelayMilliseconds,
  shouldHaltTimer,
} from './validation';

const PROJECT = '11111111-1111-4111-8111-111111111111';
const TASK = '22222222-2222-4222-8222-222222222222';

describe('mobile enrollment validation', () => {
  it('accepts a bounded HTTPS enrollment and normalizes trailing slashes', () => {
    expect(parseEnrollment(JSON.stringify({
      server_url: 'https://work.example.test///',
      device_token: 't'.repeat(48),
      project_id: PROJECT,
    }))).toEqual({
      serverUrl: 'https://work.example.test',
      deviceToken: 't'.repeat(48),
      projectId: PROJECT,
    });
  });

  it.each([
    'http://work.example.test',
    'https://user:secret@work.example.test',
    'https://work.example.test?token=leak',
    'https://work.example.test#fragment',
  ])('rejects an unsafe URL: %s', (serverUrl) => {
    expect(() => parseEnrollment(JSON.stringify({
      server_url: serverUrl,
      device_token: 't'.repeat(48),
    }))).toThrow();
  });

  it('rejects malformed credentials and oversized input', () => {
    expect(() => parseEnrollment('{}')).toThrow();
    expect(() => parseEnrollment('x'.repeat(16_385))).toThrow(/size/);
    expect(() => parseEnrollment(JSON.stringify({
      server_url: 'https://work.example.test',
      device_token: 'short',
    }))).toThrow(/token/);
  });
});

describe('configuration validation', () => {
  it('parses projects and tasks', () => {
    expect(parseConfiguration({
      allowed_apps: 'all',
      projects: [{ id: PROJECT, name: 'Road work', tasks: [{ id: TASK, name: 'Survey' }] }],
    })).toEqual({
      allowedApps: 'all',
      projects: [{ id: PROJECT, name: 'Road work', tasks: [{ id: TASK, name: 'Survey' }] }],
    });
  });

  it('rejects duplicate and malformed identifiers', () => {
    expect(() => parseConfiguration({ projects: [
      { id: PROJECT, name: 'A', tasks: [] },
      { id: PROJECT, name: 'B', tasks: [] },
    ] })).toThrow(/duplicate/);
    expect(() => parseConfiguration({ projects: [
      { id: 'not-a-uuid', name: 'A', tasks: [] },
    ] })).toThrow(/Project ID/);
  });
});

describe('retry classification', () => {
  it('caps exponential delays and respects numeric Retry-After', () => {
    expect(retryDelayMilliseconds(0)).toBe(1_000);
    expect(retryDelayMilliseconds(4)).toBe(16_000);
    expect(retryDelayMilliseconds(99)).toBe(1_024_000);
    expect(retryDelayMilliseconds(1, '3601')).toBe(3_600_000);
  });

  it('supports HTTP-date Retry-After and permanent status classification', () => {
    vi.setSystemTime(new Date('2026-09-10T00:00:00Z'));
    expect(retryDelayMilliseconds(1, 'Thu, 10 Sep 2026 00:00:09 GMT')).toBe(9_000);
    vi.useRealTimers();
    expect(isPermanentStatus(422)).toBe(true);
    expect(isPermanentStatus(429)).toBe(false);
    expect(isPermanentStatus(503)).toBe(false);
  });

  it('halts local tracking for revocation or a rejected active heartbeat', () => {
    expect(shouldHaltTimer(401, 'location', undefined)).toBe(true);
    expect(shouldHaltTimer(422, 'heartbeat', 'active')).toBe(true);
    expect(shouldHaltTimer(422, 'heartbeat', 'paused')).toBe(false);
    expect(shouldHaltTimer(409, 'location', undefined)).toBe(false);
    expect(shouldHaltTimer(503, 'heartbeat', 'active')).toBe(false);
  });
});
