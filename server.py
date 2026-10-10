#!/usr/bin/env python3
"""Live per-client speed dashboard for the MikroTik hEX.

Polls `/tool torch` on the LAN bridge over SSH, sums the per-flow rates by LAN
client, and serves a live page at http://127.0.0.1:8088.
Stdlib only. Run: python3 server.py [--bind 127.0.0.1] [--port 8088]

Configured by environment variables (defaults in brackets):
  MIKROTIK_HOST   SSH host or ~/.ssh/config alias        [ssh.mikrotik]
  MIKROTIK_USER   SSH user (blank = from ssh config)      []
  MIKROTIK_PORT   SSH port                                [22]
  SSH_KEY         private key file (blank = ssh default)  []
  LAN_IFACE       LAN interface to torch                  [bridge]
  LAN_CIDR        LAN subnet                              [192.168.88.0/24]
  ROUTER_IP       router's LAN IP (excluded)              [192.168.88.1]
  STATIC_NAMES    JSON {ip: name} for lease-less clients  [{}]
  DATA_DIR        where aliases.json lives                [script dir]
  BIND, PORT      listen address                          [127.0.0.1, 8088]
  DASH_PASSWORD   initial login password; can be changed  [] (no login)
                  in the dashboard, changing it here resets that
  SSH_KNOWN_HOSTS known_hosts file; auto-accepts new keys []
"""
import argparse, hashlib, hmac, ipaddress, json, os, re, subprocess, threading, time
from collections import deque
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

env = os.environ.get
HOST = env("MIKROTIK_HOST", "ssh.mikrotik")
USER = env("MIKROTIK_USER", "")
SSH_PORT = env("MIKROTIK_PORT", "")
SSH_KEY = env("SSH_KEY", "")
IFACE = env("LAN_IFACE", "bridge")
LAN = ipaddress.ip_network(env("LAN_CIDR", "192.168.88.0/24"))
ROUTER_IP = env("ROUTER_IP", "192.168.88.1")
SAMPLE = 2          # torch duration per sample, seconds
HISTORY = 300       # samples kept per client (~10 min at 2s)
# Names for clients with no DHCP lease (static IPs).
STATIC_NAMES = json.loads(env("STATIC_NAMES") or "{}")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = env("DATA_DIR", HERE)
ALIASES = os.path.join(DATA_DIR, "aliases.json")   # {MAC or "ip:<ip>": alias}
CTL = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", f"mikrotik-dash-{os.getuid()}.sock")
SSH_OPTS = ["-o", "LogLevel=ERROR", "-o", "BatchMode=yes",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={CTL}", "-o", "ControlPersist=10m"]
if USER:
    SSH_OPTS += ["-l", USER]
if SSH_PORT:
    SSH_OPTS += ["-p", SSH_PORT]
if SSH_KEY:
    SSH_OPTS += ["-i", SSH_KEY, "-o", "IdentitiesOnly=yes"]
if env("SSH_KNOWN_HOSTS"):   # e.g. a writable file in the container's data volume
    SSH_OPTS += ["-o", f"UserKnownHostsFile={env('SSH_KNOWN_HOSTS')}",
                 "-o", "StrictHostKeyChecking=accept-new"]

lock = threading.Lock()
state = {"clients": {}, "names": {}, "limits": {}, "blocked": set(), "updated": 0, "error": None, "wan": None}


def ros(cmd, timeout=20):
    """Run a RouterOS command over a persistent (ControlMaster) SSH connection."""
    out = subprocess.run(
        ["ssh", *SSH_OPTS, HOST, cmd],
        capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"ssh exit {out.returncode}")
    return out.stdout


def parse_as_value(text):
    """Parse `:put [... as-value]` output (k=v;k=v;... records starting at .id)."""
    rows, cur = [], None
    for part in text.strip().split(";"):
        k, _, v = part.partition("=")
        if k == ".id":
            cur = {}
            rows.append(cur)
        if cur is not None:
            cur[k] = v
    return rows


def lan_ip(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip in LAN and addr != ROUTER_IP


def refresh_names():
    names = {ip: {"mac": "", "name": n} for ip, n in STATIC_NAMES.items()}
    for r in parse_as_value(ros(":put [/ip arp print as-value]")):
        # Skip incomplete/failed entries (no MAC = nothing answered).
        if lan_ip(r.get("address", "")) and r.get("mac-address") \
                and r.get("status") not in ("failed", "incomplete"):
            e = names.setdefault(r["address"], {"mac": "", "name": ""})
            e["mac"] = r.get("mac-address", "")
    static_ids = set(ros(":put [/ip dhcp-server lease find where !dynamic]").strip().split(";"))
    for r in parse_as_value(ros(":put [/ip dhcp-server lease print as-value]")):
        a = r.get("address", "")
        if lan_ip(a) and r.get("status") == "bound":
            e = names.setdefault(a, {"mac": r.get("mac-address", ""), "name": ""})
            e["name"] = r.get("comment") or r.get("host-name") or e["name"]
            e["host"] = r.get("host-name", "")
            e["static"] = r.get(".id") in static_ids
    with lock:
        state["names"] = names


# --- Limit / block -----------------------------------------------------------
# Limits are simple queues named "dash:<ip>"; blocks are entries in the
# dash-blocked address list. Everything we create carries TAG in its comment.
# FastTrack skips simple queues, so limited clients go in the dash-limited list
# and an accept rule placed before the defconf fasttrack rule keeps them out of it.
TAG = "dash-limit"
LIMIT_LIST, BLOCK_LIST = "dash-limited", "dash-blocked"
setup_done = False


def ensure_rules():
    """Idempotently add the fasttrack-bypass and block rules to the forward chain."""
    global setup_done
    if setup_done:
        return
    have = parse_as_value(ros(
        f':put [/ip firewall filter print as-value where comment~"^{TAG}"]'))
    have = {r.get("comment") for r in have}
    cmds = []
    top = "[:pick [/ip firewall filter find where dynamic=no] 0]"
    for direction in ("src", "dst"):
        c = f"{TAG}: block {direction}"
        if c not in have:
            cmds.append(f'/ip firewall filter add chain=forward action=drop '
                        f'{direction}-address-list={BLOCK_LIST} comment="{c}" place-before={top}')
    ft = '[/ip firewall filter find where action=fasttrack-connection]'
    for direction in ("src", "dst"):
        c = f"{TAG}: skip fasttrack {direction}"
        if c not in have:
            cmds.append(f':if ([:len {ft}] > 0) do={{/ip firewall filter add chain=forward '
                        f'action=accept connection-state=established,related '
                        f'{direction}-address-list={LIMIT_LIST} comment="{c}" '
                        f'place-before=[:pick {ft} 0]}}')
    if cmds:
        ros("; ".join(cmds))
    setup_done = True


def flush_conns(ip):
    """Drop tracked connections of ip so existing flows re-hit the new rules."""
    return (f'/ip firewall connection remove [find where src-address~"^{ip}:" '
            f'or dst-address~"^{ip}:"]')


def set_limit(ip, down_bps, up_bps):
    ensure_rules()
    if down_bps or up_bps:
        pin_lease(ip)
    q = f'[/queue simple find where name="dash:{ip}"]'
    cmds = [f"/queue simple remove {q}",
            f'/ip firewall address-list remove [find where list={LIMIT_LIST} address={ip}]']
    if down_bps or up_bps:
        cmds += [
            f'/ip firewall address-list add list={LIMIT_LIST} address={ip} comment="{TAG}"',
            # max-limit is target-upload/target-download; 0 = unlimited.
            f'/queue simple add name="dash:{ip}" target={ip}/32 '
            f'max-limit={up_bps}/{down_bps} comment="{TAG}"',
            # First match wins, so move ours to the top.
            f'/queue simple move {q} [:pick [/queue simple find] 0]',
            flush_conns(ip),
        ]
    ros("; ".join(cmds))
    refresh_controls()


def set_block(ip, blocked):
    ensure_rules()
    if blocked:
        pin_lease(ip)
    cmds = [f'/ip firewall address-list remove [find where list={BLOCK_LIST} address={ip}]']
    if blocked:
        cmds += [f'/ip firewall address-list add list={BLOCK_LIST} address={ip} comment="{TAG}"',
                 flush_conns(ip)]
    ros("; ".join(cmds))
    refresh_controls()


def parse_rate(v):
    """RouterOS rate string ('2M', '500k', '1500', '0') → bps."""
    v = (v or "0").strip()
    mult = {"k": 1e3, "M": 1e6, "G": 1e9}.get(v[-1:], 1)
    return int(float(v[:-1] if mult != 1 else v) * mult)


def refresh_controls():
    limits, blocked = {}, set()
    for r in parse_as_value(ros(
            f':put [/queue simple print as-value where comment="{TAG}"]')):
        ip = r.get("target", "").split("/")[0]
        up, _, down = r.get("max-limit", "0/0").partition("/")
        limits[ip] = {"down": parse_rate(down), "up": parse_rate(up)}
    for r in parse_as_value(ros(
            f':put [/ip firewall address-list print as-value where list={BLOCK_LIST}]')):
        blocked.add(r.get("address", ""))
    with lock:
        state["limits"], state["blocked"] = limits, blocked


def load_aliases():
    try:
        with open(ALIASES) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


aliases = load_aliases()


def alias_key(ip):
    mac = state["names"].get(ip, {}).get("mac")
    return mac.upper() if mac else f"ip:{ip}"


def alias_for(ip):
    """Alias follows the MAC (survives IP changes); IP-keyed for MAC-less clients."""
    mac = state["names"].get(ip, {}).get("mac", "").upper()
    return aliases.get(mac) or aliases.get(f"ip:{ip}") or ""


def ros_str(v):
    """Make a value safe to embed inside a RouterOS "..." string."""
    return re.sub(r'[^\w .,()\'@#+-]', "", v)[:60]


def set_alias(ip, alias):
    alias = alias.strip()[:60]
    with lock:
        key = alias_key(ip)
        if alias:
            aliases[key] = alias
        else:
            aliases.pop(key, None)
        tmp = ALIASES + ".tmp"
        with open(tmp, "w") as f:
            json.dump(aliases, f, indent=2, sort_keys=True)
        os.replace(tmp, ALIASES)
    # Mirror onto a static lease's comment so Winbox shows it too.
    ros(f'/ip dhcp-server lease set [find where address="{ip}" !dynamic] '
        f'comment="{ros_str(alias)}"')


def pin_lease(ip):
    """Turn the client's dynamic DHCP lease static, so it keeps this IP (and with
    it the limit/block, which are IP-based). No-op for clients without a lease."""
    with lock:
        name = alias_for(ip) or state["names"].get(ip, {}).get("name", "")
    cmds = [f'/ip dhcp-server lease make-static [find where address="{ip}" dynamic]']
    if name:
        cmds.append(f'/ip dhcp-server lease set [find where address="{ip}" comment=""] '
                    f'comment="{ros_str(name)}"')
    ros("; ".join(cmds))


def sample():
    """One torch sample → {ip: (down_bps, up_bps)}.

    On the bridge, torch tx = sent toward the LAN client (download) and
    rx = received from the client (upload); src-address is the LAN side.
    """
    out = ros(f":put [/tool torch interface={IFACE} src-address=0.0.0.0/0 "
              f"duration={SAMPLE}s as-value]", timeout=SAMPLE + 20)
    agg = {}
    for r in parse_as_value(out):
        src, dst = r.get("src-address", ""), r.get("dst-address", "")
        tx, rx = int(r.get("tx") or 0), int(r.get("rx") or 0)
        if lan_ip(src):
            ip, down, up = src, tx, rx
        elif lan_ip(dst):
            ip, down, up = dst, rx, tx
        else:
            continue
        d, u = agg.get(ip, (0, 0))
        agg[ip] = (d + down, u + up)
    return agg


def poller():
    last_names = 0
    while True:
        try:
            if time.time() - last_names > 30:
                refresh_names()
                refresh_controls()
                last_names = time.time()
            agg = sample()
            now = time.time()
            with lock:
                # Known clients (leases/ARP) and limited/blocked ones always get a row,
                # so an idle or blocked client can still be controlled.
                known = set(state["names"]) | set(state["limits"]) | state["blocked"]
                for ip in set(agg) | set(state["clients"]) | known:
                    d, u = agg.get(ip, (0, 0))
                    c = state["clients"].setdefault(
                        ip, {"hist": deque(maxlen=HISTORY), "peak_down": 0, "peak_up": 0,
                             "bytes_down": 0, "bytes_up": 0, "last_active": 0})
                    c["hist"].append((round(now, 1), d, u))
                    c["peak_down"] = max(c["peak_down"], d)
                    c["peak_up"] = max(c["peak_up"], u)
                    c["bytes_down"] += d * SAMPLE / 8
                    c["bytes_up"] += u * SAMPLE / 8
                    if d or u:
                        c["last_active"] = now
                state["updated"] = now
                state["error"] = None
        except Exception as e:  # keep polling; surface the error in the UI
            with lock:
                state["error"] = str(e)
            time.sleep(3)


def snapshot():
    with lock:
        out = []
        for ip, c in state["clients"].items():
            n = state["names"].get(ip, {})
            h = list(c["hist"])
            out.append({
                "ip": ip, "name": alias_for(ip) or n.get("name", ""), "alias": alias_for(ip),
                "host": n.get("host", ""), "static": n.get("static", False), "mac": n.get("mac", ""),
                "down": h[-1][1] if h else 0, "up": h[-1][2] if h else 0,
                "peak_down": c["peak_down"], "peak_up": c["peak_up"],
                "bytes_down": c["bytes_down"], "bytes_up": c["bytes_up"],
                "last_active": c["last_active"], "hist": h,
                "limit": state["limits"].get(ip), "blocked": ip in state["blocked"],
            })
        return {"clients": out, "updated": state["updated"], "error": state["error"],
                "sample": SAMPLE}


# --- Auth --------------------------------------------------------------------
# Optional: with a password set, every API call needs a login.
# DASH_PASSWORD is the initial password. Changing it in the dashboard stores a
# salted PBKDF2 hash in DATA_DIR/auth.json, which then wins — until
# DASH_PASSWORD itself is changed, which resets it (the lock-out escape hatch).
# The session cookie is an HMAC keyed by the stored hash: it survives restarts
# and every session is invalidated when the password changes.
PASSWORD = env("DASH_PASSWORD", "")
AUTH_FILE = os.path.join(DATA_DIR, "auth.json")
COOKIE = "mtdash_session"
MIN_PASSWORD = 8
ENV_SALT = b"mikrotik-dash-env"   # fixed so the env password hashes the same on every start


def hash_pw(pw, salt=None, iters=200_000):
    salt = salt or os.urandom(16)
    return {"salt": salt.hex(), "iters": iters,
            "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters).hex()}


def load_auth():
    env_rec = hash_pw(PASSWORD, ENV_SALT) if PASSWORD else None
    try:
        with open(AUTH_FILE) as f:
            rec = json.load(f)
    except (FileNotFoundError, ValueError):
        return env_rec
    if env_rec and rec.get("env_hash") != env_rec["hash"]:
        # DASH_PASSWORD changed since the dashboard password was set: reset.
        os.remove(AUTH_FILE)
        print("DASH_PASSWORD changed: dashboard-set password reset", flush=True)
        return env_rec
    return rec


auth = load_auth()


def check_pw(pw):
    rec = auth
    if not rec:
        return False
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(rec["salt"]), rec["iters"]).hex()
    return hmac.compare_digest(h, rec["hash"])


def set_password(pw):
    global auth
    rec = hash_pw(pw)
    if PASSWORD:
        rec["env_hash"] = hash_pw(PASSWORD, ENV_SALT)["hash"]
    tmp = AUTH_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, AUTH_FILE)
    auth = rec


def session_token():
    return hmac.new(bytes.fromhex(auth["hash"]), b"mikrotik-dash-session", hashlib.sha256).hexdigest()


def session_cookie():
    return ("Set-Cookie", f"{COOKIE}={session_token()}; Max-Age=2592000; Path=/; "
                          f"HttpOnly; SameSite=Strict")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def authed(self):
        if not auth:
            return True
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return COOKIE in c and hmac.compare_digest(c[COOKIE].value, session_token())

    def reply(self, code, body=b"", ctype="text/html; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        # The page itself is public (it holds the login view); data needs a session.
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.reply(200, f.read())
        if not self.authed():
            return self.reply(401, b'{"error": "login required"}', "application/json")
        if path == "/api/stats":
            data = snapshot()
            data["auth"] = bool(auth)
            self.reply(200, json.dumps(data).encode(), "application/json")
        else:
            self.send_error(404)

    def do_POST(self):
        length = min(int(self.headers.get("Content-Length", 0)), 64 * 1024)
        raw = self.rfile.read(length)
        if self.path == "/api/login":
            try:
                pw = str(json.loads(raw).get("password", ""))
            except ValueError:
                pw = ""
            if check_pw(pw):
                return self.reply(200, b'{"ok": true}', "application/json", [session_cookie()])
            time.sleep(1)   # slow down password guessing
            return self.reply(401, b'{"ok": false, "error": "Wrong password"}', "application/json")
        if self.path == "/api/logout":
            return self.reply(200, b'{"ok": true}', "application/json", [("Set-Cookie",
                              f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict")])
        if self.path == "/api/password":
            if not self.authed() or not auth:
                return self.reply(401, b'{"ok": false, "error": "login required"}', "application/json")
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.send_error(415)
            try:
                req = json.loads(raw)
                current, new = str(req.get("current", "")), str(req.get("new", ""))
            except ValueError:
                return self.reply(400, b'{"ok": false, "error": "bad request"}', "application/json")
            if not check_pw(current):
                time.sleep(1)
                return self.reply(403, b'{"ok": false, "error": "Current password is wrong"}',
                                  "application/json")
            if len(new) < MIN_PASSWORD:
                return self.reply(400, json.dumps({"ok": False, "error":
                                  f"New password must be at least {MIN_PASSWORD} characters"}).encode(),
                                  "application/json")
            set_password(new)
            # New hash → every old session is invalid; keep this one logged in.
            return self.reply(200, b'{"ok": true}', "application/json", [session_cookie()])
        # JSON-only so a cross-site form post can't trigger it (needs CORS preflight).
        if self.path != "/api/control":
            return self.send_error(404)
        if not self.authed():
            return self.reply(401, b'{"ok": false, "error": "login required"}', "application/json")
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_error(415)
        try:
            req = json.loads(raw)
            ip = str(req.get("ip", ""))
            if not lan_ip(ip):
                raise ValueError("not a LAN client IP")
            action = req.get("action")
            if action == "limit":
                down = int(float(req.get("down_mbps") or 0) * 1e6)
                up = int(float(req.get("up_mbps") or 0) * 1e6)
                if down < 0 or up < 0:
                    raise ValueError("negative rate")
                set_limit(ip, down, up)
            elif action == "unlimit":
                set_limit(ip, 0, 0)
            elif action == "alias":
                set_alias(ip, str(req.get("alias", "")))
            elif action in ("block", "unblock"):
                set_block(ip, action == "block")
            else:
                raise ValueError("unknown action")
            code, body = 200, {"ok": True}
        except Exception as e:
            code, body = 400, {"ok": False, "error": str(e)}
        self.reply(code, json.dumps(body).encode(), "application/json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(env("PORT", "8088")))
    ap.add_argument("--bind", default=env("BIND", "127.0.0.1"))
    args = ap.parse_args()
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=poller, daemon=True).start()
    print(f"MikroTik dashboard on http://{args.bind}:{args.port}"
          f" ({'password protected' if auth else 'no password'})", flush=True)
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()
