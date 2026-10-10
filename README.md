# mikrotik-dash

A small live dashboard for a MikroTik router: per-client download/upload speed,
plus speed limits, blocking and name aliases. Python stdlib only, no router
password needed — it talks to RouterOS over SSH.

## Run

```sh
python3 server.py            # http://127.0.0.1:8088
python3 server.py --bind 0.0.0.0 --port 8088   # reachable from the LAN
```

Requires an SSH host alias `ssh.mikrotik` (key auth) to the router. Edit the
constants at the top of `server.py` for your setup (`HOST`, `IFACE`, `LAN`,
`ROUTER_IP`, `STATIC_NAMES`).

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

The control endpoint only accepts JSON POSTs, so a cross-site form can't trigger
it. There is no login; keep it bound to localhost or a trusted LAN.
