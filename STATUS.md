# Status

_Last updated: 2026-10-28 (first review, 4.9 days of logging). Next review: around 2026-11-04, or after the first cold spell, whichever comes first._

## Where we are

**Phase 1 (logger only) is running** on the NixOS host as a systemd user timer. Nothing is written to the heat pump.

| Component | State |
|---|---|
| `termostat-log.timer` | Installed and enabled, every 5 minutes. Changed to a UTC calendar spec (see below) |
| `termostat-backfill.timer` | New, every 6 hours |
| Deployment | Deployed to the NixOS host at the commit time of the first review changes (`git log --before=2026-10-28T18:03Z -1`), 2026-10-28 18:02:22 UTC (19:02:22 local). Confirm with `systemctl --user list-timers` that both timers are active |
| Install-time backfill | Ran `termostat-backfill --days 1` (24 hours is the account's history limit) |
| `termostat-report` | Available for reviewing history |
| Controller (phase 3) | Not started |

### Verified against the real APIs (review of 2026-10-28)

Database downloaded from the host (1384 readings, 2026-10-23 21:00 – 2026-10-28 17:20 UTC).

- **Reliability:** 1384 readings against about 1397 expected. Two gaps over 7 minutes: one missed run (10 min, 10-25 09:25 UTC) and the DST change (65 min, 10-25 00:55–02:00 UTC). The latter is most likely systemd evaluating `OnCalendar=*:0/5` in local time and skipping the repeated hour; this is an inference, not confirmed. The timer is now scheduled in UTC.
- **Field mapping:** no nulls in any `readings` column. `measured_at`, `is_alive` and the AC state fields are all populated correctly. The raw response has `measurements.time` as `{"time", "secondsAgo"}`, as assumed.
- **Measurement freshness:** age when logged is 0 / 48 / 90 s (min / mean / max). The same measurement was never logged twice.
- **Sensibo resolution:** `historical_measurements` has an interval of exactly 90 s. The logger only samples every 5 minutes, so it sees about 1 in 3.3 measurements. The backfill is now periodic.
- **Forecast caching works.** About 41 forecasts a day, not the 24 the original checklist expected. met.no's `Expires` is about 30 minutes after `Last-Modified`, so this is the correct rate (uncached would be 288 a day). 83–88 steps per forecast.
- **Database size:** 3.5 MB after 5 days, about 2.2 MB of it `forecast_steps` and its index. Roughly 250 MB a year, acceptable. Pruning old forecast steps is possible later.
- **AC state history:** `changed_at` and `reason` are populated. Reasons seen: `UserRequest` (the app), `ScheduledCommand` (Sensibo schedule) and `Tibber` (price integration, with empty `changed_properties`).
- **IR remote commands are invisible to Sensibo** (confirmed by the household). They appear neither in the AC history nor in `acState`, which keeps showing the last state Sensibo sent. The remote is not used much, but it was used on 10-25 (playing around) and 10-26 (service), so the data around those times is disturbed in ways the history cannot show (see below).

### Still unverified

- Which `reason` the controller's own API writes will get (probably `UserRequest`). Needed to tell the controller's writes from manual changes in the history. Test during the step test.
- Whether the `OnCalendar=... UTC` spec behaves as expected on the host (check `systemctl --user list-timers` after reinstalling). Next DST change is 2027-03-28.
- Whether the timer gap at the DST change really was the timer (see above).

## Findings from the first review

Times are UTC. Norway was on UTC+2 until 2026-10-25 01:00 and UTC+1 after.

### Baseline

| Setpoint / fan | Readings | Mean indoor | Range | Std dev | Mean offset (indoor – setpoint) | Mean outdoor |
|---|---|---|---|---|---|---|
| 18 / low | 1055 | 19.83 | 19.2 – 21.2 | 0.53 | +1.83 | 8.2 |
| 20 / medium_low | 301 | 21.17 | 20.6 – 21.9 | 0.38 | +1.17 | 9.0 |
| 21 / low | 27 | 20.54 | 19.9 – 20.8 | 0.21 | –0.46 | 12.4 |

Overall: indoor mean 20.00, range 19.2 – 21.9, std dev 0.65. Outdoor range 5.0 – 12.7 °C (mean 8.4), which is warm for the season and **not representative of winter**.

Only the stretch 10-23 21:00 to 10-25 07:52 (about 35 hours, setpoint 18, fan low) is free of manual changes.

### Observations

- **The pump held a flat floor.** At setpoint 18 / fan low, the nightly minimum was 19.3 °C on every night (19.3, 19.32, 19.35, 19.28, 19.28), although outdoor temperature varied. Hypothesis: the unit holds the room at the setpoint as seen by its own sensor, which corresponds to about 19.3 on the Sky (offset about +1.3), and idles above that. Needs power data or the step test to confirm.
- **Above the floor, the temperature follows other heat sources.** The afternoon maximum is about 20.2 on average. On 10-26 the sky was clear (cloud 0–30%) and the room reached 21.2 at setpoint 18. If the pump is idle above its floor, a heat-only controller can raise the floor but not remove overshoot. Its useful actions are to avoid heating ahead of solar gain and to lift the floor when needed. Forecast cloud cover may matter as an input.
- **Setpoint 21 / fan low plateaued at 20.6** for about 1.5 hours (10-27 14:40–16:10), less than a "floor = setpoint + 1.3" model predicts. Either it had not settled or the pump was at its capacity limit. The step test should show which.
- **Heat pump service on 10-26.** The unit was serviced and cleaned, with the remote used (invisible to Sensibo). The AC history shows the swing set to `stopped` at 13:22:57 and back to `rangeFull` at 14:29:49 UTC, after an off/on at 13:22. The indoor temperature stayed flat at 21.0–21.2 throughout, and humidity rose from 71 to 77% between about 12:50 and 14:10 UTC. These times do not line up exactly with 13:30–14:30 if that was local time (UTC+1, i.e. 12:30–13:30 UTC), so please confirm. Treat about 12:30–14:30 UTC on 10-26 as disturbed, and note that the pump was cleaned: its behaviour before and after may differ slightly (airflow, efficiency).
- **The 10-25 morning rise was most likely remote use.** The Sky rose from 19.4 (07:50) to 20.8 (08:40 UTC), about 2 °C/h at first, while Sensibo showed setpoint 18 and fan low. The remote was used then ("played with the remote a bit"), and the app off/on at 08:43 followed by 17 → 18 at 08:49 looks like resetting the state afterwards. The remote's setpoint and fan level are unknown, so this is **not** usable as a step response, but it does show that the unit gives about 2 °C/h when asked for more, and the temperature then decayed from 20.8 to 20.1 within 40 minutes of the reset. Treat 10-25 07:50–09:30 UTC as disturbed.
- **Comfort data point.** On 10-27 at 13:57 UTC the Sky read 19.8–19.9 °C (outdoor 12.6 °C) and the room felt too cold when someone came home, which prompted the setpoint to go from 18 to 21. So about 19.8 is below the comfort limit, at least in the afternoon.
- **Possibly the fan level matters more than the setpoint, but this is confounded.** At 21 / low the Sky plateaued at 20.6 for 1.5 hours. After the setpoint was lowered to 20 and the fan raised to medium_low (10-27 16:16), the Sky rose to 21.2–21.4 by evening, with outdoor temperature falling from 12.4 to 9.8. If the unit idled above a setpoint-determined floor, lowering the setpoint should not have heated the room more, which weakens the "floor = setpoint + 1.3" hypothesis. However, humidity rose to 84% at about 18:00 UTC, which suggests cooking, so the evening rise may be partly other heat. Needs the step test, ideally with power data. Was there cooking that evening?
- **The previous automation is off.** Before logging started, a Sensibo schedule moved the setpoint between 17, 18 and 19, and Tibber events appeared. The last of both was on 10-23 at about 17:40. Nothing since. If they were not turned off deliberately, check Sensibo, because they would conflict with the controller.
- **Setpoint 20 and medium_low since 10-27 16:16.** The Sky has read 20.6 – 21.9 since (21.5 at 17:20 on 10-28). Is about 21 the comfort target?

## Review checklist

Run on the NixOS host, or against a downloaded copy: `sqlite3` works on `./termostat.db`, and `TERMOSTAT_DB=termostat.db termostat-report ...` for the report. `sqlite3` is not installed by default on NixOS; use `nix shell nixpkgs#sqlite`.

```sh
DB=~/.local/share/termostat/termostat.db
```

### 1. Is the timer running reliably?

```sh
systemctl --user list-timers termostat-log.timer termostat-backfill.timer
systemctl --user status termostat-log.service termostat-backfill.service
journalctl --user -u termostat-log --since "2 days ago" | grep -c "Failed with result"
journalctl --user -u termostat-log --since "2 days ago" | grep "termostat-log:" | sort | uniq -c | sort -rn | head
```

Expect: about 288 runs a day, few or no failures. Recurring warnings point at a specific failing step (forecast, device reading or AC state history).

### 2. Are there gaps?

```sql
-- Readings per hour (expect 12)
SELECT substr(logged_at, 1, 13) AS hour, COUNT(*) FROM readings GROUP BY hour ORDER BY hour;

-- Gaps longer than 7 minutes
SELECT prev, logged_at, ROUND((julianday(logged_at) - julianday(prev)) * 1440) AS gap_min
FROM (SELECT logged_at, LAG(logged_at) OVER (ORDER BY logged_at) AS prev FROM readings)
WHERE gap_min > 7;
```

Gaps may come from the host sleeping, lingering not being enabled, network outages, API rate limiting or the DST change (before the timer was moved to UTC).

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

Expect a maximum age of 90 s (Sensibo's measurement interval) and a mean around 45 s. Larger ages matter for the controller's stale-data safeguard.

### 5. Is the forecast caching working?

```sql
SELECT substr(fetched_at, 1, 10) AS day, COUNT(*) FROM forecasts GROUP BY day;
SELECT * FROM metno_cache;
SELECT COUNT(*) FROM forecast_steps;
```

Expect about 40 forecasts a day (met.no's `Expires` is about 30 minutes after `Last-Modified`). Close to 288 means caching is not working and must be fixed, both out of respect for the met.no terms and database growth.

### 6. AC state history

```sql
SELECT reason, COUNT(*) FROM ac_state_changes GROUP BY reason;
SELECT changed_at, reason, changed_properties, ac_on, mode, target_temperature, fan_level
FROM ac_state_changes ORDER BY changed_at DESC LIMIT 10;
```

Check that no `ScheduledCommand` or `Tibber` rows appear after 2026-10-23 (they would conflict with the controller).

### 7. Backfill

```sql
SELECT COUNT(*), MIN(time), MAX(time) FROM historical_measurements;

-- Gaps in the backfilled data (expect only the pre-timer gap until the periodic backfill has run)
SELECT prev, time, ROUND((julianday(time) - julianday(prev)) * 1440) AS gap_min
FROM (SELECT time, LAG(time) OVER (ORDER BY time) AS prev FROM historical_measurements)
WHERE gap_min > 3;
```

Until the periodic backfill is installed, `historical_measurements` covers only 2026-10-22 00:00 to 2026-10-23 20:54 UTC, so the 90 s data stops where the 5-minute logging starts.

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
- Day/night patterns: solar gain (compare with cloud cover), cooking, occupancy.

## Manual observations

Fill in during the logging period. Events not visible in the data will otherwise be confused with heat pump behaviour.

### Events in the AC history to explain

Taken from `ac_state_changes`, all `UserRequest` (so app changes; the remote is invisible to Sensibo). Please add what happened.

| Time (UTC) | Change | What happened? |
|---|---|---|
| 10-25 07:52 | setpoint 19, then 18 (3 s apart) | (Not commented. Part of the same remote session?) |
| 10-25 08:43 | off, then on 11 s later; 08:49 setpoint 17, then 18 | Played with the remote a bit. (So the Sky rise from 07:50 is probably the remote; the app events look like a state reset. Please confirm roughly when you started with the remote, and what you set.) |
| 10-26 13:22 | off, then on 4 s later; swing `stopped` at 13:22, `rangeFull` again at 14:29 | Heat pump service (remote used). The appointment was at 13:30 UTC+1 and lasted about one hour.  |
| 10-27 13:57–14:02 | off/on twice, setpoint 18 → 19 → 20 → 21 | Wife returned, felt the room was too cold. (Sky read 19.9 °C.) |
| 10-27 16:16 | fan low → medium_low, setpoint 21 → 20 | I lowered the temp one degree and increased fan instead. |

### Other events (window open, wood stove, guests, away, ...)

| Time | Event |
|---|---|
| | |

### Reference thermometer readings (Sky placement check)

| Time | Sky (from report) | Reference, room centre | Other room | Fan level | Notes |
|---|---|---|---|---|---|
| | | | | | |

## Decisions to make at the next review

1. **Target temperature and comfort band.** What temperature do you want (is it about 21?), and what deviation is acceptable (e.g. ±0.3 °C)? Different for night?
2. **Fan level.** Which fixed level to use for the step test and phase 3 (low or medium_low; the model is only valid for one).
3. **Sensor placement.** Based on reference readings: is the Sky representative, and how much of the +1.3 offset is sensor placement?
4. **Length of phase 1.** The outdoor range so far (5–13 °C) is too warm. Continue until it includes a cold period, or proceed with a step test now and repeat it in the cold.
5. **Step test plan (phase 2).** Pick a night with stable weather and nobody touching the heat pump: fixed fan level, setpoint +2 °C, hold 6–12 hours or until settled, then back. Also use the test to answer: does the unit idle above its floor, and does the plateau follow the setpoint one for one? Optionally a fan level step on another night. Needs a small command to set the state at a scheduled time, or can be done manually in the Sensibo app with the time noted here.
6. **Power metering.** This is now the most useful additional input: it would show compressor activity directly and turn the floor hypothesis into a fact (HAN port / smart plug).
7. **Other inputs.** Local outdoor sensor, electricity prices (Tibber was in use before), forecast cloud cover as a solar gain feed-forward.
8. **Existing automation.** Confirm the Sensibo schedule and Tibber integration are deliberately off.

## Known limitations

- Report averages weight samples equally, not by time; this only matters around gaps.
- Outdoor conditions are forecast values interpolated to the current time, not observations. MET Norway Frost could provide observations from a nearby station if forecast error turns out to matter.
- Sensibo only keeps 24 hours of history for this account, so data is lost if the logger and the backfill are both down for more than a day. The AC state history is limited to the last 20 changes.
- IR remote commands are not recorded by Sensibo, so they cannot be detected from the AC history or `acState`. Rare in this household, but the state shown for the unit may then be wrong. Known cases: 10-25 morning and the 10-26 service. Note these in the manual observations; the step test must be done without touching the remote.

## Decision log

| Date | Decision |
|---|---|
| 2026-10-23 | Phase 1 logger deployed as systemd user timer on NixOS; backfill once at install only |
| 2026-10-28 | First review: logger and field mapping verified, no fixes to the logging code needed |
| 2026-10-28 | Backfill moved to a timer every 6 hours (`--days 1`), to get Sensibo's 90 s resolution |
| 2026-10-28 | Logger timer changed to a UTC calendar spec, to avoid skipping the repeated hour at the end of DST |
| 2026-10-28 | Continue phase 1 into colder weather; step test to be planned after the next review |
| 2026-10-28 | Override detection in the design only covers app changes; IR remote use is accepted as undetectable (see DESIGN.md, robustness) |
| 2026-10-28 | Install the backfill timer and keep logging as is for a few more days, without changing the setpoint, fan level or schedule |
| 2026-10-28 18:02:22 UTC | Deployed the backfill timer and the UTC logger timer to the NixOS host (time of the commit) |
| | |
