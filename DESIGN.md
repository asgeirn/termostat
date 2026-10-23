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

The Sky sits at roughly head height, out of the direct airflow, which is a reasonable position. However, a floor-level console unit produces a warm plume rising along the wall it is mounted on, so the Sky may read high while the heat pump is working hard and close to room temperature while it is idling. This should be verified during the logging phase by comparing against a reference thermometer placed in the middle of the room (or in the rooms where comfort matters), both at high and low heat output. If the bias is significant, add a separate room sensor (e.g. Sensibo Room Sensor) as the control input, or compensate for it.

## Design

Python program run on a cron schedule, reading actual temperature and changing the setpoint to approach the target.

Use the Sensibo REST API defined in `sensibo.openapi.yaml`.

The API key is in the `SENSIBO_API_KEY` environment variable.

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

### State and logging

Cron runs are stateless, so persist controller state between runs in SQLite: integral term, last written setpoint, time of last change and override status.

Log everything from day one in the same database: indoor temperature and humidity, outdoor temperature, setpoint, AC state and every action taken. The `historicalMeasurements` endpoint only covers a limited number of days, and this data is needed for system identification and tuning.

Run every 5–10 minutes. The Sensibo Sky updates its measurements roughly every 90 seconds.

### Robustness and safety

- Manual override detection: if the current `acState` differs from what was last written, someone used the remote or app. Back off for a few hours and reset the integrator.
- Fail safe: if measurements are stale or the API fails, leave the setpoint unchanged. Never turn the unit off because of an error.
- Clamp the setpoint to the range supported by the unit, read from the device's `remoteCapabilities` rather than hard coded.
- Hard limits: if the room temperature falls below a safety threshold (e.g. 16 °C), override the controller and set a safe high setpoint.

## Phases

1. **Logger only.** Collect indoor, outdoor and setpoint data for 1–2 weeks without controlling anything. Verify the Sky placement against a reference thermometer.
2. **Step test.** On a stable night, raise the setpoint by 2 °C and record the response. Fit a first order plus dead time model and tune the PI controller from it (SIMC or lambda tuning).
3. **PI with outdoor feedforward**, including the safeguards above. Compare against Sensibo's built-in Climate React as a baseline.
4. **Optional: model predictive control** using the weather forecast and electricity spot prices, once a trustworthy model exists.
