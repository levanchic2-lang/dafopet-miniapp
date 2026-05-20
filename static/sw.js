/* Service Worker for 大风动物医院 TNR 管理系统
 * 策略：
 *   - 静态资源（/static/*）：cache-first，更新后台拉新
 *   - HTML 页面：network-first，离线兜底
 *   - API / 表单 POST：直通网络（不缓存）
 *   - 升级版本时改 CACHE_VERSION，自动清旧
 */
const CACHE_VERSION = 'v18-soap-panel';
const STATIC_CACHE = 'static-' + CACHE_VERSION;
const PAGE_CACHE   = 'pages-' + CACHE_VERSION;

const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== STATIC_CACHE && k !== PAGE_CACHE)
            .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // 只处理同源
  if (url.origin !== self.location.origin) return;
  // 非 GET 直通（POST 表单、API 写）
  if (req.method !== 'GET') return;
  // /uploads/、/api/ 直通，不缓存
  if (url.pathname.startsWith('/uploads/') || url.pathname.startsWith('/api/')) return;
  // 登录、CSRF 相关也直通
  if (url.pathname === '/admin/login' || url.pathname === '/admin/logout') return;

  // 静态资源：cache-first
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const fetchPromise = fetch(req).then((resp) => {
          if (resp && resp.status === 200) {
            const clone = resp.clone();
            caches.open(STATIC_CACHE).then((c) => c.put(req, clone));
          }
          return resp;
        }).catch(() => cached);
        return cached || fetchPromise;
      })
    );
    return;
  }

  // HTML 页面：network-first，断网用缓存兜底
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    event.respondWith(
      fetch(req).then((resp) => {
        if (resp && resp.status === 200) {
          const clone = resp.clone();
          caches.open(PAGE_CACHE).then((c) => c.put(req, clone));
        }
        return resp;
      }).catch(() =>
        caches.match(req).then((cached) =>
          cached || new Response(
            '<!doctype html><meta charset="utf-8"><title>离线</title>' +
            '<div style="font-family:system-ui;text-align:center;padding:3rem;color:#475569;">' +
            '<h2>📡 网络不可用</h2>' +
            '<p>请检查 Wi-Fi 或移动数据，然后<a href="javascript:location.reload()">刷新</a>。</p>' +
            '</div>',
            { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
          )
        )
      )
    );
  }
});
