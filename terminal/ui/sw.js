/* Service worker: offline app shell, network-first API, push notifications for alerts. */
const VERSION = 'v30.2-pwa-2';
const SHELL = ['/', '/static/styles.css', '/static/app.js', '/static/charts.js', '/manifest.webmanifest', '/static/icons/icon-192.png', '/static/icons/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname === '/ws' || url.pathname === '/metrics') {
    // live data is never served from cache; fail fast when offline so the UI shows "disconnected"
    e.respondWith(fetch(e.request).catch(() => new Response(JSON.stringify({ ok: false, error: 'OFFLINE' }), { status: 503, headers: { 'Content-Type': 'application/json' } })));
    return;
  }
  // app shell: cache first (navigations always map to the cached "/" regardless of ?view=), refresh in background
  const key = e.request.mode === 'navigate' ? '/' : e.request;
  e.respondWith(caches.match(key, { ignoreSearch: true }).then(hit => {
    const refresh = fetch(e.request).then(res => { if (res.ok) caches.open(VERSION).then(c => c.put(key, res.clone())); return res; })
      .catch(() => hit || new Response('<!doctype html><meta charset="utf-8"><title>Offline</title><body style="background:#0b1220;color:#e2e8f0;font-family:system-ui;padding:40px"><h2>AI Terminal is offline</h2><p>No connection to the terminal server. Alerts resume when the network is back.</p>', { status: 503, headers: { 'Content-Type': 'text/html' } }));
    return hit || refresh;
  }));
});

self.addEventListener('push', e => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch (_) { data = { title: 'AI Terminal', body: e.data ? e.data.text() : '' }; }
  const level = data.level || 'INFO';
  e.waitUntil(self.registration.showNotification(data.title || 'AI Terminal', {
    body: data.body || '', icon: '/static/icons/icon-192.png', badge: '/static/icons/icon-192.png', tag: data.tag || (level + ':' + (data.category || '')),
    renotify: level === 'CRITICAL', requireInteraction: level === 'CRITICAL', vibrate: level === 'CRITICAL' ? [200, 100, 200, 100, 400] : [120],
    data: { url: data.url || '/?view=' + (data.view || 'overview') },
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) { if ('focus' in c) { c.navigate(target); return c.focus(); } }
    return self.clients.openWindow(target);
  }));
});
