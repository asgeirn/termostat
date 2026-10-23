# Status

_Last updated: 2026-10-23. Next review: around 2026-10-25, after ~48 hours of logging._

## Where we are

**Phase 1 (logger only) is running** on the NixOS host as a systemd user timer. Nothing is written to the heat pump.

| Component | State |
|---|---|
| `termostat-log.timer` | Installed and enabled, every 5 minutes |
| Install-time backfill | Ran `termostat-backfill --days 1` (24 hours is the account's history limit) |
| `termostat-report` | Available for reviewing history |
| Controller (phase 3) | Not started |

### Verified so far

- Offline unit tests (20) pass: forecast parsing/interpolation/caching, storage, backfill, reporting.
- Installer tested locally with stubbed `systemctl`.

### Not yet verified against the real APIs

These parts are based on assumptions about undocumented response fields. Raw JSON is stored alongside, so nothing is lost if they are wrong, but they need checking:

- Sensibo measurement time format (`{"time", "secondsAgo"}` object or string) → `readings.measured_at`
- `connectionStatus.isAlive` → `readings.is_alive`
- AC state history fields `id`, `time`, `reason`, `changedProperties` → `ac_state_changes`
- `historicalMeasurements` response shape → `historical_measurements`
- MET Norway caching: `Expires` / `If-Modified-Since` should give roughly one new forecast per hour, not one per run

## Review checklist

Run on the NixOS host. `sqlite3` is not installed by default; use `nix shell nixpkgs#sqlite`.

```sh
DB=~/.local/share/termostat/termostat.db
```

### 1. Is the timer running reliably?

```sh
systemctl --user list-timers termostat-log.timer
systemctl --user status termostat-log.service
journalctl --user -u termostat-log --since "2 days ago" | grep -c "Failed with result"
journalctl --user -u termostat-log --since "2 days ago" | grep "termostat-log:" | sort | uniq -c | sort -rn | head
```

Expect: ~576 runs in 48 hours, few or no failures. Recurring warnings point at a specific failing step (forecast, device reading or AC state history).

### 2. Are there gaps?

```sql
-- Readings per hour (expect 12)
SELECT substr(logged_at, 1, 13) AS hour, COUNT(*) FROM readings GROUP BY hour ORDER BY hour;

-- Gaps longer than 7 minutes
SELECT prev, logged_at, ROUND((julianday(logged_at) - julianday(prev)) * 1440) AS gap_min
FROM (SELECT logged_at, LAG(logged_at) OVER (ORDER BY logged_at) AS prev FROM readings)
WHERE gap_min > 7;
```

Gaps may come from the host sleeping, lingering not being enabled, network outages or API rate limiting.

### 3. Are all fields populated?

```sql
SELECT COUNT(*) AS total,
       SUM(measured_at IS NULL) AS no_measured_at,
       SUM(temperature IS NULL) AS no_temp,
       SUM(humidity IS NULL) AS no_humidity,
       SUM(target_temperature IS NULL) AS no_setpoint,
       SUM(fan_level IS NULL) AS no_fan,
       SUM(ac_on IS NULL) AS no_on,
       SUM(is_alive IS NULL) AS no_alive,
       SUM(outdoor_temperature IS NULL) AS no_outdoor
FROM readings;
```

Expect zeros everywhere. If a column is consistently null, inspect the raw JSON and fix the field mapping:

```sql
SELECT raw_device FROM readings ORDER BY id DESC LIMIT 1;
```

### 4. How fresh are the measurements?

```sql
-- Age of the measurement when logged, in seconds
SELECT ROUND(MIN(age)), ROUND(AVG(age)), ROUND(MAX(age))
FROM (SELECT (julianday(logged_at) - julianday(measured_at)) * 86400 AS age
      FROM readings WHERE measured_at IS NOT NULL);

-- Same measurement logged repeatedly (sensor or connectivity stalls)
SELECT measured_at, COUNT(*) FROM readings GROUP BY measured_at HAVING COUNT(*) > 1 ORDER BY 2 DESC LIMIT 10;
```

Expect an average age around 45–90 seconds. Large ages matter for the controller's stale-data safeguard.

### 5. Is the forecast caching working?

```sql
SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at) FROM forecasts;

SELECT f.id, f.fetched_at, f.updated_at, COUNT(*) AS steps, MIN(s.time), MAX(s.time)
FROM forecasts f JOIN forecast_steps s ON s.forecast_id = f.id
GROUP BY f.id ORDER BY f.id DESC LIMIT 5;

SELECT * FROM metno_cache;

-- Database size check: forecast steps dominate
SELECT COUNT(*) FROM forecast_steps;
```

Expect roughly 48 forecasts in 48 hours (one per update). Close to 576 means caching is not working and must be fixed before running longer, both out of respect for the met.no terms and database growth.

### 6. AC state history

```sql
SELECT reason, COUNT(*) FROM ac_state_changes GROUP BY reason;
SELECT changed_at, reason, changed_properties, ac_on, mode, target_temperature, fan_level
FROM ac_state_changes ORDER BY changed_at DESC LIMIT 10;
```

Check that `changed_at` and `reason` are populated, and note which `reason` values appear for app changes vs. the IR remote. These are needed for manual override detection in phase 3.

### 7. Backfill and Sensibo resolution

```sql
SELECT COUNT(*), MIN(time), MAX(time) FROM historical_measurements;

-- Typical interval between Sensibo's stored measurements, in seconds
SELECT ROUND(AVG(dt)), MIN(dt), MAX(dt)
FROM (SELECT (julianday(time) - julianday(LAG(time) OVER (ORDER BY time))) * 86400 AS dt
      FROM historical_measurements);
```

If the resolution is finer than 5 minutes, consider a periodic backfill after all (it is idempotent), since it gives better data for the step test.

### 8. Overall picture

```sh
termostat-report --days 2 --changes
termostat-report --days 2 --interval 15m
termostat-report --days 2 --target 21.5    # replace with the temperature you actually want
```

Note down:

- Indoor temperature range and standard deviation with the current manual setpoint. This is the baseline the controller must beat.
- Offset between setpoint and measured indoor temperature, and whether it varies with outdoor temperature.
- How quickly indoor temperature reacts to setpoint changes, if any were made.
- Outdoor temperature range during the period (has it been cold enough to be representative?).
- Day/night patterns: solar gain, cooking, occupancy.

## Manual observations

Fill in during the logging period. Events not visible in the data will otherwise be confused with heat pump behaviour.

| Time | Event (window open, wood stove, guests, manual setpoint/fan change, away, ...) |
|---|---|
| | |

### Reference thermometer readings (Sky placement check)

| Time | Sky (from report) | Reference, room centre | Other room | Fan level | Notes |
|---|---|---|---|---|---|
| | | | | | |

## Decisions to make at the review

1. **Fixes needed?** Any field mapping, caching or reliability issues found above.
2. **Target temperature and comfort band.** What temperature do you want, and what deviation is acceptable (e.g. ±0.3 °C)? Different for night?
3. **Fan level.** Which fixed level to use for the step test and phase 3.
4. **Sensor placement.** Based on reference readings: is the Sky representative, or is a room sensor needed?
5. **Length of phase 1.** Continue logging 1–2 weeks to cover a wider range of outdoor temperatures, or proceed sooner?
6. **Step test plan (phase 2).** Pick a night with stable weather and nobody touching the heat pump: fixed fan level, setpoint +2 °C, hold 6–12 hours or until settled, then back. Optionally a fan level step on another night. Needs a small command to set the state at a scheduled time, or can be done manually in the Sensibo app with the time noted here.
7. **Additional inputs.** Worth adding now: power metering (HAN port / smart plug), local outdoor sensor, electricity prices?

## Known limitations

- Report averages weight samples equally, not by time; this only matters around gaps.
- Outdoor conditions are forecast values interpolated to the current time, not observations. MET Norway Frost could provide observations from a nearby station if forecast error turns out to matter.
- Sensibo only keeps 24 hours of history for this account, so data is lost if the logger is down for more than a day.

## Decision log

| Date | Decision |
|---|---|
| 2026-10-23 | Phase 1 logger deployed as systemd user timer on NixOS; backfill once at install only |
| | |
