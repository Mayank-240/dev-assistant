/* Service worker for the ADA console PWA.
 *
 * Job: surface Web Push messages (agent asks / permission requests) as system
 * notifications and deep-link back into the console when tapped. Registered
 * and subscribed by the console UI; the server sends payloads shaped
 * {title, body, tag, url} (see src/ai_dev_assistant/web/push.py).
 */

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = { title: 'AI Dev Assistant', body: event.data ? event.data.text() : '' };
  }
  const title = payload.title || 'AI Dev Assistant';
  event.waitUntil(
    self.registration.showNotification(title, {
      body: payload.body || '',
      tag: payload.tag || undefined,
      data: { url: payload.url || '/app' },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/app';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      // An open console tab: steer it to the deep link and focus it.
      for (const client of clients) {
        if ('focus' in client) {
          return client.navigate(url).then((c) => (c || client).focus());
        }
      }
      return self.clients.openWindow(url);
    })
  );
});

// No 'fetch' handler on purpose: the console is a live dashboard over server
// state, so offline caching would only show stale runs. Push + notification
// click handling is all this worker does.
