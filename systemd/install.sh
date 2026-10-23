#!/bin/sh
# Install termostat as systemd user timers.
#
# Usage: systemd/install.sh [--uninstall]
#
# Environment:
#   PYTHON   Python interpreter to use. Default: python315 from nixpkgs, built into
#            ./.python (a GC root, so it survives nix-collect-garbage).
#   NIXPKGS  Flake reference for nixpkgs (default: nixpkgs, from the system registry).
#   BIN_DIR  Where to put command wrappers (default: ~/.local/bin).

set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
UNIT_DIR=$CONFIG_HOME/systemd/user
ENV_FILE=$CONFIG_HOME/termostat/env
BIN_DIR=${BIN_DIR:-$HOME/.local/bin}
UNITS="termostat-log.service termostat-log.timer"
TIMERS="termostat-log.timer"
COMMANDS="configure log backfill report"

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now $TIMERS 2>/dev/null || true
    for unit in $UNITS; do rm -f "$UNIT_DIR/$unit"; done
    for command in $COMMANDS; do rm -f "$BIN_DIR/termostat-$command"; done
    systemctl --user daemon-reload
    echo "Uninstalled. Left in place: $ENV_FILE, the database and $SRC/.python."
    exit 0
fi

if [ -z "${PYTHON:-}" ]; then
    echo "Building python315 from ${NIXPKGS:-nixpkgs} ..."
    nix --extra-experimental-features 'nix-command flakes' \
        build "${NIXPKGS:-nixpkgs}#python315" --out-link "$SRC/.python"
    PYTHON=$SRC/.python/bin/python3
fi
echo "Using $("$PYTHON" --version) at $PYTHON"

mkdir -p "$UNIT_DIR" "$BIN_DIR"

for unit in $UNITS; do
    sed -e "s|@SRC@|$SRC|g" -e "s|@PYTHON@|$PYTHON|g" -e "s|@ENV_FILE@|$ENV_FILE|g" \
        "$SRC/systemd/$unit" > "$UNIT_DIR/$unit"
done
echo "Installed units in $UNIT_DIR"

# Wrappers for interactive use. They load the environment file if it exists.
for command in $COMMANDS; do
    module=$command
    [ "$command" = "log" ] && module=logger
    cat > "$BIN_DIR/termostat-$command" <<EOF
#!/bin/sh
if [ -r "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
PYTHONPATH="$SRC/src" PYTHONDONTWRITEBYTECODE=1 exec "$PYTHON" -m termostat.$module "\$@"
EOF
    chmod 755 "$BIN_DIR/termostat-$command"
done
echo "Installed termostat-{$(echo $COMMANDS | tr ' ' ',')} in $BIN_DIR"

systemctl --user daemon-reload

if [ ! -r "$ENV_FILE" ]; then
    mkdir -p "$(dirname "$ENV_FILE")"
    cat <<EOF

No environment file found. Create it, then rerun this script to enable the timers:

    SENSIBO_API_KEY=... $BIN_DIR/termostat-configure > $ENV_FILE
    chmod 600 $ENV_FILE
    \$EDITOR $ENV_FILE    # check location, altitude and METNO_USER_AGENT
EOF
    exit 0
fi

chmod 600 "$ENV_FILE"

# Sensibo keeps 24 hours of history for this account, so a one-off backfill at install
# time covers the period before logging starts. Safe to repeat on reinstall.
echo "Backfilling the last 24 hours of Sensibo measurements ..."
"$BIN_DIR/termostat-backfill" --days 1 || echo "Warning: backfill failed; rerun with $BIN_DIR/termostat-backfill"

systemctl --user enable --now $TIMERS
echo "Enabled timers:"
systemctl --user list-timers $TIMERS --no-pager

if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" != "yes" ]; then
    cat <<EOF

Warning: lingering is not enabled for $USER, so the timers only run while you are logged in.
On NixOS, add this to your configuration and rebuild:

    users.users.$USER.linger = true;
EOF
fi
