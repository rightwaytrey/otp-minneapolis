#!/usr/bin/env python3
"""The TransitNav Pipeline board, self-hosted (2026-10-09; replaces the Claude
Artifact RtFoFD16ijxfYUxBXABgQ2). Stdlib only. Binds 127.0.0.1:8098, exposed
tailnet-only by `tailscale serve --bg --https=9600 http://127.0.0.1:8098`.
Runs as the systemd --user unit `pipeline-board`.

Store: ~/.claude/pipeline/db/<collection>/<doc>.json = {data, version, updatedAt}

  GET  /                   index.html (build-index.py output) and /shim.js
  GET  /api/doc?path=c/d   {exists, data, version}
  GET  /api/list?path=c    {docs: [{id, data, version}]}
  POST /api/write          {path, op: set|update, data, if_version}
                           409 {error: version_mismatch, version} if if_version is stale;
                           update deep-merges objects, replaces arrays, {"__delete__": true} drops a key
  GET  /api/events         SSE, one {path, data, version} per write
  POST /api/press          {text, key} -> ~/bin/pipeline-route.sh <N|board> --thread - --key <key>
                           (text on stdin; N from "cycle N" in the first line, else the
                           whole text; START triage always goes to "board")
  POST /api/inject         {win: cN|board, text} -> types the text (one line) into tmux window
                           transitnav:<win> and presses Enter: the Chat view's one "next phase"
                           button (rider, 2026-10-12), read by the thread as typed instructions
  GET  /api/terminals      {cN|board: "https://<tailnet host>:<port>"} for each live window
                           of tmux session `transitnav`; ensures a WRITABLE ttyd on
                           127.0.0.1:20000+N (board 19999) and a tailscale serve mapping

Terminals attach through a grouped session (web-cN, destroyed when unattached),
NOT `tmux attach -t transitnav:cN`: that makes cN the shared session's current
window, which flips the rider's own terminal and every other attached client
(measured 2026-10-09 on a throwaway tmux server). ttyd 1.6.3 is writable by
default (it has no -W); `-i lo` is required or ttyd takes the tailscale
interface's port away from tailscale serve's TLS listener. `-I ttyd-index.html` is ttyd's own
client page (ttyd-index.orig.html, as served) plus a script that turns touch drags into wheel
events, so a phone can scroll the pane (xterm.js never reports touch drags to tmux).
"""
import http.server
import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
DB = os.path.join(HOME, ".claude", "pipeline", "db")
ROUTE = os.path.join(HOME, "bin", "pipeline-route.sh")
PORT = 8098
PORT_BASE = 20000
BOARD_PORT = 19999
SEG = re.compile(r"^[A-Za-z0-9_\-.~:@+]{1,200}$")

lock = threading.Lock()
subscribers = set()
sub_lock = threading.Lock()
term_lock = threading.Lock()
served_ports = set()
_host = None


# ---- store ---------------------------------------------------------------
def seg_ok(s):
    return bool(SEG.match(s)) and s not in (".", "..")


def doc_file(path):
    parts = (path or "").split("/")
    if len(parts) != 2 or not all(seg_ok(p) for p in parts):
        raise ValueError("path must be <collection>/<doc>")
    return os.path.join(DB, parts[0], parts[1] + ".json")


def read(path):
    try:
        with open(doc_file(path)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def strip_deletes(v):
    if isinstance(v, dict):
        return {k: strip_deletes(x) for k, x in v.items() if not (isinstance(x, dict) and x.get("__delete__") is True)}
    return v


def merge(base, patch):
    out = dict(base) if isinstance(base, dict) else {}
    for k, v in patch.items():
        if isinstance(v, dict) and v.get("__delete__") is True:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = strip_deletes(v)
    return out


def write(path, op, data, if_version):
    if op not in ("set", "update") or not isinstance(data, dict):
        raise ValueError("op must be set|update and data an object")
    f = doc_file(path)
    with lock:
        cur = read(path)
        ver = cur["version"] if cur else 0
        if if_version is not None and int(if_version) != ver:
            return 409, {"error": "version_mismatch", "version": ver}
        new = merge(cur["data"] if cur else {}, data) if op == "update" else strip_deletes(data)
        rec = {"data": new, "version": ver + 1, "updatedAt": int(time.time() * 1000)}
        os.makedirs(os.path.dirname(f), exist_ok=True)
        tmp = f + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        os.replace(tmp, f)
        publish({"path": path, "data": new, "version": rec["version"]})
    return 200, {"ok": True, "version": rec["version"]}


def publish(ev):
    msg = json.dumps(ev, ensure_ascii=False)
    with sub_lock:
        for q in list(subscribers):
            q.put(msg)


# ---- presses -------------------------------------------------------------
def press_target(text):
    first = text.strip().splitlines()[0] if text.strip() else ""
    if re.match(r"\s*START triage\b", first, re.I):
        return "board"
    for t in (first, text):
        m = re.search(r"\bcycle\s+(\d+)\b", t, re.I)
        if m:
            return m.group(1)
    return "board"


def press(text, key):
    if not isinstance(text, str) or not text.strip():
        return 400, {"error": "text required"}
    key = re.sub(r"[^A-Za-z0-9_.:@+-]", "-", key or "") or "web:%d" % int(time.time() * 1000)
    n = press_target(text)
    p = subprocess.run([ROUTE, n, "--thread", "-", "--key", key], input=text, capture_output=True, text=True, timeout=30)
    out = (p.stdout + p.stderr).strip()
    return (200 if p.returncode == 0 else 500), {"ok": p.returncode == 0, "routed": n, "key": key, "out": out}


def inject(win, text):
    if not isinstance(win, str) or not re.fullmatch(r"c\d+|board", win):
        return 400, {"error": "win must be cN or board"}
    if not isinstance(text, str) or not text.strip():
        return 400, {"error": "text required"}
    text = " ".join(text.split())[:2000]  # one line: a newline would submit early
    t = f"transitnav:{win}"
    names = subprocess.run(["tmux", "list-windows", "-t", "transitnav", "-F", "#{window_name}"], capture_output=True, text=True).stdout.split()
    if win not in names:
        return 404, {"error": f"no tmux window {t}"}
    mode = subprocess.run(["tmux", "display-message", "-p", "-t", t, "#{pane_in_mode}"], capture_output=True, text=True).stdout.strip()
    if mode == "1":  # scrolled back in copy-mode: keys would go to copy-mode, not to claude
        subprocess.run(["tmux", "send-keys", "-t", t, "-X", "cancel"])
    subprocess.run(["tmux", "send-keys", "-t", t, "-l", "--", text], check=True)
    time.sleep(0.4)  # let claude's input settle the typed text before Enter
    subprocess.run(["tmux", "send-keys", "-t", t, "Enter"], check=True)
    return 200, {"ok": True, "win": win}


# ---- terminals -----------------------------------------------------------
def tailnet_host():
    global _host
    if not _host:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10)
        _host = json.loads(out.stdout)["Self"]["DNSName"].rstrip(".")
    return _host


def listening(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_terminal(win, port):
    old = subprocess.run(["pgrep", "-af", f"ttyd .*-p {port} "], capture_output=True, text=True).stdout
    for line in old.splitlines():  # a read-only mirror from the old pipeline-terminals page
        if "--readonly" in line or " -R " in line:
            subprocess.run(["kill", line.split()[0]])
            time.sleep(0.3)
    if not listening(port):
        subprocess.Popen(
            ["ttyd", "-i", "lo", "-p", str(port), "-I", os.path.join(HERE, "ttyd-index.html"),
             "-t", "fontSize=12",   # 52 columns fit a 390 px phone (rider, 2026-10-09: "font size reduced")
             "tmux", "new-session", "-A", "-s", f"web-{win}", "-t", "transitnav", ";",
             "set-option", "destroy-unattached", "on", ";", "select-window", "-t", win],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    if port not in served_ports:
        r = subprocess.run(["tailscale", "serve", "--bg", f"--https={port}", f"http://127.0.0.1:{port}"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            served_ports.add(port)


def terminals():
    out = subprocess.run(["tmux", "list-windows", "-t", "transitnav", "-F", "#{window_name}"], capture_output=True, text=True)
    names = [n.strip() for n in out.stdout.splitlines()]
    wins = [(n, PORT_BASE + int(n[1:])) for n in names if re.fullmatch(r"c\d+", n)]
    if "board" in names:
        wins.append(("board", BOARD_PORT))
    host = tailnet_host()
    res = {}
    with term_lock:
        for win, port in wins:
            ensure_terminal(win, port)
            res[win] = f"https://{host}:{port}"
    return res


# ---- http ----------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        if "/api/write" in self.path or "/api/press" in self.path or "/api/inject" in self.path:
            super().log_message(fmt, *a)

    def send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/shim.js":
                return self.send(200, open(os.path.join(HERE, "shim.js"), "rb").read(), "text/javascript; charset=utf-8")
            if u.path == "/api/doc":
                r = read(q.get("path", ""))
                return self.send(200, {"exists": bool(r), "data": r["data"] if r else None, "version": r["version"] if r else 0})
            if u.path == "/api/list":
                c = q.get("path", "")
                if not seg_ok(c):
                    return self.send(400, {"error": "bad collection"})
                d = os.path.join(DB, c)
                docs = []
                for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                    if fn.endswith(".json"):
                        r = read(c + "/" + fn[:-5])
                        if r:
                            docs.append({"id": fn[:-5], "data": r["data"], "version": r["version"]})
                return self.send(200, {"docs": docs})
            if u.path == "/api/terminals":
                return self.send(200, terminals())
            if u.path == "/api/events":
                return self.events()
            return self.send(404, {"error": "not found"})
        except ValueError as e:
            return self.send(400, {"error": str(e)})

    def do_POST(self):
        try:
            b = self.body()
            if self.path == "/api/write":
                return self.send(*write(b.get("path"), b.get("op"), b.get("data"), b.get("if_version")))
            if self.path == "/api/press":
                return self.send(*press(b.get("text"), b.get("key")))
            if self.path == "/api/inject":
                return self.send(*inject(b.get("win"), b.get("text")))
            return self.send(404, {"error": "not found"})
        except (ValueError, TypeError) as e:
            return self.send(400, {"error": str(e)})

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = queue.Queue()
        with sub_lock:
            subscribers.add(q)
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(b"data: " + msg.encode() + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with sub_lock:
                subscribers.discard(q)
            self.close_connection = True


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    os.makedirs(DB, exist_ok=True)
    print(f"pipeline-board serving on 127.0.0.1:{PORT}, store {DB}", flush=True)
    Server(("127.0.0.1", PORT), Handler).serve_forever()
