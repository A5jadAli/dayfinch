(() => {
  const root = document.querySelector('[data-field-tracker]');
  if (!root) return;
  const state = root.querySelector('[data-location-state]');
  const syncState = root.querySelector('[data-sync-state]');
  const accuracy = root.querySelector('[data-accuracy]');
  const toggle = root.querySelector('[data-location-toggle]');
  const csrf = root.dataset.csrf;
  let watchId = null;
  let keyPromise;

  function request(store, mode = 'readonly') {
    return new Promise((resolve, reject) => {
      const open = indexedDB.open('dayfinch-field-v1', 2);
      open.onupgradeneeded = () => {
        if (!open.result.objectStoreNames.contains('events')) open.result.createObjectStore('events', {keyPath: 'id'});
        if (!open.result.objectStoreNames.contains('rejected')) open.result.createObjectStore('rejected', {keyPath: 'id'});
        if (!open.result.objectStoreNames.contains('meta')) open.result.createObjectStore('meta');
      };
      open.onerror = () => reject(open.error);
      open.onsuccess = () => resolve({db: open.result, tx: open.result.transaction(store, mode)});
    });
  }

  async function cryptoKey() {
    if (keyPromise) return keyPromise;
    keyPromise = (async () => {
      if (!crypto.subtle) throw new Error('Encrypted offline storage is unavailable');
      const {db, tx} = await request('meta', 'readwrite');
      const store = tx.objectStore('meta');
      const existing = await new Promise((resolve, reject) => {
        const get = store.get('encryption-key');
        get.onsuccess = () => resolve(get.result);
        get.onerror = () => reject(get.error);
      });
      if (existing) { db.close(); return existing; }
      db.close();
      const key = await crypto.subtle.generateKey({name: 'AES-GCM', length: 256}, false, ['encrypt', 'decrypt']);
      const opened = await request('meta', 'readwrite');
      await new Promise((resolve, reject) => {
        const put = opened.tx.objectStore('meta').put(key, 'encryption-key');
        put.onsuccess = resolve;
        put.onerror = () => reject(put.error);
      });
      opened.db.close();
      return key;
    })();
    return keyPromise;
  }

  async function enqueue(kind, payload) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const encoded = new TextEncoder().encode(JSON.stringify(payload));
    const ciphertext = await crypto.subtle.encrypt({name: 'AES-GCM', iv}, await cryptoKey(), encoded);
    const {db, tx} = await request('events', 'readwrite');
    await new Promise((resolve, reject) => {
      const put = tx.objectStore('events').put({id: payload.event_id, kind, recorded_at: payload.recorded_at || payload.observed_at, iv, ciphertext});
      put.onsuccess = resolve;
      put.onerror = () => reject(put.error);
    });
    db.close();
  }

  async function pending() {
    const {db, tx} = await request('events');
    const rows = await new Promise((resolve, reject) => {
      const get = tx.objectStore('events').getAll();
      get.onsuccess = () => resolve(get.result.sort((a, b) => a.recorded_at.localeCompare(b.recorded_at)));
      get.onerror = () => reject(get.error);
    });
    db.close();
    return rows;
  }

  async function rejectedCount() {
    const {db, tx} = await request('rejected');
    const count = await new Promise((resolve, reject) => {
      const result = tx.objectStore('rejected').count();
      result.onsuccess = () => resolve(result.result);
      result.onerror = () => reject(result.error);
    });
    db.close();
    return count;
  }

  async function decrypt(row) {
    const plain = await crypto.subtle.decrypt({name: 'AES-GCM', iv: row.iv}, await cryptoKey(), row.ciphertext);
    return JSON.parse(new TextDecoder().decode(plain));
  }

  async function remove(id) {
    const {db, tx} = await request('events', 'readwrite');
    await new Promise((resolve, reject) => {
      const deletion = tx.objectStore('events').delete(id);
      deletion.onsuccess = resolve;
      deletion.onerror = () => reject(deletion.error);
    });
    db.close();
  }

  async function quarantine(row, responseStatus, reason = 'server-rejected') {
    const {db, tx} = await request(['events', 'rejected'], 'readwrite');
    await new Promise((resolve, reject) => {
      tx.objectStore('rejected').put({
        ...row,
        rejected_at: new Date().toISOString(),
        response_status: responseStatus,
        rejection_reason: reason
      });
      tx.objectStore('events').delete(row.id);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error('quarantine transaction aborted'));
    });
    db.close();
  }

  async function flush() {
    const rows = await pending();
    if (!rows.length) {
      const rejected = await rejectedCount();
      syncState.textContent = rejected
        ? `${rejected} invalid event${rejected === 1 ? '' : 's'} encrypted and quarantined · Needs attention`
        : '';
      return true;
    }
    syncState.textContent = `Syncing ${rows.length} encrypted event${rows.length === 1 ? '' : 's'}…`;
    for (const row of rows) {
      let body;
      try {
        body = JSON.stringify(await decrypt(row));
      } catch (_error) {
        await quarantine(row, 0, 'local-integrity-failure');
        continue;
      }
      try {
        const response = await fetch(row.kind === 'location' ? '/field/location' : '/field/timer', {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
          body
        });
        if (!response.ok) {
          if ([400, 404, 409, 410, 413, 415, 422].includes(response.status)) {
            await quarantine(row, response.status);
            continue;
          }
          throw new Error(`server returned ${response.status}`);
        }
        await remove(row.id);
      } catch (error) {
        syncState.textContent = `Offline · ${rows.length} event${rows.length === 1 ? '' : 's'} safely queued (${error.message})`;
        return false;
      }
    }
    const rejectedTotal = await rejectedCount();
    syncState.textContent = rejectedTotal
      ? `${rejectedTotal} invalid event${rejectedTotal === 1 ? '' : 's'} encrypted and quarantined · Needs attention`
      : 'All offline events synchronized';
    return true;
  }

  async function recordLocation(position) {
    const payload = {
      event_id: crypto.randomUUID(),
      recorded_at: new Date(position.timestamp).toISOString(),
      latitude: position.coords.latitude,
      longitude: position.coords.longitude,
      accuracy_meters: position.coords.accuracy,
      event_type: 'position'
    };
    try {
      await enqueue('location', payload);
      const synced = await flush();
      state.textContent = synced ? 'Location recorded' : 'Location encrypted and waiting to sync';
      accuracy.textContent = `Accuracy ±${Math.round(position.coords.accuracy)} m`;
    } catch (error) {
      state.textContent = `Location could not be secured: ${error.message}`;
    }
  }

  function locationError(reason) {
    state.textContent = reason.message || 'Location permission is unavailable';
  }

  toggle.addEventListener('click', () => {
    if (watchId !== null) {
      navigator.geolocation.clearWatch(watchId);
      watchId = null;
      toggle.textContent = 'Start location tracking';
      state.textContent = 'Location tracking stopped';
      return;
    }
    if (!navigator.geolocation) return locationError({message: 'This browser does not support location'});
    watchId = navigator.geolocation.watchPosition(recordLocation, locationError, {
      enableHighAccuracy: true, maximumAge: 15000, timeout: 30000
    });
    toggle.textContent = 'Stop location tracking';
    state.textContent = 'Requesting precise location…';
  });

  root.querySelectorAll('[data-field-timer]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const action = new URL(form.action).pathname.split('/').pop();
    const data = new FormData(form);
    const payload = {
      event_id: crypto.randomUUID(),
      observed_at: new Date().toISOString(),
      action,
      project_id: data.get('project_id') || null,
      task_id: data.get('task_id') || null
    };
    try {
      await enqueue('timer', payload);
      if (await flush()) location.reload();
    } catch (error) {
      syncState.textContent = `Timer action could not be secured: ${error.message}`;
    }
  }));

  window.addEventListener('online', () => flush().then(done => { if (done) location.reload(); }));
  flush();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/service-worker.js');
})();
