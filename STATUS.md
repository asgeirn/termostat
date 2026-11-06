# Status

_Last updated: 2026-11-06 (third review: both step tests done, Shelly plug installed). Next review: after a few days of power data, or after the first real cold spell._

## Where we are

**Phase 1 (logger) is running** on the NixOS host. **Phase 2 (step tests) is being repeated:** both tests ran once (heat on 11-02, cool on 11-03); `runs_per_kind` raised to 2 on 2026-11-06, so a second heat test and then a second cool test run on the next suitable nights, now with 1-minute power data and the drift-corrected analysis. **Power metering is deployed** and logging on the host since 2026-11-06 06:20 UTC: every 5 minutes from `termostat-log` until 06:43 UTC, every minute from `termostat-power` since.

| Component | State |
|---|---|
| `termostat-log.timer` | Installed and enabled, every 5 minutes. Changed to a UTC calendar spec (see below) |
| `termostat-backfill.timer` | New, every 6 hours |
| Deployment | Commit `71d8f3f` (2026-10-28 18:02:22 UTC) was first pushed late, so the first install attempt failed. After the push and a rerun of `systemd/install.sh`, the unit files were installed on the host at **2026-10-28 19:09:14 CET (18:09:14 UTC)** (file mtimes), and both timers were confirmed active at 19:09:47 CET |
| Install-time backfill | Ran `termostat-backfill --days 1` (24 hours is the account's history limit) |
| `termostat-report` | Available for reviewing history |
| `termostat-steptest.timer` | Deployed 2026-11-01 (logger gap 22:00–22:20 UTC is the install). Both tests completed; idle since |
| Power metering | Shelly Plug PM Gen3 (S3PL-30116EU, firmware 2.1.1) installed 2026-11-06 ~05:57 UTC, HTTPS with a mkcert certificate, password set. Deployed on the host 2026-11-06 (commit `80d00e3`, `SHELLY_CA_FILE` set); first host reading 06:20:31 UTC, 459 W |
| `termostat-power.timer` | Deployed 2026-11-06 (commit `120cd01`), first run 07:43:00 CET (06:43 UTC). Every minute; took over power logging from `termostat-log` |
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

- Whether the UTC `OnCalendar` spec avoids the gap at the next DST change. The gap seen in 2026 was at the end of DST, so the real test is 2027-10-31 (the start of DST is 2027-03-28). Both specs were accepted by systemd on the host and scheduled as expected: the logger at 5-minute marks, the backfill at 18:20 UTC plus a random delay (next run 19:20:52 CET).
- Whether the timer gap at the DST change really was the timer (see above).

## Deploy to the host

The host needs commit `db21065` (Shelly logging) plus the HTTPS support from this review (not yet committed), and the CA certificate:

```sh
cd <repo on host> && git pull && systemd/install.sh
cp rootCA.pem ~/.config/termostat/rootCA.pem      # from the Mac; only the CA, never the *-key.pem files
echo 'SHELLY_CA_FILE=~/.config/termostat/rootCA.pem' >> ~/.config/termostat/env
termostat-configure > /dev/null                   # should list the plug and its power
termostat-log && sqlite3 $DB "SELECT * FROM power_readings ORDER BY id DESC LIMIT 1"
```

`SHELLY_HOST=https://192.168.10.191/` and `SHELLY_PASSWORD` are already set. Without `SHELLY_CA_FILE` the HTTPS call fails certificate verification on the host. (It works on the Mac without it only because Homebrew's CA bundle imports the mkcert root from the keychain.) `*.pem` is now in `.gitignore`.

## Findings from the third review (2026-11-06)

Database downloaded 2026-11-06 06:01 UTC.

### Logging health

- 288 readings every full day 11-02 to 11-05, no nulls. Gaps: 11-01 03:40 (10 min, missed run) and 11-01 22:00–22:20 (steptest install). 90 s backfill gap 11-01 22:03–22:16, same cause.
- 39–42 forecasts a day. Database 10.4 MB.
- Colder now: outdoor −0.5 to 9.0 °C since 11-01, down to around 0 °C at night.

### Step tests

| | Heat (night 11-02) | Cool (night 11-03) |
|---|---|---|
| Setpoint | 20 → 22, 00:01–05:31 local | 20 → 18, 00:01–05:31 local |
| Outdoor (mean) | 5.5 °C, cloud 3 % | 1.3 °C, cloud 31 % |
| Room start → end | 21.06 → 21.4 (max 21.5) | 21.1 → 19.39 (min 19.3) |
| Fit as stored at the time | Δ∞ +0.49, τ 1.8 h, gain 0.24, RMSE 0.055 | Δ∞ −1.82, τ 2.65 h, gain 0.91, RMSE 0.062 |
| Pre-step slope, 1 h / 2 h / 3 h | −0.39 / +0.07 / −0.02 °C/h | −0.04 / −0.14 / −0.15 °C/h |
| Fit corrected for 2 h drift (refit 2026-11-06) | Δ∞ +0.36, gain 0.18 | Δ∞ −1.44, gain 0.72 |

- **Both ran as designed.** No limit or override aborts, restore on time, everything recorded. **Our API writes get reason `UserAPI`** in the AC history, separate from the app's `UserRequest`. That answers the open question: the controller can tell its own writes from manual changes.
- **The cool test's gain is biased by drift; the heat test's is not** (corrected 2026-11-06; this review first said both were). The heat test's −0.39 °C/h covers only the last hour and is a wiggle at the sensor's 0.1 °C resolution. Over 2–3 hours the room was flat at about 21.05, near the setpoint-20 level for that outdoor temperature (20.8 on 10-28 and 11-01 at 6.5–7 °C). The cool test started while the room was still cooling after the evening at −0.14 °C/h (21.4 at 19:00 to 21.0 at 23:00 UTC), so the uncorrected fit credited about 0.4 °C of that to the step. With the 2-hour drift correction now in `steptest.analyze`, the gains are **0.18 (heat, 20 → 22) and 0.72 (cool, 20 → 18)**. The asymmetry is real: more setpoint gives little extra heat above 20, less setpoint cools the room a lot. A power reading from a repeat heat test would show whether the pump is at its capacity at setpoint 22 with fan medium_low. The overnight plateaus (02–05 UTC, fan medium_low) agree:

  | Setpoint | Overnight plateau | Outdoor | Source |
  |---|---|---|---|
  | 18 | 19.3–19.5 (fit T∞ 19.2) | 1 °C | cool test |
  | 19 | 20.0–20.4 | 5–8 °C | 10-29 to 10-31 |
  | 20 | 20.6 (20.8 at 6–7 °C) | 1–2 °C | 11-04, 11-05 |
  | 22 | 21.4–21.5 (fit T∞ 21.5) | 5 °C | heat test |

  The incremental gain falls as the setpoint rises: about 0.8 °C per setpoint degree from 18 to 19, 0.5 from 19 to 20, and 0.35 from 20 to 22 (comparing plateaus at similar outdoor temperatures; 0.18 from the corrected heat fit). The 10-31 daytime step (gain 0.83) does not fit this well, but it had cooking and daylight in it. The plateau at 20 barely moves with outdoor temperature (20.6 at 1 °C, 20.8 at 6.5 °C), so the unit regulates on its own sensor and the Sky offset is fairly stable for now.
- **Time constant 1.8–2.65 h, no dead time** (3.4 h on 10-31). Use about 2.5 h for the design.
- **Cooling is faster than heating near the unit.** After the heat test was restored, the room fell from 21.5 to 20.65 in one hour, much faster than τ. When the unit stops blowing warm air, the Sky drops quickly. There is likely a fast component (air at the sensor) on top of the slow one (thermal mass). Power data will show whether the compressor stopped.
- **Comfort cost of the cool test:** below 20.0 from about 02:00 to 08:00 local on 11-03. The room had recovered to 20.0 about 2.5 h after the 05:31 restore (recovery is slower than the drop).

### What this means for the controller (phase 3)

- Setpoint resolution limits precision. Integer °C only (Fahrenheit is not native on this unit and maps one-to-one to the °C values). At night, setpoint 19 gives about 20.0–20.1 (too close to the minimum; the household raised it twice from 19.7–19.9), and 20 gives about 20.6. To hold about 20.2–20.3, the controller will have to **time-proportion between two adjacent setpoints** (e.g. 19 and 20, with a period well under τ, say 30–60 min), with the duty cycle set by an integral term on the Sky temperature.
- The nonlinear gain (smaller at higher setpoints) and the fast drop when the unit idles mean a plain linear PI with a fixed gain will be detuned somewhere. Schedule the gain on setpoint, or keep the setpoint within a narrow band (19–21) where it is roughly linear.

### Power (first observations, 2026-11-06)

- **The pump was off in Sensibo from 05:49 to about 06:09 UTC** (forgotten after plugging in through the Shelly, the 05:22/05:30/05:49 `UserRequest` off/on events), so the 05:50–06:05 readings are with the pump off.
- **Off draw is about 83 W**, steady. This is probably the outdoor unit's crankcase heater (outdoor about −0.5 °C), which is roughly 2 kWh a day whenever the pump is off in the cold.
- **Compressor stop at 07:20–07:35 local:** power fell from 459 W to the off level (~84 W) and restarted around 07:35 (305 W), while the room dipped 20.3 → 20.1. It was foggy at −0.6 °C, so this may be a defrost, or the unit idling on its own sensor. 5-minute samples cannot tell them apart; hence the 1-minute timer. To check: look at the units during a dip (steam from the outdoor unit, indoor fan stopped), and see whether dips recur at regular intervals regardless of room temperature (defrost) or follow the room temperature (idling).
- **Start-up:** after it was switched on, the power ramped 84 → 174 → 352 → 603 → 680 W over about a minute, then eased to about 350 W within the next two minutes.
- **`aenergy.by_minute[0]` is the last complete minute**, i.e. the energy in `[minute_ts − 60, minute_ts)` in mWh. `minute_ts` is the start of the current, partial minute. Checked against `aenergy.total`: 1.489 Wh between 06:08:07 and 06:09:05 against `by_minute[0]` = 1488.3 mWh. The counter moves in steps of 0.2126 Wh, so `by_minute` values are multiples of that (1275.7 = 6 steps, 1488.3 = 7 steps), giving about ±13 W resolution per minute. Over 5 minutes, `aenergy_total` differences are better than ±1 W at typical heating power.

## Findings from the second review (2026-11-01)

Database downloaded 2026-11-01 (2446 readings up to 09:55 UTC).

### Logging health

- 288 readings every full day since 10-26. One missed run (10 min, 11-01 03:40 UTC). No nulls; measurement age 6 / 46 / 90 s.
- The backfill timer works: 90 s data has been continuous since 10-27 00:00, at 960 points a day.
- 41–42 forecasts a day. Database 6.5 MB, about 260 MB a year.
- No `ScheduledCommand` or `Tibber` rows since 10-23.

### Model

- **The "floor = setpoint + 1.3" hypothesis does not hold.** At 19 / medium_low (10-28 20:05 to 10-31 08:03, undisturbed) there was no flat floor: the room drifted down all night, to 20.1, 19.8 and 19.7. The offset between the Sky and the setpoint shrinks as the setpoint rises: about +1.3 at 18 / low, +0.7–1.1 at 19 / medium_low, +0.6–1.0 at 20 / medium_low, and about +0.35 at 21 / medium_low (fitted). A setpoint degree gives less than a room degree, so the controller needs integral action on the Sky and cannot use a fixed offset.
- **The 10-31 change worked as a step test** (setpoint 19 → 21, medium_low, 100 % cloud, 6–7 °C outdoor, by the household because it was too cold). The room rose from 19.70 to 21.19 in 6.5 h. A first order fit gives **τ ≈ 3.4 h, Δ∞ ≈ +1.66 °C (gain ≈ 0.83 per setpoint degree), no measurable dead time, RMSE 0.05** (`steptest.analyze` on the 90 s data). When the setpoint went back to 20 at 14:32, the room fell 0.25 °C within 20 minutes, so the pump was actively heating.
- **Cooking**: +0.4–0.6 °C lasting hours, always with a 3–4 point humidity rise (10-29 18:30, 10-30 16:30, 10-31 15:30 UTC). Humidity can serve as a disturbance marker.
- **Sun**: about +0.8 °C on a clear late-October day (10-29, 20.2 → 21.0 at 12:30 UTC).
- **Comfort**: both times the room reached 19.7–19.9, the setpoint was raised (10-27, 10-31). The minimum comfort temperature is **20.0 °C**, and the target should be as close above it as the control precision allows.
- Still not winter: outdoor 4.2–12.7 °C over the whole period.

### Step tests (phase 2)

`termostat-steptest` runs the two step tests autonomously, heat (+2) first, then cool (−2), each once, from 00:00 to 05:30 local time, on nights with a stable forecast (outdoor spread ≤ 3 °C, wind ≤ 8 m/s), a settled room and no manual changes in the last 3 hours. Safety limits: restore early below 19.0 °C (cool) or above 23.0 °C (heat); app changes abort the test and are left in place. Decisions, writes, API responses and fitted models are stored in `step_tests` and `step_test_events`; see README. Run on the real data, the analysis reproduces the 10-31 fit above.

Expectations at the current baseline (20 / medium_low, room about 20.8): heat test to about 22.1 by 05:30 (20.8 + 1.66 × (1 − e^(−5.5/3.4))); cool test to about 19.5 if the response is symmetric (it may not be, since the pump idles), so the room is below 20.0 at 05:30 and needs roughly 1–1.5 h to recover. If that is unacceptable, move the end time earlier (`Settings.end_time`) for the cool test.

To review after the tests:

```sh
termostat-steptest status
sqlite3 $DB "SELECT night, decision, reasons, json_extract(checks, '$.forecast') FROM step_tests"
sqlite3 $DB "SELECT * FROM step_test_events ORDER BY id"
-- reason Sensibo gives our own writes:
sqlite3 $DB "SELECT changed_at, reason, target_temperature FROM ac_state_changes WHERE changed_at > '2026-11-01' ORDER BY changed_at"
```

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

## When the Shelly plug arrives

Steps 2–6 done 2026-11-06 from the Mac (see the third review); step 3 additionally needs `SHELLY_CA_FILE` (HTTPS). Step 1 checked: the plug is rated 3.6 kW, the heat pump draws at most 2.4 kW. Deployed to the host 2026-11-06. Remaining: step 7.

1. Check the plug's rated current against the heat pump's rating plate (maximum and start-up current) before plugging the heat pump in.
2. Shelly app or web UI: connect it to Wi-Fi, update the firmware, give it a DHCP reservation. Optionally set a password.
3. Add `SHELLY_HOST` (and `SHELLY_PASSWORD`) to `~/.config/termostat/env`. No reinstall needed.
4. `termostat-configure > /dev/null` should show the plug's model and plausible power. If it reports an RPC error, the device may not use the `PM1` component: check `http://<host>/rpc/Shelly.GetStatus` for the component name and adjust `shelly.py`.
5. `termostat-log`, then check the stored row: `sqlite3 $DB "SELECT * FROM power_readings ORDER BY id DESC LIMIT 1"`. Verify that `apower`, `aenergy_total` and `aenergy_by_minute` are populated, and whether `by_minute[0]` is the current partial minute or the last complete one (compare `aenergy_minute_ts` with `logged_at`).
6. Write unit tests for `shelly.py` from a real response saved from step 5.
7. After a day: the 5-minute mean power from `aenergy_total` differences against setpoint, indoor and outdoor temperature. Is the compressor cycling, and how fast?

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

Check that no `ScheduledCommand` or `Tibber` rows appear after 2026-10-23 (they would conflict with the controller). `UserAPI` rows are termostat's own writes (step tests, later the controller).

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
| 10-28 20:05 | setpoint 20 → 19 | (Not commented.) |
| 10-31 08:03 | off/on, setpoint 19 → 20 → 21 | Temperature too low (Sky read 19.7 °C). |
| 10-31 14:32 | setpoint 21 → 20 | (Not commented. Too warm?) |
| 11-06 05:22–05:49 | off, on, off | Shelly plug installed (plugged in twice); pump left off until about 06:09 |

### Other events (window open, wood stove, guests, away, ...)

| Time | Event |
|---|---|
| | |

### Reference thermometer readings (Sky placement check)

| Time | Sky (from report) | Reference, room centre | Other room | Fan level | Notes |
|---|---|---|---|---|---|
| | | | | | |

## Decisions to make at the next review

1. **Target temperature and comfort band.** Answered 2026-11-01: minimum 20.0 °C, target as close above it as possible. Still open: acceptable deviation, and whether the night may be cooler.
2. **Fan level.** medium_low for now (the household's choice; the step tests use whatever fixed level is set at midnight and record it).
3. **Sensor placement.** Based on reference readings: is the Sky representative, and how much of the +1.3 offset is sensor placement?
4. **Length of phase 1.** Decided 2026-11-01: step test now, repeat in the cold (raise `runs_per_kind`).
5. **Step test plan (phase 2).** Implemented as `termostat-steptest` (see above). Original plan: pick a night with stable weather and nobody touching the heat pump: fixed fan level, setpoint +2 °C, hold 6–12 hours or until settled, then back. Also use the test to answer: does the unit idle above its floor, and does the plateau follow the setpoint one for one? Optionally a fan level step on another night. Needs a small command to set the state at a scheduled time, or can be done manually in the Sensibo app with the time noted here.
6. **Power metering.** Decided 2026-11-01: Shelly Plug PM Gen3 ordered (see "When the Shelly plug arrives").
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
| 2026-10-28 19:09:14 CET (18:09:14 UTC) | Backfill timer and UTC logger timer deployed to the NixOS host (unit file mtimes) after the push was forgotten once; confirmed with `systemctl --user list-timers` at 19:09:47 CET (18:09:47 UTC): `termostat-log.timer` next 19:10:20 CET, last 19:05:12 CET; `termostat-backfill.timer` next 19:20:52 CET, not yet run |
| 2026-11-01 | Second review: logging healthy; floor hypothesis rejected; 10-31 accidental step gives τ ≈ 3.4 h, gain ≈ 0.83 |
| 2026-11-01 | Comfort minimum 20.0 °C, target as close above it as possible |
| 2026-11-01 | Shelly Plug PM Gen3 ordered for power metering; metering only (no relay); logging into `power_readings` written, testing deferred until delivery |
| 2026-11-01 | Step tests now rather than waiting for cold: autonomous `termostat-steptest`, heat +2 then cool −2, 00:00–05:30 local, gated on forecast and room stability, everything recorded in the database |
| 2026-11-06 | Third review: both step tests completed; τ ≈ 2–2.7 h, no dead time; gain falls with setpoint (≈ 0.8 → 0.45 per degree); own writes show as `UserAPI` |
| 2026-11-06 | Power logging deployed on the host (`80d00e3`); first `power_readings` row 06:20:31 UTC |
| 2026-11-06 | Repeat both step tests (`runs_per_kind` 2), with power metering. Cool test keeps the 05:30 restore: below 20.0 overnight is acceptable as long as the room is back around 08:00 |
| 2026-11-06 | Step test: refuse nights drifting more than 0.2 °C/h over 2 hours; fit corrects for the 2-hour pre-step drift (uncorrected fit kept). Refit gains: heat 0.18, cool 0.72 |
| 2026-11-06 | Power logging moved to its own 1-minute timer (`termostat-power`), to resolve compressor stops and defrosts |
| 2026-11-06 | Shelly plug installed, served over HTTPS with a mkcert certificate; `SHELLY_HOST` accepts `https://` URLs and `SHELLY_CA_FILE` names the CA to trust |
| | |
