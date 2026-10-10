# mikrotik-dash

A small live dashboard for a MikroTik router: per-client download/upload speed,
plus speed limits, blocking and name aliases. Python stdlib only, no router
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
  per-flow rates by LAN client. Names come from DHCP leases (comment, then
  host-name) and ARP.
- **Limit:** a simple queue `dash:<ip>` (`max-limit=up/down`). FastTrack skips
  queues, so limited clients go into the `dash-limited` address list and an
  accept rule above the defconf fasttrack rule keeps them out of FastTrack.
- **Block:** the `dash-blocked` address list plus drop rules at the top of the
  forward chain.
- **Lease pinning:** limiting or blocking a client first makes its DHCP lease
  static, so it keeps the IP the rule applies to.
- **Aliases:** stored locally in `aliases.json` (keyed by MAC) and mirrored to
  the static lease comment.

Everything it creates on the router is commented `dash-limit`. Changing a limit
or block drops the client's tracked connections so it takes effect at once.

Set `DASH_PASSWORD` to require a login (session cookie, HttpOnly, SameSite=Strict;
changing the password logs everyone out). API calls only accept JSON POSTs, so a
cross-site form can't trigger them. Without a password, keep it bound to
localhost.
