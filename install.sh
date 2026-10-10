#!/usr/bin/env bash
# Install mikrotik-dash as a systemd *user* service (no root needed).
#
#   ./install.sh               install / upgrade, then start it
#   ./install.sh --no-start    install files only
#   ./install.sh --uninstall   stop and remove the service and app files
#                              (keeps the config and aliases)
#
# Paths can be overridden with APP_DIR, CONF_DIR and UNIT_DIR.
set -euo pipefail

APP_DIR=${APP_DIR:-$HOME/.local/share/mikrotik-dash}
CONF_DIR=${CONF_DIR:-$HOME/.config/mikrotik-dash}
UNIT_DIR=${UNIT_DIR:-$HOME/.config/systemd/user}
ENV_FILE=$CONF_DIR/env
UNIT=$UNIT_DIR/mikrotik-dash.service
SRC=$(cd "$(dirname "$0")" && pwd)

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [[ ${1:-} == --uninstall ]]; then
  say "Stopping and removing the service"
  systemctl --user disable --now mikrotik-dash.service 2>/dev/null || true
  rm -f "$UNIT" "$APP_DIR/server.py" "$APP_DIR/index.html"
  systemctl --user daemon-reload 2>/dev/null || true
  say "Removed. Config and aliases kept in $CONF_DIR and $APP_DIR/data"
  exit 0
fi
START=1
[[ ${1:-} == --no-start ]] && START=0

# --- Requirements -----------------------------------------------------------
command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "python3 >= 3.8 is required"
command -v ssh >/dev/null || die "ssh (openssh client) is required"
if (( START )); then
  command -v systemctl >/dev/null || die "systemd is required (or use docker compose)"
fi

# --- Config -----------------------------------------------------------------
ask() {  # ask VAR "Prompt" default  → sets VAR, prompting only on a terminal
  local reply
  if [[ -t 0 ]]; then
    read -r -p "$2 [$3]: " reply
    printf -v "$1" '%s' "${reply:-$3}"
  else
    printf -v "$1" '%s' "$3"
  fi
}

mkdir -p "$CONF_DIR"
if [[ -f $ENV_FILE ]]; then
  say "Keeping existing config $ENV_FILE"
else
  say "Creating config $ENV_FILE"
  ask HOST_ "Router SSH host or ~/.ssh/config alias" "ssh.mikrotik"
  ask USER_ "SSH user (blank = from ssh config)" ""
  ask PASS_ "Dashboard password (blank = no login)" ""
  ask BIND_ "Listen address (0.0.0.0 = whole LAN)" "$([[ -n $PASS_ ]] && echo 0.0.0.0 || echo 127.0.0.1)"
  [[ -z $PASS_ && $BIND_ != 127.0.0.1 ]] && warn "listening on $BIND_ with no password: anyone on the network can block clients"
  umask 077
  cat > "$ENV_FILE" <<EOF
# mikrotik-dash settings; restart after editing:
#   systemctl --user restart mikrotik-dash
MIKROTIK_HOST=$HOST_
MIKROTIK_USER=$USER_
#MIKROTIK_PORT=22
#SSH_KEY=$HOME/.ssh/id_rsa
LAN_IFACE=bridge
LAN_CIDR=192.168.88.0/24
ROUTER_IP=192.168.88.1
#STATIC_NAMES='{"192.168.88.2": "Orange Pi"}'
BIND=$BIND_
PORT=8088
DASH_PASSWORD=$PASS_
EOF
  umask 022
fi

# --- App files --------------------------------------------------------------
say "Installing app to $APP_DIR"
mkdir -p "$APP_DIR/data"
install -m 644 "$SRC/server.py" "$SRC/index.html" "$APP_DIR/"
# Carry over aliases from a source-checkout run, if any.
if [[ -f $SRC/aliases.json && ! -f $APP_DIR/data/aliases.json ]]; then
  cp "$SRC/aliases.json" "$APP_DIR/data/aliases.json"
fi

# --- Router check -----------------------------------------------------------
set -a; . "$ENV_FILE"; set +a
ssh_args=(-o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR)
[[ -n ${MIKROTIK_USER:-} ]] && ssh_args+=(-l "$MIKROTIK_USER")
[[ -n ${MIKROTIK_PORT:-} ]] && ssh_args+=(-p "$MIKROTIK_PORT")
[[ -n ${SSH_KEY:-} ]] && ssh_args+=(-i "$SSH_KEY" -o IdentitiesOnly=yes)
say "Checking SSH access to $MIKROTIK_HOST"
if ident=$(ssh "${ssh_args[@]}" "$MIKROTIK_HOST" ':put [/system identity get name]' 2>&1); then
  say "Router reachable: $ident"
else
  warn "could not run a command on the router over SSH: $ident"
  warn "the dashboard needs key-based SSH; fix it, then: systemctl --user restart mikrotik-dash"
fi

# --- Service ----------------------------------------------------------------
mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=MikroTik client speed dashboard
After=network-online.target
Wants=network-online.target

[Service]
Environment=DATA_DIR=$APP_DIR/data
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/env python3 $APP_DIR/server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

if (( START )); then
  say "Starting service"
  systemctl --user daemon-reload
  systemctl --user enable mikrotik-dash.service >/dev/null
  systemctl --user restart mikrotik-dash.service
  sleep 1
  systemctl --user is-active --quiet mikrotik-dash.service \
    || die "service failed to start; see: journalctl --user -u mikrotik-dash"
  host=${BIND:-127.0.0.1}; [[ $host == 0.0.0.0 ]] && host=$(hostname -I 2>/dev/null | awk '{print $1}')
  say "Running at http://${host:-127.0.0.1}:${PORT:-8088}"
  if [[ $(loginctl show-user "$USER" -p Linger --value 2>/dev/null) != yes ]]; then
    echo "    To keep it running after you log out / start at boot: loginctl enable-linger $USER"
  fi
else
  say "Installed (not started). Start with: systemctl --user daemon-reload && systemctl --user enable --now mikrotik-dash"
fi
echo "    Config: $ENV_FILE   Logs: journalctl --user -u mikrotik-dash -f"
