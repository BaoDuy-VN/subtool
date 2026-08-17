// SubTool Service Worker - PWA Offline Support
const CACHE_NAME = 'subtool-v1';
const STATIC_CACHE = 'subtool-static-v1';

// Assets to cache on install
const STATIC_ASSETS = [
  '/',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

// Install event - cache static assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => {
      return cache.addAll(STATIC_ASSETS);
    })
  );
  self.skipWaiting();
});

// Activate event - clean up old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME && name !== STATIC_CACHE)
          .map((name) => caches.delete(name))
      );
    })
  );
  self.clients.claim();
});

// Fetch event - serve from cache, fallback to network
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip API requests (they need real server)
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(request).catch(() => {
        return new Response(JSON.stringify({
          error: 'offline',
          message: 'Bạn đang offline. Vui lòng kết nối mạng để sử dụng tính năng này.'
        }), {
          headers: { 'Content-Type': 'application/json' },
          status: 503
        });
      })
    );
    return;
  }

  // Static assets - cache first, then network
  if (url.pathname.startsWith('/static/') || url.pathname === '/') {
    event.respondWith(
      caches.match(request).then((cachedResponse) => {
        if (cachedResponse) {
          return cachedResponse;
        }
        return fetch(request).then((response) => {
          // Cache successful responses
          if (response.ok) {
            const responseClone = response.clone();
            caches.open(STATIC_CACHE).then((cache) => {
              cache.put(request, responseClone);
            });
          }
          return response;
        });
      }).catch(() => {
        // Offline fallback
        if (request.headers.get('accept')?.includes('text/html')) {
          return caches.match('/');
        }
      })
    );
    return;
  }

  // Default: network first, fallback to cache
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(request, responseClone);
          });
        }
        return response;
      })
      .catch(() => caches.match(request))
  );
});

// Background sync for failed uploads (future enhancement)
self.addEventListener('sync', (event) => {
  if (event.tag === 'retry-upload') {
    event.waitUntil(retryUpload());
  }
});

async function retryUpload() {
  // Placeholder for future background sync feature
  console.log('Background sync: retry upload');
}

// Push notifications (future enhancement)
self.addEventListener('push', (event) => {
  const data = event.data?.json() || {};
  const title = data.title || 'SubTool';
  const options = {
    body: data.body || 'Video của bạn đã được xử lý xong!',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    vibrate: [200, 100, 200],
    data: data
  };
  event.waitUntil(self.registration.showNotification(title, options));
});
