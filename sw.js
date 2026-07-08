const CACHE_NAME = 'arb-studio-v1';
const URLS_TO_CACHE = [
  '/Abrt-scraper/index.html',
  '/Abrt-scraper/manifest.json'
  // Add icons or extra files here if you want.
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(URLS_TO_CACHE))
  );
});

self.addEventListener('fetch', (event) => {
  event.respondWith(
    caches.match(event.request).then((response) => response || fetch(event.request))
  );
});
