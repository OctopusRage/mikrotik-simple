# mikrotik-dash

A small live dashboard for a MikroTik router: per-client download/upload speed,
plus speed limits, blocking (optionally timed), client groups and name aliases. Python stdlib only, no router
password needed — it talks to RouterOS over SSH.

## Run

**Docker (recommended):**

```sh
cp .env.example .env      # set MIKROTIK_HOST, SSH_KEY_FILE, DASH_PASSWORD, ...
docker compose up -d --build
```

**As a systemd user service:**

```sh
./install.sh              # asks for router host + password, then starts it
./install.sh --uninstall
```

**By hand:** `python3 server.py` (config via environment variables, listed at
the top of `server.py`).

The router must accept key-based SSH from wherever this runs (import the public
key on the router with `/user ssh-keys import`).

## How it works

- **Speeds:** every 2 s it runs `/tool torch` on the LAN bridge and sums the
  per-flow rates by LAN client. Torch takes LAN traffic off the router's fast
  path (on a hEX this cut peak download throughput by ~35%), so it only runs
  while someone has the dashboard open: the page stops polling when its tab is
  hidden, and the server stops torch 15 s after the last poll. Graphs and
  totals therefore cover the time the page was open. Names come from DHCP leases (comment, then
  host-name) and ARP.
- **Limit:** a simple queue `dash:<ip>` (`max-limit=up/down`). FastTrack skips
  queues, so limited clients go into the `dash-limited` address list and an
  accept rule above the defconf fasttrack rule keeps them out of FastTrack.
- **Block:** the `dash-blocked` address list plus drop rules at the top of the
  forward chain. A timed block is an address-list entry with `timeout=`, so the
  router lifts it by itself, even if the dashboard is down.
- **Lease pinning:** limiting or blocking a client first makes its DHCP lease
  static, so it keeps the IP the rule applies to.
- **Aliases:** stored locally in `aliases.json` (keyed by MAC) and mirrored to
  the static lease comment.
- **Groups:** stored locally in `groups.json` (members keyed by MAC). Blocking or
  limiting a group applies the same block or per-client limit to each member it
  can currently find on the network; a limit is not shared across the group.

Everything it creates on the router is commented `dash-limit`. Changing a limit
or block drops the client's tracked connections so it takes effect at once.

Set `DASH_PASSWORD` to require a login (session cookie, HttpOnly, SameSite=Strict).
The password can be changed in the dashboard (**Change password**, top right); it
is stored as a salted PBKDF2 hash in `DATA_DIR/auth.json` and logs out other
devices. Forgot it? Change `DASH_PASSWORD` and restart: that resets it. API calls only accept JSON POSTs, so a
cross-site form can't trigger them. Without a password, keep it bound to
localhost.
