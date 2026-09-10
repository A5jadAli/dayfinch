import type {
  Enrollment,
  TrackerConfiguration,
  TrackerProject,
  TrackerTask,
} from './types';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Expected an object');
  }
  return value as Record<string, unknown>;
}

function string(value: unknown, label: string, maximum: number): string {
  if (typeof value !== 'string') throw new Error(`${label} is missing`);
  const normalized = value.trim();
  if (!normalized || normalized.length > maximum) {
    throw new Error(`${label} is invalid`);
  }
  return normalized;
}

export function parseEnrollment(input: string): Enrollment {
  if (!input.trim() || input.length > 16_384) {
    throw new Error('The enrollment payload has an invalid size');
  }
  let value: Record<string, unknown>;
  try {
    value = record(JSON.parse(input));
  } catch (error) {
    throw new Error('Paste the mobile enrollment JSON from Dayfinch', {
      cause: error,
    });
  }
  const serverUrl = string(value.server_url, 'Server URL', 2_048).replace(/\/+$/, '');
  let parsed: URL;
  try {
    parsed = new URL(serverUrl);
  } catch (error) {
    throw new Error('The server URL is invalid', { cause: error });
  }
  if (
    parsed.protocol !== 'https:' ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error('The mobile tracker requires a public HTTPS Dayfinch URL');
  }
  const deviceToken = string(value.device_token, 'Device token', 512);
  if (deviceToken.length < 32 || /\s/.test(deviceToken)) {
    throw new Error('The device token is invalid');
  }
  const projectId = value.project_id == null ? null : string(value.project_id, 'Project ID', 64);
  if (projectId !== null && !UUID.test(projectId)) {
    throw new Error('The project ID is invalid');
  }
  return { serverUrl, deviceToken, projectId };
}

function parseTask(value: unknown): TrackerTask {
  const item = record(value);
  const id = string(item.id, 'Task ID', 64);
  if (!UUID.test(id)) throw new Error('Task ID is invalid');
  return { id, name: string(item.name, 'Task name', 200) };
}

function parseProject(value: unknown): TrackerProject {
  const item = record(value);
  const id = string(item.id, 'Project ID', 64);
  if (!UUID.test(id)) throw new Error('Project ID is invalid');
  if (!Array.isArray(item.tasks) || item.tasks.length > 5_000) {
    throw new Error('Project tasks are invalid');
  }
  return {
    id,
    name: string(item.name, 'Project name', 200),
    tasks: item.tasks.map(parseTask),
  };
}

export function parseConfiguration(value: unknown): TrackerConfiguration {
  const item = record(value);
  if (!Array.isArray(item.projects) || item.projects.length > 500) {
    throw new Error('The project catalogue is invalid');
  }
  const projects = item.projects.map(parseProject);
  if (projects.reduce((total, project) => total + project.tasks.length, 0) > 50_000) {
    throw new Error('The task catalogue is too large');
  }
  const projectIds = new Set<string>();
  const taskIds = new Set<string>();
  for (const project of projects) {
    if (projectIds.has(project.id)) throw new Error('The project catalogue contains duplicate IDs');
    projectIds.add(project.id);
    for (const task of project.tasks) {
      if (taskIds.has(task.id)) throw new Error('The task catalogue contains duplicate IDs');
      taskIds.add(task.id);
    }
  }
  return {
    allowedApps:
      typeof item.allowed_apps === 'string' ? item.allowed_apps : 'all',
    projects,
  };
}

export function retryDelayMilliseconds(attempt: number, retryAfter?: string | null): number {
  if (retryAfter) {
    const seconds = Number(retryAfter);
    if (Number.isFinite(seconds) && seconds >= 0) {
      return Math.min(3_600_000, Math.max(1_000, Math.round(seconds * 1_000)));
    }
    const date = Date.parse(retryAfter);
    if (Number.isFinite(date)) {
      return Math.min(3_600_000, Math.max(1_000, date - Date.now()));
    }
  }
  const exponent = Math.min(10, Math.max(0, attempt));
  return Math.min(3_600_000, 1_000 * 2 ** exponent);
}

export function isPermanentStatus(status: number): boolean {
  return [400, 401, 403, 404, 409, 410, 413, 415, 422].includes(status);
}

export function shouldHaltTimer(
  status: number,
  kind: 'heartbeat' | 'location',
  timerStatus: unknown,
): boolean {
  return status === 401 || (
    kind === 'heartbeat' &&
    timerStatus === 'active' &&
    [403, 404, 410, 422].includes(status)
  );
}
