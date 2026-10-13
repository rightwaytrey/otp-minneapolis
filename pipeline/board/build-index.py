#!/usr/bin/env python3
"""Build index.html for the self-hosted board from the Artifact's page source.

  python3 build-index.py <saved artifact html>

Changes to the page: the shim is loaded first; the Chat view's terminal is a
live, writable ttyd iframe of the cycle's tmux window (the Artifact's
text-snapshot pane is gone with the sandbox that forced it); a moved-notice
constant so nothing else differs. The Artifact's publish skeleton (first line)
is kept as the document head. Step 0 strips the "moved" banner the Artifact
source now carries; steps 8-11 give the Chat view's Triage an Inbox (the Classic
view's intake ledger, stacked on a phone) and a Terminal (the router's) sub-view.
"""
import re
import sys

src = sys.argv[1]
page = open(src).read()

# 0. the "This board moved" banner added to the Artifact source on 2026-10-09 points at this page;
#    it is for the old Artifact URL only
page = re.sub(r'\n<div style="position:sticky;top:0;z-index:99;[^\n]*?This board moved on[^\n]*?</div>(?=\n)', "", page, count=1)
assert "This board moved on" not in page

# 1. shim before the first script
page = page.replace("<script>\n// One page, two views.", '<script src="/shim.js"></script>\n<script>\n// One page, two views.', 1)
assert 'src="/shim.js"' in page

# 2. terminal: iframe instead of the snapshot pane
start = page.index('    const snap = c && typeof c.terminalSnapshot === "string"')
end = page.index('    el.classList.remove("tty-full");\n', start) + len('    el.classList.remove("tty-full");\n')
page = page[:start] + '''    // The agent as it is: its tmux window, live, writable, in a ttyd iframe (self-hosted since
    // 2026-10-09; the Artifact sandbox could never do this). Only the src changes when the cycle
    // changes, so a re-render never reloads the terminal.
    const win = c ? "c" + c.number : "board";
    const src = TERMINALS[win];
    if (src) {
      el.classList.add("tty-full");
      const cur = el.querySelector("iframe.tty");
      if (!cur || cur.dataset.w !== win) el.innerHTML = `<iframe class="tty" data-w="${esc(win)}" src="${esc(src)}" title="Terminal ${esc(win)}" allow="clipboard-write"></iframe>`;
      return;
    }
    el.classList.remove("tty-full");
''' + page[end:]

# 3. TERMINALS map, refreshed every 20 s (new cycles get a window; retired ones lose theirs)
page = page.replace('  const MONITOR = "https://rwtpc4.tail9d6464.ts.net:7443/";',
                    '  const MONITOR = "https://rwtpc4.tail9d6464.ts.net:7443/";\n  const TERMINALS = {};\n  const loadTerminals = () => fetch("/api/terminals").then((r) => r.json()).then((t) => { const changed = JSON.stringify(t) !== JSON.stringify(TERMINALS); for (const k in TERMINALS) delete TERMINALS[k]; Object.assign(TERMINALS, t); if (changed) render(true); }).catch(() => {});\n  loadTerminals(); setInterval(loadTerminals, 20000);', 1)
assert "loadTerminals()" in page

# 4. css for the iframe
page = page.replace("  #pv-chat .chat.tty-full a.ttyl {", "  #pv-chat .chat.tty-full iframe.tty { flex: 1; min-height: 0; width: 100%; border: 0; background: #0d1211; }\n  /* with the agent's terminal on screen you type to it directly: no message box (rider, 2026-10-09) */\n  #pv-chat .chat.tty-full ~ .dock .compose, #pv-chat .chat.tty-full ~ .dock .target { display: none; }\n  #pv-chat .chat.tty-full a.ttyl {", 1)

# 5. title
page = page.replace("<title>TransitNav Pipeline</title>", "<title>TransitNav Pipeline</title>\n<link rel=\"icon\" href=\"data:,\">", 1)

# 6. phone keyboard: size the Chat view to the visual viewport so the terminal you are typing
#    into (and the dock) stay on screen when the keyboard is up (rider, 2026-10-09)
page = page.replace("viewport-fit=cover", "viewport-fit=cover,interactive-widget=resizes-content", 1)
page = page.replace("</body></html>", '''<script>
(function () {
  const vv = window.visualViewport; if (!vv) return;
  const fit = () => {
    const chat = document.getElementById("pv-chat"), bar = document.getElementById("pv-bar");
    if (document.body.dataset.pv !== "chat") { chat.style.height = ""; return; }
    const barH = bar && getComputedStyle(bar).display !== "none" ? bar.offsetHeight : 0;
    chat.style.height = Math.max(200, vv.height - barH) + "px";
    window.scrollTo(0, 0);
  };
  vv.addEventListener("resize", fit); vv.addEventListener("scroll", fit); window.addEventListener("resize", fit);
  document.addEventListener("click", (e) => { if (e.target.closest("[data-pv]")) setTimeout(fit, 0); });
  fit();
})();
</script>
</body></html>''', 1)

# 7. no success toasts in the Chat view: the terminal shows the press landing (rider, 2026-10-09: "redundant");
#    failure toasts stay
page = page.replace('  const sentMsg = (r, okMsg) => r.ok ? okMsg : REVIEW_COPY',
                    '  // No success toasts: the agent\'s terminal shows the press landing (rider, 2026-10-09: "redundant"). Failures still toast.\n  const sentMsg = (r, okMsg) => r.ok ? "" : REVIEW_COPY', 1)
page = page.replace('  function toast(msg, ms) { const t = $("chat-toast"); t.textContent = msg;',
                    '  function toast(msg, ms) { if (!msg) return; const t = $("chat-toast"); t.textContent = msg;', 1)
assert 'r.ok ? "" : REVIEW_COPY' in page and 'if (!msg) return; const t = $("chat-toast")' in page

# 8. Chat view's Triage has two sub-views behind an Inbox · Terminal switch on its phase line (rider,
#    2026-10-09 ~15:05: "the triage tab's got too much on it, need to separate the views"; it was the
#    ledger stacked over the terminal since ~14:45). Inbox = the Classic view's intake ledger (the
#    classic IIFE owns it and has board/cycles even while hidden); Terminal = the router's ttyd iframe,
#    message box hidden as for a cycle. Only the active one is rendered; default Inbox, remembered in
#    localStorage pipeline-triage-sub. The dock's "Run triage" stays in both.
page = page.replace("  function renderTriage() {", """  window.PV_TRIAGE = { html: () => ledger(), setFilter: (k) => { ui.tfilter = k; saveUi(); }, openItem: (key) => { ui.sec = "flow"; ui.item = key; saveUi(); } };
  function renderTriage() {""", 1)
assert "window.PV_TRIAGE" in page
page = page.replace("""    const win = c ? "c" + c.number : "board";
    const src = TERMINALS[win];""", """    if (!c && window.PV_TRIAGE) {
      if (triSub() === "terminal" && TERMINALS.board) {
        el.classList.add("tty-full");
        const fr = el.querySelector("iframe.tty");
        if (!fr || fr.dataset.w !== "board") el.innerHTML = `<iframe class="tty" data-w="board" src="${esc(TERMINALS.board)}" title="Terminal board" allow="clipboard-write"></iframe>`;
        return;
      }
      el.classList.remove("tty-full");
      let led = el.querySelector(".tri-ledger");
      if (!led) { el.innerHTML = '<div class="tri tri-ledger"></div>'; led = el.querySelector(".tri-ledger"); }
      const top = el.scrollTop; led.innerHTML = window.PV_TRIAGE.html(); el.scrollTop = keepScroll ? top : 0;
      return;
    }
    const win = c ? "c" + c.number : "board";
    const src = TERMINALS[win];""", 1)
assert 'class="tri tri-ledger"' in page
page = page.replace("""  function renderHeader(v) {""", """  const triSub = () => { try { return localStorage.getItem("pipeline-triage-sub") === "terminal" ? "terminal" : "inbox"; } catch (_) { return "inbox"; } };
  function renderHeader(v) {""", 1)
page = page.replace("""    if (!c) { $("phase").innerHTML = `<span class="p" data-cur="true" data-s="${ts.s}">Triage · ${esc(ts.text)}</span>`; return; }""",
                    """    if (!c) { const s = triSub(); $("phase").innerHTML = `<span class="p" data-cur="true" data-s="${ts.s}">Triage · ${esc(ts.text)}</span><span class="pv-switch tri-sub" role="group" aria-label="Triage view"><button type="button" data-trisub="inbox" aria-pressed="${s === "inbox"}">Inbox</button><button type="button" data-trisub="terminal" aria-pressed="${s === "terminal"}">Terminal</button></span>`; return; }""", 1)
assert 'data-trisub="inbox"' in page
page = page.replace("""    if (b.dataset.view) { drafts[current()]""", """    if (b.dataset.trisub) { try { localStorage.setItem("pipeline-triage-sub", b.dataset.trisub); } catch (_) {} return render(false); }
    if (b.dataset.tfilter && window.PV_TRIAGE) { window.PV_TRIAGE.setFilter(b.dataset.tfilter); return render(true); }
    if (b.dataset.item && window.PV_TRIAGE) { window.PV_TRIAGE.openItem(b.dataset.item); const sw = document.querySelector('[data-pv="classic"]'); if (sw) sw.click(); return; }
    if (b.dataset.view) { drafts[current()]""", 1)
assert "PV_TRIAGE.setFilter" in page and "dataset.trisub" in page

# 9. the ledger's classic CSS also applies inside #pv-chat .tri: in the classic <style> block, each
#    selector "#pv-classic <ledger part>" becomes ":is(#pv-classic, #pv-chat .tri) <ledger part>"
LEDGER_SEL = re.compile(r"#pv-classic (?=(\.ledger|\.tchips|\.chip|\.tbl|table|th|td|\.kind|\.img|\.track|\.stage-lbl|\.link|\.sub|\.muted|\.mono|\.empty|\.filters)\b)")
a = page.index("<style>\n")
b = page.index("</style>", a)
out = []
for line in page[a:b].split("\n"):
    if "{" in line and line.lstrip().startswith("#pv-classic"):
        sel, rest = line.split("{", 1)
        line = ",".join(LEDGER_SEL.sub(":is(#pv-classic, #pv-chat .tri) ", x) for x in sel.split(",")) + "{" + rest
    out.append(line)
css = "\n".join(out)
assert css.count(":is(#pv-classic, #pv-chat .tri)") > 40, css.count(":is(#pv-classic, #pv-chat .tri)")
page = page[:a] + css + page[b:]

# 10. the variable names the classic rules use, aliased to the chat view's palette; the sub-view switch
page = page.replace("  #pv-chat .chat.tty-full a.ttyl {", """  #pv-chat .tri { --requested: var(--you); --requested-soft: var(--you-soft); --running: var(--run); --running-soft: var(--run-soft); --surface-3: var(--surface-2); }
  #pv-chat .tri .tchips { margin: 0 0 8px; }
  #pv-chat .phase .tri-sub { margin-left: auto; }
  #pv-chat .chat.tty-full a.ttyl {""", 1)
assert "#pv-chat .phase .tri-sub" in page

# 11. on a phone the ledger stacks each row instead of a 4-column table that side-scrolls (rider,
#     2026-10-09): line 1 time + kind + input, line 2 what it became, line 3 where now. Pure CSS on the
#     shared ledger markup, so Classic stacks the same way at that width; desktop keeps the table.
page = page.replace("</style>\n", """  @media (max-width: 640px) {
    :is(#pv-classic, #pv-chat .tri) .tbl.ledger { overflow-x: visible; }
    :is(#pv-classic, #pv-chat .tri) .ledger table, :is(#pv-classic, #pv-chat .tri) .ledger tbody { display: block; width: 100%; }
    :is(#pv-classic, #pv-chat .tri) .ledger thead { display: none; }
    :is(#pv-classic, #pv-chat .tri) .ledger tr { display: grid; grid-template-columns: auto minmax(0, 1fr); column-gap: 10px; row-gap: 6px; padding: 9px 0; border-bottom: 1px solid var(--line); }
    :is(#pv-classic, #pv-chat .tri) .ledger tr.day { display: block; padding: 12px 0 4px; border-bottom: 0; }
    :is(#pv-classic, #pv-chat .tri) .ledger td { display: block; padding: 0; border: 0; min-width: 0; max-width: none; }
    :is(#pv-classic, #pv-chat .tri) .ledger td.became, :is(#pv-classic, #pv-chat .tri) .ledger td.where { grid-column: 1 / -1; white-space: normal; min-width: 0; }
    :is(#pv-classic, #pv-chat .tri) .ledger td.in { min-width: 0; max-width: none; }
    :is(#pv-classic, #pv-chat .tri) .ledger td.became .sub, :is(#pv-classic, #pv-chat .tri) .ledger td.where .sub { max-width: none; }
    :is(#pv-classic, #pv-chat .tri) .ledger tr[data-new="true"] { background: var(--requested-soft); box-shadow: inset 3px 0 0 var(--requested); padding-left: 8px; }
    :is(#pv-classic, #pv-chat .tri) .ledger tr[data-new="true"] td { background: none; box-shadow: none; }
  }
</style>
""", 1)
assert "grid-template-columns: auto minmax(0, 1fr)" in page

# 12. desktop ledger fits its column (rider via the board session, 2026-10-09): the table was 835 px in a
#     748 px chat column because "Became" never wrapped and "Where now" had a 260 px floor, so the
#     where-now labels were cut at the edge. Became wraps; where-now keeps row number + track on one line
#     and the label drops underneath when it needs the room. Same rules serve Classic.
page = page.replace("</style>\n", """  @media (min-width: 641px) {
    :is(#pv-classic, #pv-chat .tri) .ledger td.became { white-space: normal; min-width: 150px; }
    :is(#pv-classic, #pv-chat .tri) .ledger td.where { min-width: 200px; }
  }
  :is(#pv-classic, #pv-chat .tri) .ledger .wn > span { column-gap: 8px; row-gap: 2px; }
  :is(#pv-classic, #pv-chat .tri) .ledger .wn .stage-lbl { white-space: normal; overflow-wrap: anywhere; }
</style>
""", 1)
assert "td.became { white-space: normal; min-width: 150px; }" in page

# 13. no "Verify now" anywhere (rider, 2026-10-09 ~19:20: "automate the verification — it should be
#     able to tell when verify can run from a user run; then get rid of the [Verify now] button"). The
#     cycle thread's watcher fires on every trip that ends (pipeline-wait.sh); nothing to press.
def one(old, new):
    global page
    assert page.count(old) == 1, (page.count(old), old[:80])
    page = page.replace(old, new)
one('''      const vn = onDev(c) && rowsOnDev && !busy(stat(c, "verify")) ? `<div class="go-row"><button class="quiet" data-start="${esc(c.id)}" data-step="verify" ${canEdit === false ? "disabled" : ""}>Verify now</button><span class="why">Only if a ride's report never arrived.</span></div>` : "";''',
    '''      const vn = "";''')
one('''    // Build and ship-dev run on their own (2026-10-08): no press. Verify runs on each dev-app ride;
    // "Verify now" stays as a small secondary action for a ride whose report never landed.
    const vnow = { label: "Verify now", act: () => requestStep(c.id, "verify") };
    if (k === "build" || k === "ship-dev") return null;
    if (k === "verify") return { alt: vnow };''', '''    // Build, ship-dev and verify all run on their own (verify on each trip that ends, since 2026-10-09):
    // no press for any of them.
    if (k === "build" || k === "ship-dev" || k === "verify") return null;''')
one('''    // Verify again only makes sense while some row is still waiting on a ride.
    if (k === "ship-prod" && waitingOnRide(c)) p.alt = vnow;
''', "")

# 14. what happened to the last ride, in the rider's words, from steps.verify.lastRide (written by the
#     cycle thread for every trip after ship-dev): on the Verify card and under the cycle's status line.
page = page.replace('<script src="/shim.js"></script>', '''<script src="/shim.js"></script>
<script>
window.pvRideLine = function (c) {
  const st = (c && c.steps) || {}, sd = st["ship-dev"] || {}, lr = (st.verify || {}).lastRide;
  if (!c || c.retiredAt || sd.status !== "done") return "";
  const when = (ms) => new Date(ms).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).replace(",", "");
  const clock = (ms) => new Date(ms).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  if (!lr || !lr.endedAt || (sd.finishedAt && lr.endedAt < sd.finishedAt)) return "No ride on the dev app since this shipped" + (sd.finishedAt ? " (" + when(sd.finishedAt) + ")" : "");
  const head = "Last ride " + when(lr.endedAt);
  if (lr.applies === "prod-app" || lr.app === "prod") return head + " · App Store app · doesn't count for dev rows";
  const b = lr.bundle ? "dev " + lr.bundle : "dev app";
  if (lr.applies === "older-bundle") return head + " · " + b + " · ran older code than this cycle";
  if (lr.applies === "no-rows") return head + " · " + b + " · nothing left to score";
  if (!lr.scoredAt) return head + " · " + b + " · scoring now";
  const n = lr.counts || {}, parts = [];
  if (n.pass != null) parts.push(n.pass + " pass");
  if (n.fail) parts.push(n.fail + " fail");
  if (n.notExercised != null) parts.push(n.notExercised + " not exercised");
  return head + " · " + b + " · scored " + clock(lr.scoredAt) + (parts.length ? " — " + parts.join(" · ") : "");
};
</script>''', 1)
assert "window.pvRideLine" in page
one('''      const before = `<p class="now">Bundle on dev <span class="mono">${esc(facts.devBundle || "—")}</span>.''',
    '''      const rl = window.pvRideLine(c);
      const before = (rl ? `<p class="now ride-line">${esc(rl)}</p>` : "") + `<p class="now">Bundle on dev <span class="mono">${esc(facts.devBundle || "—")}</span>.''')
one('''    $("next").innerHTML = `<span class="st"><span class="dot" data-s="${st.s}"></span><span>${esc(st.text)}</span></span>`''',
    '''    const rl = c ? window.pvRideLine(c) : "";
    $("next").innerHTML = `<span class="st"><span class="dot" data-s="${st.s}"></span><span>${esc(st.text)}${rl ? `<span class="ride">${esc(rl)}</span>` : ""}</span></span>`''')
one("  #pv-chat .chat.tty-full a.ttyl {", """  #pv-chat .next .st .ride { display: block; font-size: 12px; color: var(--ink-3); margin-top: 2px; }
  #pv-classic .ride-line { margin-bottom: 8px; font-weight: 500; }
  #pv-chat .chat.tty-full a.ttyl {""")

# 15. Chat has no dock and no buttons but one Ship tab (rider, 2026-10-09 ~22:30, after a Terminal ·
#     Actions split that brought the dock back: "why did you bring actions back, that's terrible. All we
#     need is one ship button; let's put the ship buttons centrally for all cycles"). A cycle's Chat view
#     is its terminal edge to edge; approval, holds and answers are typed to the agent there (the thread
#     writes the doc fields the page would). The "Ship" chip after the cycle chips lists every live
#     cycle with one Ship to prod button each, via the same requestStep(cid, "ship-prod") as before.
#     Classic keeps every control.
one('''  #pv-chat .chat.tty-full a.ttyl {''', '''  #pv-chat .dock { display: none !important; }
  #pv-chat .ship-list { display: grid; gap: 12px; padding: 4px 0 16px; }
  #pv-chat .shipc { border: 1px solid var(--line); border-radius: 12px; background: var(--surface); padding: 14px 16px; display: grid; gap: 4px; }
  #pv-chat .shipc p { margin: 0; }
  #pv-chat .shipc h3 { margin: 0 0 2px; font-size: 15px; font-weight: 600; color: var(--ink); }
  #pv-chat .shipc .st { display: flex; align-items: center; gap: 8px; font-size: 14px; color: var(--ink-2); }
  #pv-chat .shipc .ride, #pv-chat .shipc .bun { font-size: 12.5px; color: var(--ink-3); }
  #pv-chat .shipc .bun { font-family: var(--mono); }
  #pv-chat .shipc .act { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; margin-top: 8px; }
  #pv-chat .shipc .go:disabled { background: var(--surface-2); color: var(--ink-3); cursor: default; }
  #pv-chat .shipc .why { font-size: 13px; color: var(--ink-3); }
  #pv-chat .chat.tty-full a.ttyl {''')
one('''  const triSub = () =>''', '''  // Ship to prod for one cycle: allowed once every row has been through a scored dev ride (none still
  // waiting for its first one) and none failed; rows a ride did not exercise go to production as in-prod.
  function shipState(c) {
    const st = (k) => ((c.steps || {})[k] || {}).status;
    const rs = (c.rows || []).filter((r) => r.keep !== false && r.branch);
    const fail = rs.filter((r) => r.status === "fail").length;
    if (busy(st("ship-prod"))) return { ok: false, why: "shipping to production now" };
    if (st("ship-prod") === "done" || (c.bundle && c.bundle.prod)) return { ok: false, why: "already in production" };
    if (st("ship-dev") !== "done") return { ok: false, why: "not on the dev app yet" };
    if (fail) return { ok: false, why: `${fail} failed on the ride — see the backlog` };
    if (st("verify") !== "done" || rs.some((r) => r.status === "on-dev")) return { ok: false, why: "waiting for a dev ride" };
    return { ok: true, why: "" };
  }
  function shipCards() {
    const live = liveCycles(); const fx = (board && board.facts) || {};
    if (!live.length) return `<p class="empty">No live cycles.</p>`;
    return `<div class="ship-list">${live.map((c) => {
      const w = where(c), s = shipState(c), rl = window.pvRideLine(c);
      return `<section class="shipc"><h3>Cycle ${esc(c.number)}${(() => { const t = (c.title || "").replace(new RegExp("^ *cycle +" + c.number + " *[—:-]? *", "i"), "").trim(); return t ? " — " + esc(t) : ""; })()}</h3>`
        + `<p class="st"><span class="dot" data-s="${w.s}"></span><span>${esc(w.text)}</span></p>`
        + (rl ? `<p class="ride">${esc(rl)}</p>` : "")
        + `<p class="bun">dev ${esc((c.bundle && c.bundle.dev) || "—")} · production ${esc(fx.prodBundle || "—")}</p>`
        + `<div class="act"><button type="button" class="go" data-shipprod="${esc(c.id)}" ${s.ok && canEdit !== false ? "" : "disabled"}>Ship to prod</button>${s.ok ? "" : `<span class="why">${esc(s.why)}</span>`}</div></section>`;
    }).join("")}</div>`;
  }
  const triSub = () =>''')
one('''  function current() {
''', '''  function current() {
    if (ui.view === "ship") return "ship";
''')
one('''    const c = cyc(v);
    if (!c) { const s = triSub();''', '''    $("chips").insertAdjacentHTML("beforeend", `<button class="chip" data-view="ship" aria-current="${v === "ship"}"><span class="dot" data-s="${liveCycles().some((x) => shipState(x).ok) ? "ready" : ""}"></span>Ship</button>`);
    if (v === "ship") { $("phase").innerHTML = `<span class="p" data-cur="true" data-s="ready">Ship to production · one press per cycle</span>`; return; }
    const c = cyc(v);
    if (!c) { const s = triSub();''')
one('''    if (!c && window.PV_TRIAGE) {
''', '''    if (v === "ship") { el.classList.remove("tty-full"); el.innerHTML = shipCards(); return; }
    if (!c && window.PV_TRIAGE) {
''')
one('''    if (b.dataset.trisub) {''', '''    if (b.dataset.shipprod) { b.disabled = true; return requestStep(b.dataset.shipprod, "ship-prod").then(() => render(true)); }
    if (b.dataset.trisub) {''')

# 16. no Remote Control since 2026-10-09 (rider): a cycle's session is reached through its terminal on
#     the Chat view (or tmux), not the Claude app
one('Talk to it: the <b>cycle ${esc(c.number)}</b> session in the Claude app', 'Talk to it: its terminal in the Chat view (<b>cycle ${esc(c.number)}</b> · Terminal)')

# 17. one "next phase" button on the Chat view's phase line (rider, 2026-10-12: "i still liked the run
#     triage buttons and plan etc. lets have one button to promote the agents to next phase. it can just
#     inject text"). It types one line into the agent's own terminal (POST /api/inject -> tmux send-keys
#     on transitnav:cN or :board) and the thread acts on it as typed instructions, writing the fields
#     the old press wrote (skill: "The rider talking to this session directly"). Nothing goes through
#     the inbox, so the rider sees the instruction land in the terminal. Hidden while the agent is busy.
one('''    <div class="phase" id="phase" aria-label="Phase"></div>
  </header>''', '''    <div class="row2"><div class="phase" id="phase" aria-label="Phase"></div><button type="button" class="nextgo" id="nextgo" hidden></button></div>
  </header>''')
one("  #pv-chat .chat.tty-full a.ttyl {", """  #pv-chat .row2 { display: flex; align-items: center; gap: 8px; min-width: 0; }
  #pv-chat .row2 .phase { flex: 1; min-width: 0; }
  #pv-chat .nextgo { flex: none; min-height: 36px; padding: 0 14px; border-radius: 9px; background: var(--accent); color: var(--accent-ink); font-weight: 600; font-size: 14px; white-space: nowrap; }
  #pv-chat .nextgo:disabled { background: var(--surface-2); color: var(--ink-3); cursor: default; }
  #pv-chat .chat.tty-full a.ttyl {""")
one("""  const triSub = () =>""", r"""  // The one "next phase" press for the view: {label, win, text} to type into that terminal, or
  // {label, disabled, why} when the next phase can't start from here, or null (busy / nothing next).
  function promote(v) {
    if (v === "ship") return null;
    const c = cyc(v);
    if (!c) {
      const it = (board && board.intake) || {};
      if (busy(it.status)) return null;
      const n = nextNumber();
      return { win: "board", label: "Run triage", text: `START triage — then plan cycle ${n} (typed from the board's next-phase button): run the transitnav-pipeline skill for stage "triage", update pipeline/board.intake, then open cycles/c${n} with the rows triage opened or touched and run stage "plan" on it.` };
    }
    if (c.retiredAt || ORDER.some((k) => busy(stat(c, k)))) return null;
    const n = c.number, win = "c" + n, via = "(typed from the board's next-phase button — write the fields the board's press would have written, then act)";
    if (stat(c, "plan") === "draft") return { win, label: "Approve plan · build it", text: `approve the plan — build it, cycle ${n} ${via}` };
    for (const k of ORDER) if (stat(c, k) === "blocked") return { win, label: `Resume ${LABEL[k].toLowerCase()}`, text: `START ${k} — cycle ${n}, resume ${via}: run the transitnav-pipeline skill for stage "${k}" on cycles/${c.id}.` };
    const k = nextStep(c); if (!k) return null;
    if (unanswered(c).length) return { label: "Answer questions first", disabled: true };
    if (k === "verify") return { label: "Verify · needs a dev ride", disabled: true };
    if (k === "ship-prod") { const s = shipState(c); return s.ok ? { win, label: "Ship to prod", text: `ship to prod — cycle ${n} ${via}` } : { label: "Ship to prod", disabled: true, why: s.why }; }
    const lbl = { plan: "Plan it", build: "Build it", "ship-dev": "Ship to dev" }[k] || LABEL[k];
    return { win, label: lbl, text: `START ${k} — cycle ${n} ${via}: run the transitnav-pipeline skill for stage "${k}" on cycles/${c.id}.` };
  }
  function renderNextGo(v) {
    const b = $("nextgo"), p = promote(v);
    ui.promote = p;
    b.hidden = !p;
    if (!p) return;
    b.textContent = p.label;
    b.disabled = !!p.disabled || canEdit === false || !!(p.win && !TERMINALS[p.win]);
    b.title = p.why || (p.win && !TERMINALS[p.win] ? "No terminal for this agent" : p.text || "");
  }
  async function pressNextGo() {
    const p = ui.promote, b = $("nextgo");
    if (!p || p.disabled || !p.win) return;
    b.disabled = true; b.textContent = "Sent";
    try {
      const r = await fetch("/api/inject", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ win: p.win, text: p.text }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || "http " + r.status);
      // show it landing: on Triage, flip to the router's terminal
      if (p.win === "board") { try { localStorage.setItem("pipeline-triage-sub", "terminal"); } catch (_) {} }
    } catch (err) { toast("Couldn't type into the terminal (" + (err && err.message || "error") + ")."); }
    setTimeout(() => render(true), 4000);
  }
  document.getElementById("nextgo").addEventListener("click", pressNextGo);
  const triSub = () =>""")
one('''    if (v === "ship") { $("phase").innerHTML = `<span class="p" data-cur="true" data-s="ready">Ship to production · one press per cycle</span>`; return; }''',
    '''    renderNextGo(v);
    if (v === "ship") { $("phase").innerHTML = `<span class="p" data-cur="true" data-s="ready">Ship to production · one press per cycle</span>`; return; }''')
# the phase line now shares its row with the button: keep the current phase in view on a phone
one('''data-s="${i === ci ? w.s : ""}">${LABEL[k]}</span>`).join("");''',
    '''data-s="${i === ci ? w.s : ""}">${LABEL[k]}</span>`).join("");
    { const ph = $("phase"), cur = ph.querySelector('[data-cur="true"]'); if (cur) ph.scrollLeft = Math.max(0, cur.offsetLeft + cur.offsetWidth - ph.clientWidth + 8); }''')

# 18. triage never chains into a plan (rider, 2026-10-13: "the last cycle started the plan without
#     letting me approve what the triage had collected"). Triage stops and leaves its rows in
#     board.intake.planCandidates; the next-phase button then reads "Plan these N · cycle n" and
#     types "approve triage — plan cycle n" into the router. Every "then plan" path is cut: the
#     next-phase button, the Chat view's triage press and the Classic checkbox (which defaulted on).
one('''      return { win: "board", label: "Run triage", text: `START triage — then plan cycle ${n} (typed from the board's next-phase button): run the transitnav-pipeline skill for stage "triage", update pipeline/board.intake, then open cycles/c${n} with the rows triage opened or touched and run stage "plan" on it.` };''',
    '''      const pc = it.planCandidates, rows = (pc && !pc.approvedAt && Array.isArray(pc.rows)) ? pc.rows : [];
      if (rows.length) return { win: "board", label: `Plan these ${rows.length} · cycle ${n}`, text: `approve triage — plan cycle ${n} (typed from the board's next-phase button): the rider approved what the last triage collected; open cycles/c${n} with intake.planCandidates and run stage "plan" on it.` };
      return { win: "board", label: "Run triage", text: `START triage (typed from the board's next-phase button): run the transitnav-pipeline skill for stage "triage" and update pipeline/board.intake. Do not open a cycle or plan; leave the rows for the rider to approve.` };''')
one('''      b.intake = Object.assign({}, b.intake || {}, { status: "requested", requestedAt: now, startedAt: null, finishedAt: null, note, requestedBy: uid || null, wake: null, thenPlan: true });
      appendLog(b, { t: now, stage: "triage", event: "requested", text: (note ? note + " · " : "") + "then plan cycle " + n }); b.updatedAt = now; await ref.set(b);''',
    '''      b.intake = Object.assign({}, b.intake || {}, { status: "requested", requestedAt: now, startedAt: null, finishedAt: null, note, requestedBy: uid || null, wake: null, thenPlan: false });
      appendLog(b, { t: now, stage: "triage", event: "requested", text: (note ? note + " · " : "") + "triage only" }); b.updatedAt = now; await ref.set(b);''')
one('''    const r = await wake(`START triage — then plan cycle ${n}` + (note ? `\\n\\n${note}` : "") + `\\n\\nRun the transitnav-pipeline skill for stage "triage", update pipeline/board.intake, then open cycles/c${n} with the rows triage opened or touched and run stage "plan" on it, and reply here when it is done.`);''',
    '''    const r = await wake(`START triage` + (note ? `\\n\\n${note}` : "") + `\\n\\nRun the transitnav-pipeline skill for stage "triage" and update pipeline/board.intake. Do not open a cycle or plan; leave the rows for the rider to approve.`);''')
one('''    toast(sentMsg(r, "Sent. Triage is queued; a plan follows."));''',
    '''    toast(sentMsg(r, "Sent. Triage is queued; you approve its rows before any plan."));''')
one('''      extra: `<label class="opt-row"><input type="checkbox" id="intake-then-plan" ${ui.thenPlan !== false ? "checked" : ""}> Then open cycle ${nextNumber()} with what it finds and plan it <span class="muted">— Build still waits for your press</span></label>`,''',
    '''      extra: `<p class="muted">Triage stops when it is done; you approve its rows (Plan these · cycle ${nextNumber()}) before a plan starts.</p>`,''')
assert page.count("ui.thenPlan !== false") == 5, page.count("ui.thenPlan !== false")
page = page.replace("ui.thenPlan !== false", "false")

open("index.html", "w").write(page)
print("index.html", len(page), "bytes")
