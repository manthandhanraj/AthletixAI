/* AthletixAI service worker — enables installability + offline shell */
// v2: stops proxying third-party requests (see the fetch handler). Bumping
// the name also makes `activate` below purge everything v1 cached.
const CACHE = 'athletix-v2';
const SHELL = ['/', '/static/icon-192.png', '/static/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                       // never cache writes
  const url = new URL(req.url);

  // Same origin only. Third-party assets - Chart.js and jsPDF on cdnjs,
  // Google Fonts, three.js on jsDelivr - are left to the browser, which
  // loads them under the PAGE's CSP (script-src / style-src / font-src).
  //
  // Proxying them was the charts bug: a fetch() made inside this worker is
  // judged by the worker's connect-src, which does not list cdnjs or Google
  // Fonts. Every such request therefore failed once the worker was in
  // control, and the fallback answered the <script> with the cached HTML
  // home page - so Chart.js never loaded on any visit after the first.
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;           // API always live

  // network-first, cache as offline fallback
  e.respondWith(
    fetch(req)
      .then(res => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req).then(hit => {
        if (hit) return hit;
        // Only a page navigation may fall back to the cached shell. Handing
        // HTML to a script, stylesheet or image request is never right.
        if (req.mode === 'navigate') return caches.match('/');
        return Response.error();
      }))
  );
});
