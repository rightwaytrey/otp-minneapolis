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
#     cycle thread's watcher fires on every trip that ends (pipeline-cycle-watch.sh); nothing to press.
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

# 15. a cycle's Chat view has two sub-views like Triage's (rider, 2026-10-09): Terminal (default) = the
#     agent's live window filling everything, no dock at all; Actions = the old conversation (step
#     summaries, branch reports, feedback[], questions) and the whole dock (status, Ship/Hold, Approve
#     plan · build it, Ship to prod, the message box). Only the active one is in the DOM; the choice is
#     remembered in localStorage pipeline-cycle-sub.
one('''    if (src) {
      el.classList.add("tty-full");
      const cur = el.querySelector("iframe.tty");''', '''    if (src && cycSub() === "terminal") {
      el.classList.add("tty-full", "tty-cyc");
      const cur = el.querySelector("iframe.tty");''')
one('''    el.classList.remove("tty-full");
    const items''', '''    el.classList.remove("tty-full", "tty-cyc");
    const items''')
one('''    if (!c && window.PV_TRIAGE) {
''', '''    if (!c && window.PV_TRIAGE) {
      el.classList.remove("tty-cyc");
''')
one('''  const triSub = () =>''', '''  const cycSub = () => { try { return localStorage.getItem("pipeline-cycle-sub") === "actions" ? "actions" : "terminal"; } catch (_) { return "terminal"; } };
  const triSub = () =>''')
one('''${LABEL[k]}</span>`).join("");
  }''', '''${LABEL[k]}</span>`).join("") + (TERMINALS["c" + c.number] ? `<span class="pv-switch tri-sub" role="group" aria-label="Cycle view"><button type="button" data-csub="terminal" aria-pressed="${cycSub() === "terminal"}">Terminal</button><button type="button" data-csub="actions" aria-pressed="${cycSub() === "actions"}">Actions</button></span>` : "");
  }''')
one('''    if (b.dataset.trisub) {''', '''    if (b.dataset.csub) { try { localStorage.setItem("pipeline-cycle-sub", b.dataset.csub); } catch (_) {} return render(false); }
    if (b.dataset.trisub) {''')
one("  #pv-chat .chat.tty-full a.ttyl {", """  #pv-chat .chat.tty-cyc ~ .dock { display: none; }
  #pv-chat .phase { flex-wrap: wrap; row-gap: 6px; }
  #pv-chat .chat.tty-full a.ttyl {""")
assert "Verify now" not in page

# 16. no Remote Control since 2026-10-09 (rider): a cycle's session is reached through its terminal on
#     the Chat view (or tmux), not the Claude app
one('Talk to it: the <b>cycle ${esc(c.number)}</b> session in the Claude app', 'Talk to it: its terminal in the Chat view (<b>cycle ${esc(c.number)}</b> · Terminal)')

open("index.html", "w").write(page)
print("index.html", len(page), "bytes")
