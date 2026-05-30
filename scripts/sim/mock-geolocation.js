/*
 * mock-geolocation.js — fake GPS for Go Mode field-testing without a phone.
 *
 * Injected into the running frontend (via Playwright browser_evaluate or the
 * devtools console). It replaces navigator.geolocation so that getCurrentPosition
 * and watchPosition emit a scripted track of coordinates, letting us "ride" a
 * planned itinerary — and deliberately drift off it — to exercise Go Mode's
 * approach/arrival alerts, leg transitions, and off-route re-route suggestion.
 *
 * The app polls position roughly every 30s (responsive-webapp.js), so the sim
 * keeps emitting the *current* simulated fix continuously; pollers and active
 * watchers both see fresh coordinates whenever they read.
 *
 * Control surface (window.__sim):
 *   __sim.loadTrack(coords, opts)  coords = [[lat,lon], ...]; opts.speedMps (default 8 ≈ 29 km/h)
 *   __sim.play()                   start advancing along the loaded track
 *   __sim.pause()                  hold at current point
 *   __sim.jumpTo(lat, lon, [hdg])  teleport to a point (e.g. start of a leg)
 *   __sim.offsetMeters(dN, dE)     nudge the live fix N/E by meters (drift off-route)
 *   __sim.clearOffset()            remove the drift
 *   __sim.status()                 { lat, lon, heading, idx, total, playing }
 *   __sim.stop()                   restore the real navigator.geolocation
 *
 * Helper to pull a real route out of the running app's Redux store:
 *   __sim.trackFromActiveItinerary([legIndex])  -> coords for the whole trip,
 *       or just one leg; decodes OTP encoded polylines from the active itinerary.
 */
(function installMockGeolocation() {
  if (window.__sim && window.__sim.__installed) {
    console.warn('[sim] already installed; call __sim.stop() first to reinstall');
    return window.__sim;
  }

  const real = navigator.geolocation;
  const R = 6371000; // earth radius, meters

  const state = {
    track: [],          // [[lat,lon],...]
    cumDist: [],        // cumulative meters along track
    speedMps: 8,
    dist: 0,            // current distance along track, meters
    heading: 0,
    playing: false,
    offset: { n: 0, e: 0 }, // meters north/east applied to the live fix
    watchers: new Map(),
    nextWatchId: 1,
    timer: null,
    lastTick: null
  };

  const toRad = (d) => (d * Math.PI) / 180;
  const toDeg = (r) => (r * 180) / Math.PI;

  function haversine(a, b) {
    const dLat = toRad(b[0] - a[0]);
    const dLon = toRad(b[1] - a[1]);
    const la1 = toRad(a[0]);
    const la2 = toRad(b[0]);
    const h =
      Math.sin(dLat / 2) ** 2 +
      Math.cos(la1) * Math.cos(la2) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  function bearing(a, b) {
    const la1 = toRad(a[0]);
    const la2 = toRad(b[0]);
    const dLon = toRad(b[1] - a[1]);
    const y = Math.sin(dLon) * Math.cos(la2);
    const x =
      Math.cos(la1) * Math.sin(la2) -
      Math.sin(la1) * Math.cos(la2) * Math.cos(dLon);
    return (toDeg(Math.atan2(y, x)) + 360) % 360;
  }

  // Apply a north/east meter offset to a [lat,lon] point.
  function applyOffset(lat, lon) {
    if (!state.offset.n && !state.offset.e) return [lat, lon];
    const dLat = state.offset.n / R;
    const dLon = state.offset.e / (R * Math.cos(toRad(lat)));
    return [lat + toDeg(dLat), lon + toDeg(dLon)];
  }

  // Interpolate a [lat,lon] at the current cumulative distance along the track.
  function pointAtDist(d) {
    const t = state.track;
    if (t.length === 0) return null;
    if (t.length === 1) return { pos: t[0], hdg: state.heading };
    const cum = state.cumDist;
    const total = cum[cum.length - 1];
    const dd = Math.max(0, Math.min(d, total));
    let i = 1;
    while (i < cum.length && cum[i] < dd) i++;
    if (i >= cum.length) return { pos: t[t.length - 1], hdg: bearing(t[t.length - 2], t[t.length - 1]) };
    const segStart = cum[i - 1];
    const segLen = cum[i] - segStart || 1;
    const f = (dd - segStart) / segLen;
    const a = t[i - 1];
    const b = t[i];
    return {
      pos: [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f],
      hdg: bearing(a, b)
    };
  }

  function currentFix() {
    let lat, lon, hdg;
    if (state.track.length) {
      const p = pointAtDist(state.dist);
      [lat, lon] = p.pos;
      hdg = p.hdg;
    } else {
      return null;
    }
    [lat, lon] = applyOffset(lat, lon);
    state.heading = hdg;
    return {
      coords: {
        latitude: lat,
        longitude: lon,
        accuracy: 5,
        altitude: null,
        altitudeAccuracy: null,
        heading: hdg,
        speed: state.playing ? state.speedMps : 0
      },
      timestamp: Date.now()
    };
  }

  function emit() {
    const fix = currentFix();
    if (!fix) return;
    state.watchers.forEach((cb) => {
      try { cb(fix); } catch (e) { /* swallow */ }
    });
  }

  function tick() {
    const now = Date.now();
    const dt = (now - state.lastTick) / 1000;
    state.lastTick = now;
    if (state.playing && state.track.length > 1) {
      state.dist += state.speedMps * dt;
      const total = state.cumDist[state.cumDist.length - 1];
      if (state.dist >= total) {
        state.dist = total;
        state.playing = false;
        console.log('[sim] reached end of track');
      }
    }
    emit();
  }

  function ensureTimer() {
    if (state.timer) return;
    state.lastTick = Date.now();
    state.timer = setInterval(tick, 1000); // 1 Hz; app polls slower but watchers get fresh fixes
  }

  function recomputeCum() {
    state.cumDist = [0];
    for (let i = 1; i < state.track.length; i++) {
      state.cumDist[i] = state.cumDist[i - 1] + haversine(state.track[i - 1], state.track[i]);
    }
  }

  const fake = {
    getCurrentPosition(success, error) {
      const fix = currentFix();
      if (fix) success(fix);
      else if (error) error({ code: 2, message: 'sim: no track loaded' });
    },
    watchPosition(success) {
      const id = state.nextWatchId++;
      state.watchers.set(id, success);
      ensureTimer();
      const fix = currentFix();
      if (fix) setTimeout(() => success(fix), 0);
      return id;
    },
    clearWatch(id) {
      state.watchers.delete(id);
    }
  };

  // Best-effort: decode an OTP/Google encoded polyline (precision 1e-5).
  function decodePolyline(str) {
    let index = 0, lat = 0, lng = 0;
    const out = [];
    while (index < str.length) {
      let b, shift = 0, result = 0;
      do { b = str.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
      lat += (result & 1) ? ~(result >> 1) : (result >> 1);
      shift = 0; result = 0;
      do { b = str.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
      lng += (result & 1) ? ~(result >> 1) : (result >> 1);
      out.push([lat / 1e5, lng / 1e5]);
    }
    return out;
  }

  const api = {
    __installed: true,
    loadTrack(coords, opts = {}) {
      if (!Array.isArray(coords) || coords.length === 0) throw new Error('coords must be [[lat,lon],...]');
      state.track = coords.map((c) => [c[0], c[1]]);
      recomputeCum();
      state.dist = 0;
      state.speedMps = opts.speedMps ?? state.speedMps;
      ensureTimer();
      emit();
      console.log(`[sim] track loaded: ${state.track.length} pts, ${Math.round(state.cumDist[state.cumDist.length - 1])} m, speed ${state.speedMps} m/s`);
      return api.status();
    },
    play() { state.playing = true; state.lastTick = Date.now(); ensureTimer(); console.log('[sim] play'); return api.status(); },
    pause() { state.playing = false; console.log('[sim] pause'); return api.status(); },
    jumpTo(lat, lon, hdg) {
      state.track = [[lat, lon]];
      recomputeCum();
      state.dist = 0;
      if (typeof hdg === 'number') state.heading = hdg;
      ensureTimer();
      emit();
      return api.status();
    },
    seek(meters) { state.dist = meters; emit(); return api.status(); },
    offsetMeters(dN, dE) { state.offset = { n: dN || 0, e: dE || 0 }; emit(); console.log(`[sim] offset N${dN} E${dE} m`); return api.status(); },
    clearOffset() { state.offset = { n: 0, e: 0 }; emit(); return api.status(); },
    speed(mps) { state.speedMps = mps; return api.status(); },
    status() {
      const fix = currentFix();
      return {
        lat: fix?.coords.latitude,
        lon: fix?.coords.longitude,
        heading: Math.round(state.heading),
        distAlong: Math.round(state.dist),
        totalDist: Math.round(state.cumDist[state.cumDist.length - 1] || 0),
        watchers: state.watchers.size,
        playing: state.playing
      };
    },
    decodePolyline,
    // Pull leg geometry from the running app's Redux store.
    // During Go Mode the locked-in itinerary lives at otp.goMode.activeItinerary;
    // otherwise fall back to the active search's selected itinerary.
    trackFromActiveItinerary(legIndex) {
      const store = window.store || window.__OTP_STORE__;
      if (!store || !store.getState) throw new Error('Redux store not found on window; set window.store or pass coords manually');
      const s = store.getState();
      const otp = s.otp || {};
      let it = otp.goMode?.activeItinerary || null;
      if (!it) {
        const search = otp.searches?.[otp.activeSearchId];
        if (!search) throw new Error('no active search and not in Go Mode');
        const responses = Array.isArray(search.response) ? search.response : (search.response ? [search.response] : []);
        const itins = responses.filter((r) => r && !r.error && r.plan?.itineraries).flatMap((r) => r.plan.itineraries);
        if (!itins.length) throw new Error('no itineraries in active search response');
        const idx = search.activeItinerary >= 0 ? search.activeItinerary : 0;
        it = itins[idx] || itins[0];
      }
      if (!it || !it.legs) throw new Error('itinerary has no legs');
      const legs = legIndex == null ? it.legs : [it.legs[legIndex]];
      const pts = [];
      legs.forEach((leg) => {
        const enc = leg.legGeometry?.points;
        if (enc) decodePolyline(enc).forEach((p) => pts.push(p));
      });
      if (!pts.length) throw new Error('no legGeometry found on legs');
      return pts;
    },
    stop() {
      if (state.timer) clearInterval(state.timer);
      state.timer = null;
      state.watchers.clear();
      try { Object.defineProperty(navigator, 'geolocation', { value: real, configurable: true }); } catch (e) {}
      console.log('[sim] uninstalled; real geolocation restored');
    }
  };

  try {
    Object.defineProperty(navigator, 'geolocation', { value: fake, configurable: true });
  } catch (e) {
    console.error('[sim] could not override navigator.geolocation', e);
  }
  window.__sim = api;
  console.log('[sim] mock geolocation installed. Use window.__sim — e.g. __sim.loadTrack(__sim.trackFromActiveItinerary()); __sim.play();');
  return api;
})();
