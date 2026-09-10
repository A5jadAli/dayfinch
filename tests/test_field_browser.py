from __future__ import annotations

import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HARNESS = b"""<!doctype html>
<html><body>
<main data-field-tracker data-csrf="test-csrf">
  <p data-location-state></p><p data-sync-state></p><p data-accuracy></p>
  <button data-location-toggle>Location</button>
  <form data-field-timer action="/field/start">
    <input name="project_id" value="private-project-42">
    <input name="task_id" value="private-task-7">
  </form>
</main>
<output id="result" data-result="RUNNING">RUNNING</output>
<script>
  const stage = sessionStorage.getItem('stage') || 'offline';
  const reconnecting = stage !== 'offline';
  window.fetch = async (_url, options) => {
    if (!reconnecting) throw new TypeError('simulated Wi-Fi outage');
    sessionStorage.setItem('uploaded-body', options.body);
    return new Response('', {status: 201});
  };
</script>
<script src="/ui/static/field.js"></script>
<script>
  const result = document.querySelector('#result');
  const fail = message => {
    result.dataset.result = 'FAIL';
    result.textContent = `FAIL: ${message}`;
    new Image().src = '/done';
  };
  const openDb = () => new Promise((resolve, reject) => {
    const request = indexedDB.open('dayfinch-field-v1', 2);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const all = async storeName => {
    const db = await openDb();
    const rows = await new Promise((resolve, reject) => {
      const request = db.transaction(storeName).objectStore(storeName).getAll();
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    db.close();
    return rows;
  };
  const put = async (storeName, row) => {
    const db = await openDb();
    await new Promise((resolve, reject) => {
      const transaction = db.transaction(storeName, 'readwrite');
      transaction.objectStore(storeName).put(row);
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
    });
    db.close();
  };
  const waitFor = async predicate => {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      const value = await predicate();
      if (value) return value;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('browser queue operation timed out');
  };

  (async () => {
    if (!reconnecting) {
      document.querySelector('form').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
      const rows = await waitFor(async () => {
        const queued = await all('events');
        return queued.length ? queued : null;
      });
      if (rows.length !== 1 || rows[0].kind !== 'timer') throw new Error('timer event was not queued');
      if (!(rows[0].iv instanceof Uint8Array) || rows[0].iv.byteLength !== 12) throw new Error('AES-GCM IV is invalid');
      if (!(rows[0].ciphertext instanceof ArrayBuffer)) throw new Error('ciphertext was not stored as binary');
      const raw = new TextDecoder().decode(rows[0].ciphertext);
      if (raw.includes('private-project-42') || raw.includes('private-task-7')) throw new Error('offline fields leaked in plaintext');
      sessionStorage.setItem('stage', 'valid-reconnect');
      location.reload();
      return;
    }

    if (stage === 'valid-reconnect') {
      await waitFor(async () => (await all('events')).length === 0);
      const rejected = await all('rejected');
      const uploaded = JSON.parse(sessionStorage.getItem('uploaded-body') || '{}');
      if (rejected.length) throw new Error('valid reconnect event was quarantined');
      if (uploaded.action !== 'start') throw new Error('timer action was not recovered');
      if (uploaded.project_id !== 'private-project-42') throw new Error('project attribution was not recovered');
      if (uploaded.task_id !== 'private-task-7') throw new Error('task attribution was not recovered');
      await put('events', {
        id: 'corrupt-event',
        kind: 'timer',
        recorded_at: new Date().toISOString(),
        iv: crypto.getRandomValues(new Uint8Array(12)),
        ciphertext: crypto.getRandomValues(new Uint8Array(32)).buffer
      });
      sessionStorage.setItem('stage', 'corrupt-reconnect');
      location.reload();
      return;
    }

    await waitFor(async () => (await all('events')).length === 0 && (await all('rejected')).length === 1);
    const rejected = await all('rejected');
    if (rejected[0].rejection_reason !== 'local-integrity-failure') throw new Error('corrupt event lacked integrity reason');
    if (!document.querySelector('[data-sync-state]').textContent.includes('Needs attention')) throw new Error('quarantine was not surfaced');
    result.dataset.result = 'PASS';
    result.textContent = 'PASS';
    new Image().src = '/done';
  })().catch(error => fail(error.message));
</script>
<script src="/hold"></script>
</body></html>"""

POLICY_HARNESS = b"""<!doctype html>
<html><body>
<main data-field-tracker data-csrf="test-csrf">
  <p data-location-state></p><p data-sync-state></p><p data-accuracy></p>
  <button data-location-toggle>Location</button>
  <form data-field-timer action="/timer/start">
    <input name="project_id" value="project-42">
  </form>
</main>
<output id="result" data-result="RUNNING">RUNNING</output>
<script>
  window.fetch = async () => new Response(
    JSON.stringify({detail: 'Desktop tracking is required by policy'}),
    {status: 403, headers: {'Content-Type': 'application/json'}}
  );
</script>
<script src="/ui/static/field.js"></script>
<script>
  const result = document.querySelector('#result');
  const finish = (status, message) => {
    result.dataset.result = status;
    result.textContent = `${status}: ${message}`;
    new Image().src = '/done';
  };
  const rows = storeName => new Promise((resolve, reject) => {
    const request = indexedDB.open('dayfinch-field-v1', 2);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains('events')) request.result.createObjectStore('events', {keyPath: 'id'});
      if (!request.result.objectStoreNames.contains('rejected')) request.result.createObjectStore('rejected', {keyPath: 'id'});
      if (!request.result.objectStoreNames.contains('meta')) request.result.createObjectStore('meta');
    };
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const db = request.result;
      const read = db.transaction(storeName).objectStore(storeName).getAll();
      read.onerror = () => reject(read.error);
      read.onsuccess = () => { db.close(); resolve(read.result); };
    };
  });
  (async () => {
    await new Promise(resolve => setTimeout(resolve, 250));
    if (!sessionStorage.getItem('policy-submitted')) {
      sessionStorage.setItem('policy-submitted', 'true');
      document.querySelector('form').dispatchEvent(
        new Event('submit', {bubbles: true, cancelable: true})
      );
      return;
    }
    for (let attempt = 0; attempt < 50; attempt += 1) {
      const pending = await rows('events');
      const rejected = await rows('rejected');
      if (!pending.length && rejected.length === 1) {
        if (rejected[0].rejection_reason !== 'tracking-app-disabled') {
          throw new Error('policy rejection reason was not retained');
        }
        if (rejected[0].response_status !== 403) {
          throw new Error('policy rejection status was not retained');
        }
        if (!document.querySelector('[data-sync-state]').textContent.includes('Needs attention')) {
          throw new Error('rejected policy event was not surfaced');
        }
        finish('PASS', 'policy event quarantined');
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('policy event remained in retry queue');
  })().catch(error => finish('FAIL', error.message));
</script>
<script src="/hold"></script>
</body></html>"""


def _run_browser_harness(tmp_path: Path, harness: bytes):
    chrome = next(
        (
            executable
            for name in ("google-chrome", "chromium", "chromium-browser")
            if (executable := shutil.which(name))
        ),
        None,
    )
    if not chrome:
        pytest.skip(
            "Chrome/Chromium is required for the field browser integration test"
        )

    field_script = Path("ui/static/field.js").resolve().read_bytes()
    completed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/":
                body, content_type = harness, "text/html; charset=utf-8"
                status = 200
            elif self.path == "/ui/static/field.js":
                body, content_type = field_script, "text/javascript; charset=utf-8"
                status = 200
            elif self.path == "/hold":
                completed.wait(timeout=12)
                body, content_type, status = b"", "text/javascript", 200
            elif self.path == "/done":
                completed.set()
                body, content_type, status = b"", "image/gif", 204
            else:
                body, content_type = b"", "text/plain"
                status = 404
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                chrome,
                "--headless=new",
                "--no-sandbox",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
                f"--user-data-dir={tmp_path / 'chrome-profile'}",
                "--virtual-time-budget=8000",
                "--dump-dom",
                f"http://127.0.0.1:{server.server_port}/",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return result


def test_field_browser_encrypts_offline_then_replays_on_reconnect(tmp_path: Path):
    result = _run_browser_harness(tmp_path, HARNESS)

    assert result.returncode == 0, result.stderr[-2000:]
    assert 'data-result="PASS"' in result.stdout, result.stdout[-3000:]


def test_field_browser_quarantines_desktop_only_policy_rejection(tmp_path: Path):
    result = _run_browser_harness(tmp_path, POLICY_HARNESS)

    assert result.returncode == 0, result.stderr[-2000:]
    assert 'data-result="PASS"' in result.stdout, result.stdout[-3000:]
