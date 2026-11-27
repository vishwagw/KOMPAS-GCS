"""
KOMPAS configuration management.

Configuration is a nested dictionary organised into sections (network,
flight, safety, video, hud, logging, advanced). Built-in defaults are
deep-merged with an optional YAML file, so the file only needs to contain
the values you want to change.

The YAML file is located in this order:
  1. the path passed to ConfigManager / load_config
  2. the KOMPAS_CONFIG environment variable (used by the Docker image)
  3. config/drone_config.yaml next to this module

A missing file is not an error: the defaults are used. A file that exists
but cannot be parsed, or values that fail validation, are errors, because a
flight controller should not start with settings it doesn't understand.
"""

from __future__ import annotations

import copy
import ipaddress
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "KOMPAS_CONFIG"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "drone_config.yaml"

# Values here mirror the defaults hard-coded throughout src/ and the schema
# documented in docs/. Keep them in sync if either changes.
DEFAULT_CONFIG: Dict[str, Any] = {
    "network": {
        "drone_ip": "192.168.1.1",
        "command_port": 8889,
        "state_port": 8890,
        "video_port": 11111,
        "connection_timeout": 5.0,
    },
    "flight": {
        "max_altitude": 500,          # cm
        "max_speed": 100,             # 10-100
        "default_speed": 50,
        "default_move_distance": 30,  # cm
    },
    "safety": {
        "min_battery_level": 20,              # % required to take off
        "emergency_landing_enabled": True,
        "auto_land_low_battery": True,
        "auto_land_battery_threshold": 10,    # % that triggers auto-land
        "max_flight_time": 1200,              # seconds
        "connection_timeout_action": "land",  # "land" or "hover"
    },
    "video": {
        "enable_video_stream": True,
        "resolution_width": 960,
        "resolution_height": 720,
        "frame_rate": 30,
        "video_codec": "mp4v",
        "auto_start_stream": False,
        "recording_directory": "recordings",
    },
    "hud": {
        "enable_hud": True,
        "show_crosshair": True,
        "show_telemetry": True,
        "show_artificial_horizon": True,
        "show_compass": True,
        "hud_color_r": 0,
        "hud_color_g": 255,
        "hud_color_b": 0,
        "font_scale": 0.6,
        "warning_color_r": 0,
        "warning_color_g": 0,
        "warning_color_b": 255,
    },
    "logging": {
        "level": "INFO",
        "save_flight_csv": True,
    },
    "advanced": {
        "enable_debug_mode": False,
    },
    "preflight": {
        "max_tilt_deg": 10,               # block takeoff from ground steeper than this
        "battery_warning_margin": 15,     # warn when within this % of safety.min_battery_level
        "warn_temperature": 70,           # C
        "max_temperature": 80,            # C, block takeoff at or above
        "min_telemetry_rate_hz": 2,
    },
    "simulator": {
        "enabled": False,                 # or run with --sim / KOMPAS_SIMULATOR=1
        "telemetry_rate_hz": 10,
        "video_enabled": True,
        "video_fps": 15,
        "start_battery": 100,             # % - start low to rehearse low-battery handling
        "battery_drain_per_min": 7.0,     # % per minute while motors run (~14 min flight)
        "command_watchdog_s": 15,         # auto-land after this long without a command; 0 = off
    },
}

CONNECTION_TIMEOUT_ACTIONS = ("land", "hover")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_MISSING = object()


class ConfigError(ValueError):
    """Raised when the configuration file is unreadable or invalid."""


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``base`` with ``override`` merged in recursively."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _resolve_path(config_path: Optional[Union[str, Path]]) -> Path:
    if config_path is not None:
        return Path(config_path)
    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_PATH


class ConfigManager:
    """Loads, queries, validates and saves the application configuration."""

    def __init__(self, config_path: Optional[Union[str, Path]] = None):
        self.config_path = _resolve_path(config_path)
        self.config: Dict[str, Any] = self._load()

    # -- loading ------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            logger.info("No config file at %s; using built-in defaults", self.config_path)
            return copy.deepcopy(DEFAULT_CONFIG)

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                user_config = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            raise ConfigError(f"Could not read config file {self.config_path}: {e}") from e

        if user_config is None:  # empty file
            user_config = {}
        if not isinstance(user_config, dict):
            raise ConfigError(
                f"Config file {self.config_path} must contain a mapping of sections, "
                f"got {type(user_config).__name__}"
            )

        logger.info("Loaded config from %s", self.config_path)
        return _deep_merge(DEFAULT_CONFIG, user_config)

    # -- access -------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value by dotted path, e.g. ``get('network.drone_ip')``.

        A plain section name (``get('network')``) returns that section's dict.
        """
        node: Any = self.config
        for part in key.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(part, _MISSING)
            if node is _MISSING:
                return default
        return node

    def set(self, key: str, value: Any) -> None:
        """Set a value by dotted path, creating sections as needed."""
        parts = key.split(".")
        node = self.config
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    def as_dict(self) -> Dict[str, Any]:
        """Return an independent copy of the full nested configuration."""
        return copy.deepcopy(self.config)

    def save(self, config_path: Optional[Union[str, Path]] = None) -> Path:
        """Write the current configuration to YAML and return the path used."""
        path = Path(config_path) if config_path is not None else self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.config, f, sort_keys=False, default_flow_style=False)
        return path

    # -- validation ---------------------------------------------------------

    def validate_config(self) -> List[str]:
        """Return a list of human-readable problems; empty means valid."""
        errors: List[str] = []

        def number(key: str, lo: float, hi: float, integer: bool = False) -> Optional[float]:
            value = self.get(key)
            kind = int if integer else (int, float)
            # bool is a subclass of int; "true" is never a valid port or speed
            if isinstance(value, bool) or not isinstance(value, kind):
                errors.append(f"{key} must be {'an integer' if integer else 'a number'}, got {value!r}")
                return None
            if not lo <= value <= hi:
                errors.append(f"{key} must be between {lo} and {hi}, got {value}")
                return None
            return value

        # network
        ip = self.get("network.drone_ip")
        try:
            ipaddress.ip_address(str(ip))
        except ValueError:
            errors.append(f"network.drone_ip is not a valid IP address: {ip!r}")

        ports = {}
        for name in ("command_port", "state_port", "video_port"):
            port = number(f"network.{name}", 1, 65535, integer=True)
            if port is not None:
                ports[name] = port
        if len(set(ports.values())) != len(ports):
            errors.append(f"network ports must all be different, got {ports}")

        number("network.connection_timeout", 0.1, 120)

        # flight
        number("flight.max_altitude", 20, 3000, integer=True)
        max_speed = number("flight.max_speed", 10, 100, integer=True)
        default_speed = number("flight.default_speed", 10, 100, integer=True)
        if max_speed is not None and default_speed is not None and default_speed > max_speed:
            errors.append(
                f"flight.default_speed ({default_speed}) cannot exceed flight.max_speed ({max_speed})"
            )
        number("flight.default_move_distance", 20, 500, integer=True)

        # safety
        min_battery = number("safety.min_battery_level", 0, 100, integer=True)
        auto_land = number("safety.auto_land_battery_threshold", 0, 100, integer=True)
        if min_battery is not None and auto_land is not None and auto_land >= min_battery:
            errors.append(
                f"safety.auto_land_battery_threshold ({auto_land}) must be lower than "
                f"safety.min_battery_level ({min_battery})"
            )
        number("safety.max_flight_time", 1, 7200, integer=True)
        action = self.get("safety.connection_timeout_action")
        if action not in CONNECTION_TIMEOUT_ACTIONS:
            errors.append(
                f"safety.connection_timeout_action must be one of "
                f"{', '.join(CONNECTION_TIMEOUT_ACTIONS)}, got {action!r}"
            )

        # video / hud
        number("video.resolution_width", 160, 4096, integer=True)
        number("video.resolution_height", 120, 4096, integer=True)
        number("video.frame_rate", 1, 120, integer=True)
        codec = self.get("video.video_codec")
        if not (isinstance(codec, str) and len(codec) == 4):
            errors.append(f"video.video_codec must be a 4-character FourCC, got {codec!r}")
        for channel in ("hud_color_r", "hud_color_g", "hud_color_b",
                        "warning_color_r", "warning_color_g", "warning_color_b"):
            number(f"hud.{channel}", 0, 255, integer=True)
        number("hud.font_scale", 0.1, 5)

        # preflight
        number("preflight.max_tilt_deg", 1, 45)
        number("preflight.battery_warning_margin", 0, 80)
        number("preflight.min_telemetry_rate_hz", 0.5, 50)
        warn_t = number("preflight.warn_temperature", 0, 150)
        max_t = number("preflight.max_temperature", 0, 150)
        if warn_t is not None and max_t is not None and warn_t > max_t:
            errors.append(f"preflight.warn_temperature ({warn_t}) cannot exceed "
                          f"preflight.max_temperature ({max_t})")

        # simulator
        if not isinstance(self.get("simulator.enabled"), bool):
            errors.append(f"simulator.enabled must be true or false, got {self.get('simulator.enabled')!r}")
        number("simulator.telemetry_rate_hz", 1, 50)
        number("simulator.video_fps", 1, 30)
        number("simulator.start_battery", 0, 100)
        number("simulator.battery_drain_per_min", 0, 100)
        number("simulator.command_watchdog_s", 0, 600)

        # logging
        level = self.get("logging.level")
        if not (isinstance(level, str) and level.upper() in LOG_LEVELS):
            errors.append(f"logging.level must be one of {', '.join(LOG_LEVELS)}, got {level!r}")

        save_csv = self.get("logging.save_flight_csv")
        if not isinstance(save_csv, bool):
            errors.append(f"logging.save_flight_csv must be true or false, got {save_csv!r}")

        return errors


def load_config(config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Load and validate the configuration, returning it as a nested dict.

    Raises ConfigError if the file is unreadable or any value is invalid.
    """
    manager = ConfigManager(config_path)
    errors = manager.validate_config()
    if errors:
        details = "\n  - ".join(errors)
        raise ConfigError(f"Invalid configuration in {manager.config_path}:\n  - {details}")
    return manager.as_dict()
