#!/usr/bin/env python3
"""Plop Catcher leaderboard. Tiny stdlib-only HTTP API backed by SQLite.

GET  /scores          -> top 15, best score per name
POST /scores          {"name": str, "score": int, "catches": int, "ms": int}
POST /ideas           {"idea": str, "name": str (optional)}  game ideas from the arcade page; never served back
POST /pooff/join|tick|start|leave   Poo Off: one live multiplayer room, kept in memory (see the POO OFF section)
`python3 plop_scores.py ideas` prints the saved ideas (run with STATE_DIRECTORY set to the DB folder).
"""
import hmac, json, os, re, secrets, sqlite3, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.path.join(os.environ.get("STATE_DIRECTORY", "."), "scores.db")
ORIGINS = {"https://frazuur.github.io"}
PORT = int(os.environ.get("PORT", "8787"))
NAME_RE = re.compile(r"[^\w .'!?-]", re.UNICODE)

lock = threading.Lock()
hits = {}  # ip -> [timestamps]

def db():
    con = sqlite3.connect(DB)
    con.execute("create table if not exists scores(id integer primary key, name text, score int, catches int, ms int, at int)")
    con.execute("create table if not exists ideas(id integer primary key, idea text, name text, at int)")
    return con

def top():
    with lock, db() as con:
        rows = con.execute(
            "select name, max(score) s from scores group by lower(name) order by s desc, min(at) asc limit 15").fetchall()
    return [{"name": n, "score": s} for n, s in rows]

def limited(ip, kind="scores", most=6, window=60):
    now, key = time.time(), (kind, ip)
    with lock:
        ts = [t for t in hits.get(key, []) if now - t < window]
        ts.append(now)
        hits.pop(key, None)
        hits[key] = ts  # re-insert so dict order is oldest-used first
        while len(hits) > 5000:
            del hits[next(iter(hits))]
    return len(ts) > most

CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

def add_idea(d):
    idea = CTRL_RE.sub("", str(d.get("idea", ""))).strip()[:600]
    name = NAME_RE.sub("", str(d.get("name", "") or "")).strip()[:24]
    if len(idea) < 3:
        return False
    with lock, db() as con:
        if con.execute("select count(*) from ideas").fetchone()[0] >= 5000:
            return False
        con.execute("insert into ideas(idea, name, at) values(?,?,?)", (idea, name, int(time.time())))
    return True

# ==== LOCKDOWN ====
KEY = os.environ.get("PLOP_KEY", "")
DEV = os.environ.get("PLOP_DEV") == "1"
DEV_RE = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d{1,5})?$")
BODY_MAX = 8192
ROUTES = {("GET", "/scores"), ("POST", "/scores"), ("POST", "/ideas"), ("POST", "/pooff/join"),
          ("POST", "/pooff/tick"), ("POST", "/pooff/start"), ("POST", "/pooff/leave")}
# group -> (tokens per second, burst). Ticks are per IP generous (households share an IP),
# and also limited per player inside po_handle's caller.
LIMITS = {"tick": (45.0, 60.0), "tickp": (12.0, 20.0), "pooff": (2.0, 6.0),
          "post_scores": (6 / 60, 6.0), "ideas": (5 / 3600, 5.0), "get_scores": (2.0, 4.0)}
buckets, blk = {}, threading.Lock()
last_prune = [time.time()]

def no_const(c):
    raise ValueError("NaN/Infinity not allowed")

def allow_origin(o):
    return o in ORIGINS or (DEV and bool(DEV_RE.match(o)))

def take(group, who):
    rate, burst = LIMITS[group]
    now = time.time()
    with blk:
        if now - last_prune[0] > 60:
            for k in [k for k, (t, at) in buckets.items() if now - at > 3600]:
                del buckets[k]
            last_prune[0] = now
        if len(buckets) > 20000:  # evict the least recently used, never everyone
            for k in sorted(buckets, key=lambda k: buckets[k][1])[:5000]:
                del buckets[k]
        tok, at = buckets.get((group, who), (burst, now))
        tok = min(burst, tok + (now - at) * rate)
        ok = tok >= 1
        buckets[(group, who)] = (tok - 1 if ok else tok, now)
    return ok

class H(BaseHTTPRequestHandler):
    server_version = "plop"
    timeout = 10

    def cors(self):
        o = self.headers.get("Origin", "")
        if allow_origin(o):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Plop-Key")
            self.send_header("Access-Control-Max-Age", "600")

    def bare(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()[:64]

    def gate(self, method):
        """Allowlist, origin, key, rate limit, body size. Returns parsed body (or {} for GET), or None after replying."""
        path = self.path.split("?")[0]
        if (method, path) not in ROUTES:
            return self.bare(404)
        o = self.headers.get("Origin")
        if not (method == "GET" and path == "/scores") and not allow_origin(o or ""):
            return self.bare(403)
        if KEY and path != "/pooff/leave" and not hmac.compare_digest(
                self.headers.get("X-Plop-Key", "").encode(), KEY.encode()):
            return self.bare(403)
        group = {"/pooff/tick": "tick", "/ideas": "ideas"}.get(path) or \
            ("pooff" if path.startswith("/pooff/") else ("get_scores" if method == "GET" else "post_scores"))
        if not take(group, self.ip()):
            return self.reply(429, {"error": "Slow down a bit, then try again."})
        self.route = path
        if method == "GET":
            return {}
        cl = self.headers.get("Content-Length")
        if cl is None:
            return self.bare(411)
        if not (cl.isascii() and cl.isdigit()) or len(cl) > 6:
            return self.bare(400)
        n = int(cl)
        if n > BODY_MAX:
            return self.bare(413)
        try:
            d = json.loads(self.rfile.read(n), parse_constant=no_const)
        except Exception:
            d = None
        if not isinstance(d, dict):
            return self.reply(400, {"error": "That didn't look right."})
        return d

    def do_HEAD(self): self.bare(405)
    def do_PUT(self): self.bare(405)
    def do_DELETE(self): self.bare(405)
    def do_PATCH(self): self.bare(405)
    def do_TRACE(self): self.bare(405)
    def do_CONNECT(self): self.bare(405)

    def reply(self, code, body):
        data = json.dumps(body, allow_nan=False).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        if not any(p == self.path.split("?")[0] for _, p in ROUTES):
            return self.bare(404)
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        if self.gate("GET") is None:
            return
        self.reply(200, top())

    def do_POST(self):
        d = self.gate("POST")
        if d is None:
            return
        if self.route.startswith("/pooff/"):
            return self.pooff(d)
        if self.route == "/ideas":
            try:
                ok = add_idea(d)
            except Exception:
                ok = False
            return self.reply(200, {"ok": True}) if ok else self.reply(400, {"error": "Write a few words about your idea first."})
        try:
            name = NAME_RE.sub("", str(d["name"])).strip()[:16]
            score, catches, ms = int(d["score"]), int(d["catches"]), int(d["ms"])
        except Exception:
            return self.reply(400, {"error": "That score didn't look right."})
        # plausibility: max 15 points per catch (gold x3), at least ~0.25s per catch, sane caps
        if not name or not (0 <= score <= 50000) or not (0 <= catches <= 5000) \
                or score > catches * 15 or ms < catches * 250 or ms > 3 * 3600 * 1000:
            return self.reply(400, {"error": "That score didn't look right."})
        with lock, db() as con:
            con.execute("insert into scores(name, score, catches, ms, at) values(?,?,?,?,?)",
                        (name, score, catches, ms, int(time.time())))
        self.reply(200, top())

    def pooff(self, d):
        act = self.route[len("/pooff/"):]
        if act == "join" and limited(self.ip(), "pooff-join", 20, 600):
            return self.reply(429, {"error": "Slow down a bit, then try again."})
        if int(self.headers.get("Content-Length")) > PO_BODY:
            return self.bare(413)
        code, body = po_handle(act, d)
        self.reply(code, body)

    def log_message(self, *a):
        pass

# ==== POO OFF: one always-open live room, in memory (lost on restart, which is fine) ====
# Phases: lobby -> countdown -> playing -> results -> lobby. Clients poll /pooff/tick about 5x a second.
PO_MAX, PO_STALE, PO_COUNT_MS, PO_RESULTS_MS, PO_SNAP, PO_BODY = 8, 6.0, 3000, 12000, 4096, 6144
PO_STALE = float(os.environ.get("PO_STALE", PO_STALE))  # tests on a loaded machine only
PO_MATCH_MS = int(os.environ.get("PO_MATCH_MS", 20 * 60 * 1000))  # a match never runs past 20 minutes
po_lock = threading.Lock()
room = {"phase": "lobby", "seed": 0, "startAt": 0, "endAt": 0, "players": {}, "n": 0}

def po_ms():
    return int(time.time() * 1000)

def po_fresh(p):
    p.update(score=0, lives=3, catches=0, alive=True, outAt=None, place=None, snap=None, snapAt=0)

def po_step(now):
    """Advance the room clock: drop or knock out silent players, move between phases."""
    ps, t = room["players"], now / 1000
    live = room["phase"] in ("countdown", "playing", "results")
    for pid, p in list(ps.items()):
        if t - p["seen"] > PO_STALE:
            if live and p["inMatch"]:
                if p["alive"] and room["phase"] != "results":
                    p.update(alive=False, outAt=now)
            else:
                del ps[pid]
    if room["phase"] == "countdown" and now >= room["startAt"]:
        room["phase"] = "playing"
    if room["phase"] in ("countdown", "playing"):
        m = [p for p in ps.values() if p["inMatch"]]
        alive = [p for p in m if p["alive"]]
        if not m:
            po_to_lobby()
        elif room["phase"] == "playing" and (not alive or now - room["startAt"] >= PO_MATCH_MS or (
                len(m) >= 2 and len(alive) == 1 and all(alive[0]["score"] > p["score"] for p in m if p is not alive[0]))):
            # everyone's out, the last one standing is already ahead of everyone, or the 20 minute cap:
            # rank everyone as they stand
            m.sort(key=lambda p: (-p["score"], 0 if p["alive"] else 1, -(p["outAt"] or 0), p["n"]))
            for i, p in enumerate(m):
                p["place"] = i + 1
            room.update(phase="results", endAt=now)
    if room["phase"] == "results" and now - room["endAt"] >= PO_RESULTS_MS:
        po_to_lobby()

def po_to_lobby():
    room["phase"] = "lobby"
    for pid, p in list(room["players"].items()):
        if time.time() - p["seen"] > PO_STALE:
            del room["players"][pid]
        else:
            p["inMatch"] = False
            po_fresh(p)

def po_state(now, me, watch=None):
    out = {"phase": room["phase"], "now": now, "seed": room["seed"], "startAt": room["startAt"], "you": me["id"],
           "players": [{k: p[k] for k in ("id", "name", "score", "lives", "alive", "outAt", "inMatch", "place")}
                       for p in sorted(room["players"].values(), key=lambda p: p["n"])]}
    w = room["players"].get(watch) if isinstance(watch, str) else None
    if w and w["snap"] is not None and w["id"] != me["id"]:
        out.update(watchId=w["id"], watchSnap=w["snap"], watchAt=w["snapAt"])
    return out

def po_int(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError
    try:
        return max(lo, min(hi, int(v)))
    except (OverflowError, TypeError):
        raise ValueError

def po_half(v, lo, hi):
    """Lives come in halves (a tiny poo costs half a roll)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        raise ValueError
    return max(lo, min(hi, round(float(v) * 2) / 2))

def po_snap(s):
    """Return a clean copy of a snapshot {t, o: [[id, type, x, y], ...]}, or None if it isn't exactly that."""
    if not isinstance(s, dict) or set(s) != {"t", "o"}:
        return None
    t, o = s["t"], s["o"]
    ok = lambda v, lo, hi: type(v) is int and lo <= v <= hi
    if not ok(t, -500, 1500) or not isinstance(o, list) or len(o) > 40:
        return None
    for q in o:
        if not (isinstance(q, list) and len(q) == 4 and ok(q[0], 0, 2 ** 31) and ok(q[1], 0, 30)
                and ok(q[2], -1000, 2000) and ok(q[3], -1000, 2000)):
            return None
    return {"t": t, "o": [list(q) for q in o]}

def po_handle(act, d):
    now = po_ms()
    with po_lock:
        po_step(now)
        ps = room["players"]
        if act == "join":
            name = NAME_RE.sub("", str(d.get("name", ""))).strip()[:16]
            if not name:
                return 400, {"error": "Type a name first."}
            if len(ps) >= PO_MAX:
                return 409, {"error": "The room's full (8 players). Try again after this round."}
            room["n"] += 1
            p = {"id": secrets.token_hex(4), "token": secrets.token_urlsafe(16), "name": name, "n": room["n"],
                 "seen": time.time(), "inMatch": False}
            po_fresh(p)
            ps[p["id"]] = p
            return 200, dict(po_state(now, p), token=p["token"])
        p = ps.get(d.get("id")) if isinstance(d.get("id"), str) else None
        tok = d.get("token")
        if not p or not isinstance(tok, str) or not hmac.compare_digest(tok.encode(), p["token"].encode()):
            return 403, {"error": "You're not in the room."}
        if act == "tick" and not take("tickp", p["id"]):
            return 429, {"error": "Slow down a bit, then try again."}
        p["seen"] = time.time()
        if act == "leave":
            if p["inMatch"] and room["phase"] in ("countdown", "playing", "results"):
                if p["alive"] and room["phase"] != "results":
                    p.update(alive=False, outAt=now)
                p["seen"] = 0  # dropped when the room goes back to the lobby
            else:
                del ps[p["id"]]
            po_step(now)
            return 200, {"ok": True}
        if act == "start":
            if room["phase"] != "lobby":
                return 409, {"error": "A round is already on."}
            if len(ps) < 2:
                return 409, {"error": "You need at least two players."}
            for q in ps.values():
                q["inMatch"] = True
                po_fresh(q)
            room.update(phase="countdown", seed=secrets.randbelow(2 ** 31), startAt=now + PO_COUNT_MS)
            return 200, po_state(now, p)
        # tick
        if p["inMatch"] and p["alive"] and room["phase"] == "playing":
            try:
                elapsed = max(0, now - room["startAt"])
                catches = po_int(d.get("catches"), 0, min(5000, elapsed // 250 + 1))
                score = po_int(d.get("score"), 0, min(50000, catches * 15))
                lives = po_half(d.get("lives"), 0, p["lives"])
                if not isinstance(d.get("alive"), bool):
                    raise ValueError
            except ValueError:
                return 400, {"error": "That didn't look right."}
            p.update(score=score, catches=catches, lives=lives)
            if d["alive"] is False or lives <= 0:
                p.update(alive=False, lives=0, outAt=now)
            snap = po_snap(d.get("snap"))
            if snap is not None and len(json.dumps(snap, separators=(",", ":"))) <= PO_SNAP:
                p.update(snap=snap, snapAt=now)
            po_step(now)
        w = d.get("watch")
        return 200, po_state(now, p, w if isinstance(w, str) and len(w) <= 16 else None)

if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["ideas"]:
        with db() as con:
            for i, idea, name, at in con.execute("select id, idea, name, at from ideas order by at"):
                print(f"#{i} {time.strftime('%Y-%m-%d %H:%M', time.localtime(at))} {name or 'anon'}: {idea}")
        sys.exit()
    db().close()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
