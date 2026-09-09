// Yorik service worker — Web Push only. No caching: the app is served
// by the same box it talks to, and a stale shell would hurt more than
// a cold load helps.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = { title: "Yorik", body: "", url: "/r/home", tag: "yorik" };
  try { data = { ...data, ...(event.data ? event.data.json() : {}) }; } catch (e) { /* plain text */ }
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: "/r/butler-icon-192.png",
    badge: "/r/butler-icon-192.png",
    tag: data.tag,
    renotify: true,
    data: { url: data.url },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/r/home";
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if ("focus" in c) { await c.focus(); if ("navigate" in c) { try { await c.navigate(url); } catch (e) {} } return; }
    }
    await self.clients.openWindow(url);
  })());
});
