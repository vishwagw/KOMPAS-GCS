# KOMPAS

![KOMPAS flying in simulator mode with live camera HUD](docs/media/kompas-video-hud.gif)

**An open-source ground control station for commodity WiFi RC drones.**

📄 **Paper v1:*KOMPAS: An Open-Source, Safety-Gated Ground Control Station for Commodity WiFi RC Drones* [https://doi.org/10.5281/zenodo.24576399)

KOMPAS connects to a low-cost WiFi drone over UDP and gives you a desktop
ground station for it: flight control, live telemetry, a camera feed with a
HUD overlay, video recording, a pre-flight checklist that gates takeoff, and
automatic safety monitoring. A built-in flight simulator lets you run the
whole thing with no hardware.

The goal of the project is to turn cheap, closed RC vehicles into software-
controllable platforms that anyone can build autonomy on top of.

> **Status: early prototype.** KOMPAS works and is tested, but expect rough
> edges and breaking changes. Read the [Safety](#safety) section before
> flying a real aircraft.

## Features

- **WiFi control link**: UDP command, telemetry and video channels (Tello-SDK-style protocol)
- **Flight control**: takeoff, landing, movement and rotation from buttons or keyboard
- **Live telemetry**: battery, altitude, attitude, temperature and link health
- **Video + HUD**: camera feed with flight data overlay and recording
- **Pre-flight checklist**: takeoff stays locked until the aircraft passes
- **Safety monitor**: low-battery, flight-time and link-loss responses that land, never cut motors
- **Flight simulator**: the full stack against a simulated drone on your machine
- **Docker image**: run the GUI headless and reach it from a browser (noVNC)

## Supported hardware

KOMPAS speaks a Tello-SDK-style UDP protocol (commands on port 8889, state on
8890, video on 11111). It was developed against the **P8 Pro** WiFi drone.
Other drones that use the same protocol should work, possibly with config
changes. Reports of drones that do or don't work are very welcome. Please
[open an issue](../../issues).

## Quick start

Requires **Python 3.9+** with Tkinter (on Debian/Ubuntu: `sudo apt install python3-tk`).

```bash
git clone https://github.com/vishwagw/KOMPAS-autonomy.git
cd KOMPAS-autonomy
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Try it with no hardware first:
python main.py --sim

# Fly a real drone: join the drone's WiFi network, then
python main.py
```

Settings live in [`config/drone_config.yaml`](config/drone_config.yaml).
Every value is optional and falls back to a built-in default. The app refuses
to start on an invalid value and names the offending key.

### Docker

```bash
docker compose up --build
```

Then open <http://localhost:5800>. Logs, recordings and an editable copy of
the config appear in `./data`. Host networking (the default in
`docker-compose.yml`) is Linux-only. On Docker Desktop, switch to the
`ports:` block in the compose file.

## Flight simulator mode

```bash
python main.py --sim
```

The simulator speaks the same UDP protocol as the aircraft, so connection,
telemetry, controls, video and HUD all run for real. It streams telemetry at
10 Hz and a synthetic camera feed, and models flight physics, battery drain,
low-battery auto-land and the firmware's 15-second command watchdog. The
window title, an orange banner and a "SIMULATED FEED" watermark make it
impossible to mistake for a real flight. Tune it under `simulator:` in the
config (for example `start_battery: 25` to rehearse low-battery handling).

You can also enable it with `simulator.enabled: true` in the config or
`KOMPAS_SIMULATOR=1`. To run the simulated drone on its own, for a ground
station on another machine: `python run_simulator.py --bind 0.0.0.0`.

## Pre-flight checklist

![Pre-flight checklist, takeoff and landing](docs/media/kompas-preflight-takeoff.gif)
![Low battery triggers automatic landing](docs/media/kompas-safety-autoland.gif)

Takeoff stays locked until the aircraft passes a pre-flight checklist, shown
live in the main window while it's on the ground:

| Check | Fails when | Warns when |
|---|---|---|
| Connection | no session | |
| Telemetry link | silent for `connection_timeout` s, or below `min_telemetry_rate_hz` | |
| Command link | the aircraft stops answering commands | |
| Battery | unknown, or below `safety.min_battery_level` | within `battery_warning_margin` of it |
| Level | tilted more than `max_tilt_deg` | |
| Temperature | at or above `max_temperature` | at or above `warn_temperature` |
| On the ground | already flying | |
| Safety monitor | not running or disabled | |

Failures block takeoff with no override. Warnings allow it after the operator
confirms them. Every takeoff re-runs the checks with a live command probe,
because telemetry arriving only proves the aircraft can talk to us, not that
it can hear us. Thresholds are under `preflight:` in the config.

## Automatic safety monitoring

While the aircraft flies, KOMPAS watches battery, flight time, the telemetry
link and altitude. Automatic actions only ever **land** the aircraft; they
never cut the motors, which would drop it. EMERGENCY STOP remains the
operator's decision.

| Condition | Response |
|---|---|
| Battery at `min_battery_level` | Warning |
| Battery at `auto_land_battery_threshold` | Automatic landing (if `auto_land_low_battery`) |
| 80% / 100% of `max_flight_time` | Warning / automatic landing |
| No telemetry for `connection_timeout` s | Alert; with `connection_timeout_action: land`, lands as soon as the link returns. The aircraft's own watchdog lands it if the link stays down |
| Above `max_altitude` | Warning |

In simulator mode, the **Cut link** and **Battery 15%** buttons let you
rehearse these responses safely.

## Project layout

```
main.py              entry point (GUI, --sim, --config)
run_simulator.py     standalone simulated drone
config/              default config + loader/validator
src/communication/   UDP control link and video stream
src/control/         flight command layer
src/gui/             Tkinter main window and video/HUD window
src/simulator/       simulated drone and synthetic video
src/utils/           telemetry parsing, pre-flight, safety, recording, logging
tests/               unit tests (no hardware needed)
docs/                user manual, install guide, API reference, HUD guide
```

## Running the tests

```bash
pip install pytest
python -m pytest
```

No drone is needed; the tests use mocks and the simulator.

## Documentation

- [Installation guide](docs/installation_guide.md)
- [User manual](docs/user_manual.md)
- [Video and HUD guide](docs/video_hud_guide.md)
- [API reference](docs/api_reference.md)

## Contributing

Contributions are welcome: bug reports, hardware compatibility reports,
docs and code. See [CONTRIBUTING.md](CONTRIBUTING.md). Safety-relevant bugs
should be reported as described in [SECURITY.md](SECURITY.md).

## Safety

KOMPAS controls real aircraft. Software bugs, radio interference and
hardware faults can cause a crash, injury or property damage.

- Test every change in simulator mode before flying.
- Keep visual line of sight and be ready to take manual control.
- Fly in open areas, away from people, animals and property.
- Know and follow your local drone regulations.

You use this software at your own risk. See the warranty disclaimer in the
[license](LICENSE).

## Scope

KOMPAS is built for civilian and non-kinetic uses: hobby flight, research,
education, inspection, mapping and logistics. Weaponization is out of scope
for this project, and contributions in that direction will not be accepted.

## License

KOMPAS is released under the [Apache License 2.0](LICENSE).

KOMPAS is an independent project and is not affiliated with or endorsed by
any drone manufacturer. Product names are trademarks of their owners.
