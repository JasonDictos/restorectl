#!/usr/bin/env python3
"""restorectl-web — a small read-only HTTP API + UI over restic repositories.

Runs on the machine that holds the repos (typically the backup server), binds
to localhost, and is exposed through nginx. It shells out to `restic --json`
rather than reimplementing the format, so it can never disagree with the CLI.

Everything here is read-only: there is no endpoint that writes, forgets or
prunes. The worst a caller can do is read a file that is already in a backup.
That is still your entire filesystem, so put authentication in front of it —
see nginx-restorectl.conf.

MIT licensed. https://github.com/JasonDictos/restorectl
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOTS = [
    "/PlatterArray/Backups/restic-profile",
    "/mnt/backup/restic-profile",
    "/SsdArray/Archive/restic-profile",
    "/mnt/archive/restic-profile",
]
# Host-specific keys FIRST. /etc/restic/password is this machine's own key,
# so it is only ever valid for the local host -- consulting it for a remote
# host's repo yields "wrong password or no key found".
PASSWORD_CANDIDATES = [
    "/SsdArray/Archive/keys/jason/restic-password-{host}",
    "/mnt/archive/keys/jason/restic-password-{host}",
    "/PlatterArray/Backups/keys/restic-password-{host}",
]
LOCAL_ONLY_PASSWORDS = ["/etc/restic/password"]
LISTEN = os.environ.get("RESTORECTL_WEB_BIND", "127.0.0.1")
PORT = int(os.environ.get("RESTORECTL_WEB_PORT", "8723"))
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_TTL = 30

_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SNAP_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")


def discover_repos():
    """Map hostname -> repo path, honouring REPO_ROOTS priority order."""
    found = {}
    for root in REPO_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for host in entries:
            path = os.path.join(root, host)
            if host not in found and os.path.isfile(os.path.join(path, "config")):
                found[host] = path
    return found


def password_file(host):
    for pat in PASSWORD_CANDIDATES:
        p = pat.format(host=host)
        if os.path.isfile(p):
            return p
    if host == socket.gethostname().split(".")[0]:
        for p in LOCAL_ONLY_PASSWORDS:
            if os.path.isfile(p):
                return p
    return None


class ResticError(RuntimeError):
    pass


def restic(host, args, raw=False, timeout=120):
    repos = discover_repos()
    if host not in repos:
        raise ResticError(f"no repository for host {host!r}")
    pw = password_file(host)
    if not pw:
        raise ResticError(f"no password file for host {host!r}")
    env = dict(os.environ,
               RESTIC_REPOSITORY=repos[host],
               RESTIC_PASSWORD_FILE=pw,
               HOME=os.environ.get("HOME", "/root"))
    # --no-lock on every call: this service is strictly read-only, and
    # without it any browse blocks for the entire duration of a running
    # backup (which is precisely when someone wants to look).
    cmd = ["restic", "--no-lock"] + args
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ResticError("restic timed out")
    if proc.returncode != 0:
        raise ResticError(proc.stderr.decode("utf-8", "replace")[:400] or "restic failed")
    return proc.stdout if raw else proc.stdout.decode("utf-8", "replace")


_cache = {}


def cached(key, producer):
    now = datetime.now().timestamp()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    val = producer()
    _cache[key] = (now, val)
    return val


def snapshots(host):
    def go():
        out = restic(host, ["snapshots", "--json"])
        snaps = json.loads(out or "[]")
        for s in snaps:
            # Normalise the timestamp into something the browser can group on
            # without every client reimplementing restic's format.
            raw = s.get("time", "")
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                s["ts"] = dt.astimezone().isoformat()
                s["day"] = dt.astimezone().strftime("%Y-%m-%d")
                s["clock"] = dt.astimezone().strftime("%H:%M")
            except ValueError:
                s["ts"] = raw
                s["day"] = raw[:10]
                s["clock"] = raw[11:16]
            s["short_id"] = s.get("short_id") or s.get("id", "")[:8]
        snaps.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return snaps
    return cached(f"snaps:{host}", go)


def listdir(host, snap, path):
    out = restic(host, ["ls", snap, path, "--json"])
    base = path.rstrip("/") or "/"
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        p = m.get("path")
        if not p or m.get("struct_type") == "snapshot":
            continue
        parent = os.path.dirname(p.rstrip("/")) or "/"
        if parent != base:
            continue
        rows.append({
            "name": os.path.basename(p.rstrip("/")),
            "path": p,
            "type": m.get("type"),
            "size": m.get("size", 0),
            "mtime": m.get("mtime", ""),
            "mode": m.get("mode", 0),
        })
    rows.sort(key=lambda r: (r["type"] != "dir", r["name"].lower()))
    return rows


class Handler(BaseHTTPRequestHandler):
    server_version = "restorectl-web/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, indent=None), "application/json")

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        route = u.path.rstrip("/") or "/"
        # nginx may mount us under a prefix; tolerate it.
        for prefix in ("/restorectl",):
            if route.startswith(prefix):
                route = route[len(prefix):] or "/"

        def arg(name, pattern=None, default=None, required=False):
            val = (q.get(name) or [default])[0]
            if required and not val:
                raise ValueError(f"missing parameter {name}")
            if val and pattern and not pattern.match(val):
                raise ValueError(f"bad parameter {name}")
            return val

        try:
            if route == "/":
                return self._serve_index()
            if route == "/api/hosts":
                repos = discover_repos()
                out = []
                for host, path in sorted(repos.items()):
                    entry = {"host": host, "repo": path,
                             "password": bool(password_file(host))}
                    try:
                        snaps = snapshots(host)
                        entry["snapshots"] = len(snaps)
                        entry["latest"] = snaps[0]["ts"] if snaps else None
                        entry["ok"] = True
                    except ResticError as exc:
                        entry.update(ok=False, error=str(exc), snapshots=0, latest=None)
                    out.append(entry)
                return self._json({"hosts": out})

            if route == "/api/snapshots":
                host = arg("host", _HOST_RE, required=True)
                return self._json({"host": host, "snapshots": snapshots(host)})

            if route == "/api/ls":
                host = arg("host", _HOST_RE, required=True)
                snap = arg("snap", _SNAP_RE, default="latest")
                path = arg("path", default="/") or "/"
                if "\x00" in path:
                    raise ValueError("bad path")
                return self._json({"host": host, "snap": snap, "path": path,
                                   "entries": listdir(host, snap, path)})

            if route == "/api/stats":
                host = arg("host", _HOST_RE, required=True)
                out = restic(host, ["stats", "--mode", "raw-data", "--json"])
                return self._json(json.loads(out or "{}"))

            if route == "/api/download":
                host = arg("host", _HOST_RE, required=True)
                snap = arg("snap", _SNAP_RE, default="latest")
                path = arg("path", required=True)
                if "\x00" in path:
                    raise ValueError("bad path")
                blob = restic(host, ["dump", snap, path], raw=True, timeout=600)
                name = os.path.basename(path.rstrip("/")) or "download"
                return self._send(200, blob, "application/octet-stream",
                                  {"Content-Disposition":
                                   f'attachment; filename="{name}"'})

            return self._err(404, "not found")
        except ValueError as exc:
            return self._err(400, str(exc))
        except ResticError as exc:
            return self._err(502, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface anything else as 500
            return self._err(500, f"{type(exc).__name__}: {exc}")

    def _serve_index(self):
        idx = os.path.join(HERE, "index.html")
        try:
            with open(idx, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        except OSError:
            return self._err(500, "index.html missing")


def main():
    if not shutil.which("restic"):
        sys.exit("restorectl-web: restic not found in PATH")
    repos = discover_repos()
    sys.stderr.write(f"restorectl-web on {LISTEN}:{PORT}; repos: "
                     f"{', '.join(sorted(repos)) or 'none found'}\n")
    ThreadingHTTPServer((LISTEN, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
