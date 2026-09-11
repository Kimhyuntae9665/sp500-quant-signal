"use strict";

const CACHE_NAME = "sp500-quant-shell-v24";
const APP_SHELL = [
  "./",
  "./index.html",
  "./styles.css?v=24",
  "./app.js?v=24",
  "./gs-lab.html",
  "./gs-lab.css",
  "./gs-lab.js",
  "./manifest.webmanifest",
  "./assets/favicon.svg",
  "./assets/app-icon.svg",
  "./assets/app-icon-maskable.svg",
  "./assets/app-icon-192.png",
  "./assets/app-icon-512.png",
  "./assets/app-icon-maskable-192.png",
  "./assets/app-icon-maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  const allowed = new Set([CACHE_NAME]);
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => !allowed.has(key)).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/api/")) {
    // Never serve cached scores as if they were current. The UI falls back to
    // its clearly labelled DEMO state when the live API is unreachable.
    event.respondWith(fetch(request));
    return;
  }

  if (request.mode === "navigate") {
    const fallback = url.pathname.endsWith("/gs-lab.html")
      ? "./gs-lab.html"
      : "./index.html";
    event.respondWith(
      fetch(request).catch(() => caches.match(fallback)),
    );
    return;
  }

  event.respondWith(networkFirst(request, CACHE_NAME));
});

async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put(request, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw error;
  }
}
