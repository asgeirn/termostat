# termostat

Automatic temperature control for an air to air heat pump via a Sensibo Sky. See [DESIGN.md](DESIGN.md).

Currently implements **phase 1**: logging only. Nothing is ever written to the heat pump.

## Installation (NixOS, systemd user timer)

Requires only Python 3.15 (no other dependencies). The installer builds `python315` from nixpkgs into `./.python`, which is a GC root, so nothing needs to be installed in your profile.

```sh
git clone <repo> ~/src/termostat && cd ~/src/termostat
systemd/install.sh
SENSIBO_API_KEY=... ~/.local/bin/termostat-configure > ~/.config/termostat/env   # key from https://home.sensibo.com/me/api
chmod 600 ~/.config/termostat/env
$EDITOR ~/.config/termostat/env    # check location, altitude and METNO_USER_AGENT
systemd/install.sh                 # rerun to enable the timers
```

The installer:

- installs `termostat-log.{service,timer}` (every 5 minutes) in `~/.config/systemd/user`,
- installs `termostat-configure`, `termostat-log`, `termostat-backfill` and `termostat-report` wrappers in `~/.local/bin` (add it to `PATH`, or set `BIN_DIR`); they load `~/.config/termostat/env` automatically,
- once the environment file exists, backfills the last 24 hours of Sensibo measurements and enables the timer.

User timers only run while you are logged in unless lingering is enabled. On NixOS:

```nix
users.users.<you>.linger = true;
```

Override the interpreter with `PYTHON=/path/to/python3.15 systemd/install.sh`, or the nixpkgs flake with `NIXPKGS=github:NixOS/nixpkgs/nixos-26.05`. After pulling updates no reinstall is needed, since the units run the code from the checkout; rerun the installer if the unit files change. Remove everything with `systemd/install.sh --uninstall`.

Useful commands:

```sh
systemctl --user list-timers termostat-log.timer
journalctl --user -u termostat-log -n 50              # logs and errors
```

## Configuration

`termostat-configure` lists your Sensibo devices on stderr (with current state, supported setpoint range and fan levels) and prints a suggested environment file on stdout:

| Variable | Required | Description |
|---|---|---|
| `SENSIBO_API_KEY` | yes | Sensibo API key |
| `SENSIBO_DEVICE_ID` | yes | Device to log, from `termostat-configure` |
| `TERMOSTAT_LATITUDE` | yes | Latitude for the weather forecast (max 4 decimals are used) |
| `TERMOSTAT_LONGITUDE` | yes | Longitude for the weather forecast |
| `TERMOSTAT_ALTITUDE` | no | Altitude in metres above sea level; improves forecast temperature |
| `METNO_USER_AGENT` | yes | Identifies the app to MET Norway, e.g. `termostat/0.1 you@example.com` ([terms of service](https://api.met.no/doc/TermsOfService)) |
| `TERMOSTAT_DB` | no | SQLite database path, default `~/.local/share/termostat/termostat.db` |

The file uses `KEY=value` lines, which work both as a systemd `EnvironmentFile` and in a shell (`set -a; . ~/.config/termostat/env; set +a`). Keep it private, since it contains the API key.

## Logging

```sh
termostat-log
```

Run by `termostat-log.timer` every 5 minutes. Each run stores one row in `readings` (indoor measurements, AC state including fan level, and outdoor conditions interpolated from the current forecast), new entries from the Sensibo AC state history in `ac_state_changes`, and a new forecast in `forecasts`/`forecast_steps` when the cached one has expired. The exit code is non-zero if any part failed, so the failure shows up in `systemctl --user status termostat-log`.

## Backfill

```sh
termostat-backfill
```

Fetches Sensibo's stored indoor measurements (the last 24 hours, which is what the account provides) into `historical_measurements`. Run automatically by the installer to cover the period before logging starts. Safe to rerun; `--days N` fetches more if your account has a longer history.

Sensibo only provides temperature and humidity here; AC state history is limited to the last 20 changes (logged by `termostat-log`), and past outdoor conditions are not available from Locationforecast.

## Reporting

```sh
termostat-report                      # last 24 hours, hourly
termostat-report --days 7             # last week, daily
termostat-report --since 2026-01-15 --until 2026-01-16 --interval 15m --changes
termostat-report --days 7 --target 21.5   # add error statistics against a target
termostat-report --days 30 --interval hour --format csv > history.csv
```

Shows a summary (indoor, humidity, outdoor, setpoint, fan level, AC state changes and, with `--target`, mean/RMS error and time within ±0.2/±0.5 °C), sparklines, and a table per interval. Intervals are `raw`, `15m`, `hour` or `day`; the default depends on the length of the period. Indoor values use Sensibo's historical measurements where backfilled, otherwise the logged readings. Times are local unless `--utc` is given. `--format csv` and `--format json` are intended for further analysis.

## Inspecting data

```sh
sqlite3 ~/.local/share/termostat/termostat.db \
  "SELECT logged_at, temperature, humidity, target_temperature, fan_level, outdoor_temperature
   FROM readings ORDER BY logged_at DESC LIMIT 12"
```

## Development

With uv (on NixOS e.g. `nix shell nixpkgs#uv nixpkgs#python315`, and `UV_PYTHON_DOWNLOADS=never` so uv uses the Nix Python):

```sh
uv run termostat-report
uv run python -m unittest discover -s tests
```
