# Automatic temperature control

## Goal

Keep a steady temperature inside my house by controlling the air to air heat pump connected to a Sensibo Sky.  The Sensibo Sky has its internal sensors.  It is mounted on the same wall as the heat pump, but not directly in the airflow.  The heat pump is mounted 50cm above floor level, and the Sensibo Sky unit 150cm above floor level, 1 meter to the side of the heat pump.

The sensor inputs are temperature in degrees Celsius with 0.1 degree resolution and relative humidity in percent with 0.1 percentage point resolution.

The control output is heat pump target temperature in 1 degree resolution.

Due to the behavior of the heat pump the setpoint temperature is not representative of actual temperature, and assumed not to be a constant offset but rather dependent on a number of variables I'm not entirely sure of.

The house is fairly well insulated, but the actual relationship between outside and inside temperature is to be determined.

The heat pump is the main source of heating in the house.

## Questions

### What other information would be useful to be able to accurately control the temperature?

| Input | Why | Possible source |
|---|---|---|
| Outdoor temperature | Largest disturbance; enables feedforward instead of waiting for the room to cool down | met.no Locationforecast / Frost API, or a local sensor |
| Weather forecast | The house reacts slowly, so heating can start *before* a cold snap | met.no Locationforecast |
| Solar radiation / cloud cover | Solar gain can noticeably heat a well insulated house | met.no, or time of day as a proxy |
| Wind | Increases infiltration losses | met.no |
| Heat pump power draw | Shows actual heat delivered, reveals defrost cycles and saturation | HAN port reader, smart plug, Tibber Pulse |
| Other heat sources | Wood stove, cooking, guests etc. will otherwise confuse the controller | Manual flag, or detected from sudden rises |
| Electricity spot price | Optional: shift heating to cheap hours | Nord Pool / hvakosterstrommen.no |
| Thermal model of the house | Gain, time constant and dead time are needed to tune the controller | Step test (see Phases) |

Humidity is not expected to drive control directly, but is logged and may later be used for a "feels like" adjustment.

### Is the Sensibo Sky placement representative?

Probably reasonably so. The indoor unit is a regular wall-mounted unit (mounted low, at 50cm) that draws return air in at the top and blows warm air forwards, away from the wall. The Sky sits at roughly head height beside and above the intake, out of the supply airflow, so it mostly sees mixed room air on its way back to the unit, which is a good proxy for room temperature.

Remaining sources of bias to check:

- Fan speed: with a high fan speed the room is well mixed and the reading should be representative; at low fan speed or when the unit idles, the room may stratify and readings at 150cm can differ from the occupied zone and from other rooms.
- Wall temperature: if the wall is an exterior wall, the Sky may read slightly low in cold weather due to the cooler wall surface.
- Representativeness for the whole house: the Sky measures the room with the heat pump, which is likely the warmest part of the house. Other rooms may lag, especially in cold weather.

Verify during the logging phase by comparing against a reference thermometer in the middle of the room and in other rooms where comfort matters, at different fan speeds and heat outputs. If the bias is significant, add a separate room sensor (e.g. Sensibo Room Sensor) as the control input, or compensate for it.

## Design

Python program run on a schedule (systemd user timer on NixOS), reading actual temperature and changing the setpoint (and possibly the fan level) to approach the target.

Use the Sensibo REST API defined in `sensibo.openapi.yaml`.

The API key is in the `SENSIBO_API_KEY` environment variable.

### Configuration

All configuration is through environment variables. `termostat-configure` lists the Sensibo devices for the API key and suggests values.

- `SENSIBO_API_KEY`: Sensibo API key.
- `SENSIBO_DEVICE_ID`: the device to read and control.
- `TERMOSTAT_LATITUDE`, `TERMOSTAT_LONGITUDE`: location of the house, used for the weather forecast (and later sun position).
- `TERMOSTAT_ALTITUDE` (optional): metres above sea level, improves forecast temperature.
- `METNO_USER_AGENT`: application name and contact information, required by MET Norway.
- `TERMOSTAT_DB` (optional): SQLite database path.

### Control strategy: cascade PI

The heat pump has its own internal thermostat (the inner loop). This program is the outer loop, adjusting the inner loop's setpoint until the measured room temperature matches the target. The unknown, variable offset between setpoint and actual temperature does not need to be modelled explicitly: the integral term learns it.

- Use PI, not PID. With 0.1 °C quantized measurements and slow dynamics, a derivative term mostly amplifies noise.
- Add feedforward from outdoor temperature (and later forecast) once a model of the house exists.
- Anti-windup: freeze the integrator when the setpoint is at its limits, when the heat pump is off or not in heat mode, during manual override, and during defrost if it can be detected.

### Handling the 1 °C output resolution

A naive PI will limit cycle between two adjacent setpoints, and frequent changes cause compressor ramping that reduces efficiency.

- Hysteresis on rounding: only move to a new integer setpoint when the computed value is past the midpoint by a margin (e.g. ±0.3 °C).
- Rate limit: at most one change per 30–60 minutes, and at most ±1 °C per change.
- Deadband: take no action while the error is within ±0.2 °C.

### Fan level

The fan is normally run at a constant, manually chosen level rather than `auto`. Auto fan lets the heat pump vary heat output and room mixing on its own, which adds an unobserved variable to the loop.

The fan level (`fanLevel` in `acState`) is always logged, since it affects:

- Heat delivered at a given setpoint.
- How well mixed the room is, and therefore how representative the Sky reading is.
- Noise and efficiency (higher fan speed generally gives better COP at the same heat output, at the cost of noise).

Initially (phases 1–3) the fan level is a fixed parameter: the controller keeps whatever level is configured and never selects `auto`. The fan level in use is recorded with the fitted model, since the model is only valid for that level; ideally the step test is repeated per fan level.

Later, consider the fan level as a second control output:

- Finer resolution: stepping the fan up or down one level may give a smaller change in heat output than a 1 °C setpoint step, helping with the quantization problem.
- Faster recovery: raise the fan level temporarily when the error is large (e.g. after a cold night or an open door), then return to the preferred level.
- Comfort constraints: a maximum fan level per time of day (e.g. quiet at night).

To avoid the two outputs fighting each other, use a clear priority rather than two independent loops. One option is mid-ranging: the fan handles small, fast corrections around a preferred level, and the setpoint is moved only when the fan has been away from its preferred level for a sustained period. Fan changes are subject to the same rate limiting and hysteresis as setpoint changes. The available fan levels are read from `remoteCapabilities`.

### Weather forecast

Outdoor conditions are fetched from the MET Norway Locationforecast API: `https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=..&lon=..&altitude=..`. The `complete` variant can be used if additional fields (e.g. dew point, fog, UV index) turn out to be useful.

Fields of interest per time step:

- `air_temperature`: feedforward input and system identification.
- `wind_speed`, `wind_from_direction`: infiltration losses.
- `cloud_area_fraction`: proxy for solar gain, combined with sun position computed from location and time (the API does not provide solar radiation).
- `relative_humidity`, `air_pressure_at_sea_level`: logged for completeness.
- Precipitation (`next_1_hours`): logged for completeness.

Terms of service that must be followed:

- Send an identifying `User-Agent` header with application name and contact information (e.g. `termostat/0.1 github.com/asgeirn/termostat`). Requests without it are rejected.
- Truncate coordinates to at most 4 decimals.
- Respect caching: store the `Expires` and `Last-Modified` response headers, do not request again before `Expires`, and use `If-Modified-Since` so unchanged forecasts return `304 Not Modified`. The forecast is typically updated about once per hour, so most cron runs will reuse the cached response.

Location (latitude, longitude, altitude) is configuration, not hard coded.

What to store:

- Each new forecast as received (issue time from `meta.updated_at` plus the time series), so the controller can later be evaluated with exactly the information it had at the time, and forecast accuracy can be measured.
- For each controller run, the forecast values for the current hour as the best available estimate of current outdoor conditions, until a local outdoor sensor is available.

If the forecast cannot be fetched, the logger and controller continue using the latest cached forecast while it still covers the current time, and otherwise run without outdoor data (no feedforward).

### State and logging

Cron runs are stateless, so persist controller state between runs in SQLite: integral term, last written setpoint and fan level, time of last change and override status.

Log everything from day one in the same database: indoor temperature and humidity, outdoor conditions and weather forecasts (see above), setpoint, fan level, mode, full AC state and every action taken. The `historicalMeasurements` endpoint only covers a limited number of days, and this data is needed for system identification and tuning.

Run every 5–10 minutes. The Sensibo Sky updates its measurements roughly every 90 seconds.

### Robustness and safety

- Manual override detection: if the current `acState` (setpoint, fan level, mode or on/off) differs from what was last written, someone used the remote or app. Back off for a few hours and reset the integrator.
- Fail safe: if measurements are stale or the API fails, leave the setpoint unchanged. Never turn the unit off because of an error.
- Clamp the setpoint and fan level to the ranges supported by the unit, read from the device's `remoteCapabilities` rather than hard coded. Never select `auto` fan unless explicitly configured.
- Hard limits: if the room temperature falls below a safety threshold (e.g. 16 °C), override the controller and set a safe high setpoint.

## Phases

1. **Logger only.** Collect indoor measurements, setpoint and fan level from Sensibo, plus weather forecasts from MET Norway Locationforecast, for 1–2 weeks without controlling anything. Verify the Sky placement against reference thermometers at different fan speeds and heat outputs.
2. **Step test.** On a stable night, with a fixed fan level, raise the setpoint by 2 °C and record the response. If fan control is to be considered, also do a fan level step at a fixed setpoint. Fit a first order plus dead time model and tune the PI controller from it (SIMC or lambda tuning).
3. **PI with outdoor feedforward**, setpoint only, fixed fan level, including the safeguards above. Compare against Sensibo's built-in Climate React as a baseline.
4. **Optional: fan level as a second output**, using mid-ranging as described above.
5. **Optional: model predictive control** using the weather forecast and electricity spot prices, once a trustworthy model exists.
