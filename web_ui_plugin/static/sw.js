/* Service worker: makes the interface installable and keeps a friendly offline page.
 *
 * It deliberately caches no pages and no API responses: listings and settings always come
 * from the server (and may sit behind a login). Only static assets are cached.
 */
const VERSION = 'v1';
const SHELL_CACHE = 'vn-shell-' + VERSION;
const ASSET_CACHE = 'vn-assets-' + VERSION;
const PRECACHE = ['/offline', '/static/css/custom.css', '/static/icons/icon-192.png'];
const CDN_HOSTS = ['cdn.jsdelivr.net'];
// Never touched by the worker: live data and anything that changes state
const NETWORK_ONLY = ['/items/fragment', '/api/', '/control', '/login', '/logout', '/security', '/sw.js'];

self.addEventListener('install', function (event) {
    event.waitUntil(
        caches.open(SHELL_CACHE)
            .then(function (cache) {
                // One missing file must not block the install
                return Promise.all(PRECACHE.map(function (url) { return cache.add(url).catch(function () {}); }));
            })
            .then(function () { return self.skipWaiting(); })
    );
});

self.addEventListener('activate', function (event) {
    event.waitUntil(
        caches.keys()
            .then(function (keys) {
                return Promise.all(keys
                    .filter(function (key) { return key !== SHELL_CACHE && key !== ASSET_CACHE; })
                    .map(function (key) { return caches.delete(key); }));
            })
            .then(function () { return self.clients.claim(); })
    );
});

self.addEventListener('fetch', function (event) {
    const request = event.request;
    if (request.method !== 'GET') return;
    const url = new URL(request.url);
    const sameOrigin = url.origin === self.location.origin;

    if (sameOrigin && NETWORK_ONLY.some(function (path) { return url.pathname.startsWith(path); })) return;

    // Page navigations: always the network, the offline page when it is unreachable
    if (request.mode === 'navigate') {
        event.respondWith(fetch(request).catch(function () { return caches.match('/offline'); }));
        return;
    }

    // Static files and CDN libraries: serve from cache, refresh in the background
    const cacheable = (sameOrigin && url.pathname.startsWith('/static/')) || CDN_HOSTS.indexOf(url.hostname) !== -1;
    if (!cacheable) return;
    event.respondWith(
        caches.open(ASSET_CACHE).then(function (cache) {
            return cache.match(request).then(function (cached) {
                const network = fetch(request).then(function (response) {
                    if (response && (response.ok || response.type === 'opaque')) cache.put(request, response.clone());
                    return response;
                }).catch(function () { return cached; });
                return cached || network;
            });
        })
    );
});
