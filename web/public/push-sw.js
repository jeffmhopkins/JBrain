/* JBrain Web Push handlers, imported into the Workbox-generated service worker
   via workbox.importScripts (see vite.config.ts). Plain JS — no build step.
   iOS installed PWAs (16.4+) require a visible notification per push. */
self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) {}
  var title = d.title || "JBrain";
  var body = d.body || "You have items to review.";
  var url = d.url || "/shares";
  var count = typeof d.count === "number" ? d.count : 0;
  var tag = d.tag || "jbrain-reviews";   // constant tag => the OS collapses a burst into one banner
  event.waitUntil((async () => {
    await self.registration.showNotification(title, {
      body: body, tag: tag, renotify: true,
      icon: "icon.svg", badge: "icon.svg",
      data: { url: url },
    });
    // Update the home-screen icon badge while the app is closed (count rides in
    // the payload — the SW has no auth to fetch it).
    if (self.navigator && "setAppBadge" in self.navigator) {
      try { count > 0 ? await self.navigator.setAppBadge(count) : await self.navigator.clearAppBadge(); }
      catch (e) {}
    }
    // Let any open page update its bell live.
    var cs = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (var i = 0; i < cs.length; i++) cs[i].postMessage({ type: "jbrain-review", count: count });
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  var target = (event.notification.data && event.notification.data.url) || "/shares";
  event.waitUntil((async () => {
    var cs = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (var i = 0; i < cs.length; i++) {
      var c = cs[i];
      if ("focus" in c) { try { if (c.navigate) c.navigate(target); } catch (e) {} return c.focus(); }
    }
    if (self.clients.openWindow) return self.clients.openWindow(target);
  })());
});
