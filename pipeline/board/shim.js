// window.claude for the self-hosted Pipeline board (2026-10-09). Gives the
// Artifact page the three capabilities it uses — db, comments, user — backed by
// server.py, so the page code is unchanged. One SSE stream feeds every
// onSnapshot; after a reconnect each listener re-fetches, so a write missed
// while the stream was down is never lost.
(function () {
  "use strict";
  const listeners = new Set(); // { match(path), onEvent(ev), resync() }
  let es = null, wasDown = false;
  function stream() {
    if (es) return;
    es = new EventSource("/api/events");
    es.onmessage = (m) => { let ev; try { ev = JSON.parse(m.data); } catch (_) { return; } listeners.forEach((l) => { if (l.match(ev.path)) l.onEvent(ev); }); };
    es.onerror = () => { wasDown = true; };
    es.onopen = () => { if (wasDown) { wasDown = false; listeners.forEach((l) => l.resync()); } };
  }
  const fail = (code) => { const e = new Error(code); e.code = code; return e; };
  async function api(path, body) {
    let r;
    try { r = await fetch(path, body === undefined ? { cache: "no-store" } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
    catch (_) { throw fail("unavailable"); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw fail(j.error || "http_" + r.status);
    return j;
  }
  const snap = (id, exists, data, version) => ({ id, exists, version, data: () => (data == null ? undefined : JSON.parse(JSON.stringify(data))) });

  function doc(path) {
    const id = path.split("/").pop();
    const write = (op, data) => api("/api/write", { path, op, data }).then((j) => ({ version: j.version }));
    return {
      id, path,
      get: () => api("/api/doc?path=" + encodeURIComponent(path)).then((j) => snap(id, j.exists, j.data, j.version)),
      set: (data) => write("set", data),
      update: (data) => write("update", data),
      onSnapshot(ok, bad) {
        let ver = -1;
        const fetchNow = () => api("/api/doc?path=" + encodeURIComponent(path)).then((j) => { if (j.version >= ver) { ver = j.version; ok(snap(id, j.exists, j.data, j.version)); } }, (e) => { if (ver < 0 && bad) bad(e); });
        const l = { match: (p) => p === path, onEvent: (ev) => { if (ev.version > ver) { ver = ev.version; ok(snap(id, true, ev.data, ev.version)); } }, resync: fetchNow };
        listeners.add(l); stream(); fetchNow();
        return () => listeners.delete(l);
      },
    };
  }

  function collection(name) {
    return {
      doc: (id) => doc(name + "/" + id),
      onSnapshot(ok, bad) {
        const docs = new Map(); let loaded = false;
        const emit = () => ok({ size: docs.size, empty: !docs.size, docs: [...docs.values()].map((d) => snap(d.id, true, d.data, d.version)) });
        const fetchNow = () => api("/api/list?path=" + encodeURIComponent(name)).then((j) => {
          docs.clear(); j.docs.forEach((d) => docs.set(d.id, d)); loaded = true; emit();
        }, (e) => { if (!loaded && bad) bad(e); });
        const l = {
          match: (p) => p.startsWith(name + "/") && p.indexOf("/", name.length + 1) < 0,
          onEvent: (ev) => { const id = ev.path.slice(name.length + 1); const cur = docs.get(id); if (cur && cur.version >= ev.version) return; docs.set(id, { id, data: ev.data, version: ev.version }); if (loaded) emit(); },
          resync: fetchNow,
        };
        listeners.add(l); stream(); fetchNow();
        return () => listeners.delete(l);
      },
    };
  }

  const caps = {
    db: { doc, collection },
    comments: {
      canSendToClaude: async () => "available",
      anchorFor: async () => null,
      sendToClaude: async ({ text }) => {
        const j = await api("/api/press", { text, key: "web:" + Date.now() });
        if (!j.ok) throw fail("route_failed");
        return j;
      },
    },
    user: { id: async () => "rider", can: async () => true },
  };
  window.claude = { use: async (cap) => caps[cap] || null };
})();
