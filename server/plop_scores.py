#!/usr/bin/env python3
"""Plop Catcher leaderboard. Tiny stdlib-only HTTP API backed by SQLite.

GET  /scores          -> top 15, best score per name
POST /scores          {"name": str, "score": int, "catches": int, "ms": int}
POST /ideas           {"idea": str, "name": str (optional)}  game ideas from the arcade page; never served back
`python3 plop_scores.py ideas` prints the saved ideas (run with STATE_DIRECTORY set to the DB folder).
"""
import json, os, re, sqlite3, time, threading
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
        hits[key] = ts
        if len(hits) > 5000:
            hits.clear()
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

class H(BaseHTTPRequestHandler):
    server_version = "plop"

    def cors(self):
        o = self.headers.get("Origin", "")
        if o in ORIGINS or o.startswith("http://localhost") or o == "null":
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] != "/scores":
            return self.reply(404, {"error": "not found"})
        self.reply(200, top())

    def do_POST(self):
        if self.path not in ("/scores", "/ideas"):
            return self.reply(404, {"error": "not found"})
        ip = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
        if self.path == "/ideas":
            if limited(ip, "ideas", 5, 600):
                return self.reply(429, {"error": "That's a lot of ideas! Try again in a few minutes."})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n > 4096:
                    raise ValueError
                ok = add_idea(json.loads(self.rfile.read(n)))
            except Exception:
                ok = False
            return self.reply(200, {"ok": True}) if ok else self.reply(400, {"error": "Write a few words about your idea first."})
        if limited(ip):
            return self.reply(429, {"error": "Slow down a bit, then try again."})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 1024:
                raise ValueError
            d = json.loads(self.rfile.read(n))
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

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["ideas"]:
        with db() as con:
            for i, idea, name, at in con.execute("select id, idea, name, at from ideas order by at"):
                print(f"#{i} {time.strftime('%Y-%m-%d %H:%M', time.localtime(at))} {name or 'anon'}: {idea}")
        sys.exit()
    db().close()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
