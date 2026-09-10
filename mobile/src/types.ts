export type Enrollment = {
  serverUrl: string;
  deviceToken: string;
  projectId: string | null;
};

export type TrackerTask = {
  id: string;
  name: string;
};

export type TrackerProject = {
  id: string;
  name: string;
  tasks: TrackerTask[];
};

export type TrackerConfiguration = {
  allowedApps: string;
  projects: TrackerProject[];
};

export type TimerState = {
  status: 'active' | 'paused';
  projectId: string;
  taskId: string | null;
  startedAt: string;
  accumulatedSeconds: number;
};

export type EventKind = 'heartbeat' | 'location';

export type QueuePayload = Record<string, unknown> & {
  event_id: string;
};

export type QueuedEvent = {
  id: string;
  kind: EventKind;
  observedAt: string;
  payload: QueuePayload;
};

export type QueueCounts = {
  pending: number;
  rejected: number;
};
