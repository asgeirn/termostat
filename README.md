# termostat

Automatic temperature control for an air to air heat pump via a Sensibo Sky. See [DESIGN.md](DESIGN.md).

Currently implements **phase 1** (logging) and **phase 2** (autonomous overnight step tests). The step test timer is the only part that writes to the heat pump: it changes the setpoint at night and restores it before morning.

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

- installs `termostat-log.{service,timer}` (every 5 minutes), `termostat-backfill.{service,timer}` (every 6 hours) and `termostat-steptest.{service,timer}` (every 5 minutes) in `~/.config/systemd/user`,
- installs `termostat-configure`, `termostat-log`, `termostat-backfill`, `termostat-report` and `termostat-steptest` wrappers in `~/.local/bin` (add it to `PATH`, or set `BIN_DIR`); they load `~/.config/termostat/env` automatically,
- once the environment file exists, backfills the last 24 hours of Sensibo measurements and enables the timers.

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
| `SHELLY_HOST` | no | IP address or host name of the Shelly power meter (Plug PM Gen3) on the heat pump's supply, or an `https://` URL; power logging is off when unset |
| `SHELLY_PASSWORD` | no | The plug's password, if set (user name is always `admin`) |
| `SHELLY_CA_FILE` | no | CA certificate (PEM) to trust for an `https://` `SHELLY_HOST` with a private certificate, e.g. `~/.config/termostat/rootCA.pem` |

The file uses `KEY=value` lines, which work both as a systemd `EnvironmentFile` and in a shell (`set -a; . ~/.config/termostat/env; set +a`). Keep it private, since it contains the API key.

## Logging

```sh
termostat-log
```

Run by `termostat-log.timer` every 5 minutes (scheduled in UTC, so the timer does not skip the repeated hour when daylight saving time ends). Each run stores one row in `readings` (indoor measurements, AC state including fan level, and outdoor conditions interpolated from the current forecast), new entries from the Sensibo AC state history in `ac_state_changes`, and a new forecast in `forecasts`/`forecast_steps` when the cached one has expired. When `SHELLY_HOST` is set, it also stores the heat pump's power, voltage, current and energy counter from the Shelly meter in `power_readings`. `termostat-configure` shows the meter's model and current power when `SHELLY_HOST` is set. The exit code is non-zero if any part failed, so the failure shows up in `systemctl --user status termostat-log`.

## Backfill

```sh
termostat-backfill
```

Fetches Sensibo's stored indoor measurements (the last 24 hours, which is what the account provides) into `historical_measurements`. Sensibo measures every 90 seconds, while the logger samples every 5 minutes, so the backfill gives about 3.3 times the resolution. Run by the installer to cover the period before logging starts, and then every 6 hours by `termostat-backfill.timer`. Safe to rerun; `--days N` fetches more if your account has a longer history.

Sensibo only provides temperature and humidity here; AC state history is limited to the last 20 changes (logged by `termostat-log`), and past outdoor conditions are not available from Locationforecast.

## Step tests

```sh
termostat-steptest check      # what would be decided for the next night (writes nothing)
termostat-steptest status     # recorded nights, decisions and fitted models
termostat-steptest analyze 2026-11-03   # refit a test from the stored data
```

`termostat-steptest.timer` runs `termostat-steptest run` every 5 minutes. Outside the night window, with no test active, it only reads the database.

- **Decision** (first run between 00:00 and 00:30 local time): one row per night in `step_tests`, with the decision, the reasons for skipping and every value it was based on (`checks`, JSON). The test runs only if all of these hold: a test kind is still wanted; the device is connected with a fresh measurement; the heat pump is on in heat mode with a fixed fan level; the step setpoint is within 16–24; at least 9 readings in the last hour, varying at most 0.4 °C; no AC state change in the last 3 hours; and the latest forecast (at most 3 hours old) covers 00:00–05:30, with outdoor temperature varying at most 3 °C and wind at most 8 m/s.
- **Test kinds**: `heat` (setpoint +2) first, then `cool` (setpoint −2), each once. A test that ends at a safety limit also counts. Change `runs_per_kind` in `steptest.Settings` to repeat them, for example in colder weather.
- **During the test**: if the room passes 23.0 °C (heat) or falls below 19.0 °C (cool), the setpoint is restored early (`end_reason = 'limit'`). If the AC state differs from what the test set, someone used the app: the test is aborted and their change is left in place (`override`). The IR remote cannot be detected, so don't use it at night.
- **Restore** at 05:30 local time to the setpoint recorded at the start. Failures are retried every 5 minutes (`restore_failed` events) until it succeeds.
- **Analysis**: after the restore, the last 24 hours of 90 s measurements are fetched and a first order plus dead time model is fitted (`result`, JSON: change from the pre-step level, gain per setpoint degree, time constant, dead time, RMSE, outdoor mean). With a 5.5 hour test and a time constant around 3 hours, the final value is an extrapolation.

All writes and their API responses are in `step_test_events`, so they can be matched against `ac_state_changes` (to see which `reason` Sensibo gives our own writes). Stop the tests with `systemctl --user disable --now termostat-steptest.timer`.

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
