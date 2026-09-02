// Minimal service worker — its only job is to exist with a fetch handler, which is one of the
// requirements Chrome/Edge/Android check before showing the automatic "Install app" prompt.
// It intentionally does NOT cache anything: this app's data (orders, notifications, catalog)
// changes constantly via live polling, so caching responses here would risk showing stale data.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  event.respondWith(fetch(event.request));
});
