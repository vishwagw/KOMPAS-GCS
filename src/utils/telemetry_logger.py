"""
Telemetry Logger for KOMPAS
Logs flight telemetry to CSV files with timestamps
"""

import csv
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
import logging


class TelemetryLogger:
    """Logs parsed telemetry data to CSV files during flight.

    Creates a new CSV file for each flight session (takeoff to landing).
    File format: logs/flight_YYYYMMDD_HHMMSS[_N].csv
    """

    # CSV columns - covers all fields from TelemetryParser._FIELDS plus timestamp
    CSV_COLUMNS = [
        'timestamp_iso',      # ISO 8601 timestamp
        'timestamp_epoch',    # Unix epoch seconds (float)
        'battery',            # Battery percentage
        'altitude',           # Altitude in cm
        'speed_x',            # Speed X component (cm/s)
        'speed_y',            # Speed Y component (cm/s)
        'speed_z',            # Speed Z component (cm/s)
        'speed_total',        # Total ground speed (cm/s)
        'pitch',              # Pitch angle (degrees)
        'roll',               # Roll angle (degrees)
        'yaw',                # Yaw angle (degrees)
        'acceleration_x',     # Acceleration X (g)
        'acceleration_y',     # Acceleration Y (g)
        'acceleration_z',     # Acceleration Z (g)
        'barometer',          # Barometer reading
        'time_of_flight',     # Time of flight sensor (cm)
        'motor_time',         # Motor running time (seconds)
        'temperature',        # Temperature (Celsius)
        'temperature_low',    # Low temperature sensor (Celsius)
    ]

    def __init__(self, log_dir: str = "logs"):
        """
        Initialize the telemetry logger.

        Args:
            log_dir: Directory to store log files (default: "logs")
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.logger = logging.getLogger(__name__)

        # Thread safety
        self._lock = threading.RLock()

        # State
        self._current_file: Optional[Path] = None
        self._csv_writer: Optional[csv.writer] = None
        self._file_handle = None
        self._is_logging = False
        self._flight_start_time: Optional[float] = None

    def _generate_filename(self) -> Path:
        """Generate a unique flight log filename with collision resistance.

        Returns:
            Path to the new log file
        """
        base_time = datetime.now()
        base_name = base_time.strftime("flight_%Y%m%d_%H%M%S")

        # Check for existing files with same base name (same second)
        counter = 0
        while True:
            if counter == 0:
                filename = f"{base_name}.csv"
            else:
                filename = f"{base_name}_{counter}.csv"

            filepath = self.log_dir / filename
            if not filepath.exists():
                return filepath
            counter += 1

            # Safety limit
            if counter > 999:
                raise RuntimeError("Too many collision attempts for log filename")

    def _write_header(self) -> None:
        """Write CSV header row."""
        if self._csv_writer:
            self._csv_writer.writerow(self.CSV_COLUMNS)

    def _parse_telemetry_to_row(self, parsed_data: Dict[str, Any]) -> List[Any]:
        """Convert parsed telemetry dict to CSV row matching CSV_COLUMNS.

        Args:
            parsed_data: Parsed telemetry from TelemetryParser

        Returns:
            List of values matching CSV_COLUMNS order
        """
        # Use telemetry timestamp if available, otherwise current time
        epoch_time = parsed_data.get('timestamp', time.time())
        iso_time = datetime.fromtimestamp(epoch_time).isoformat(timespec='milliseconds')

        row = [
            iso_time,
            f"{epoch_time:.3f}",
            parsed_data.get('battery', ''),
            parsed_data.get('altitude', ''),
            parsed_data.get('speed_x', ''),
            parsed_data.get('speed_y', ''),
            parsed_data.get('speed_z', ''),
            parsed_data.get('speed_total', ''),
            parsed_data.get('pitch', ''),
            parsed_data.get('roll', ''),
            parsed_data.get('yaw', ''),
            parsed_data.get('acceleration_x', ''),
            parsed_data.get('acceleration_y', ''),
            parsed_data.get('acceleration_z', ''),
            parsed_data.get('barometer', ''),
            parsed_data.get('time_of_flight', ''),
            parsed_data.get('motor_time', ''),
            parsed_data.get('temperature', ''),
            parsed_data.get('temperature_low', ''),
        ]
        return row

    def start_logging(self) -> bool:
        """Start logging telemetry to a new CSV file.

        Idempotent: safe to call multiple times; subsequent calls return True
        without creating a new file if already logging.

        Returns:
            True if logging started (or already active), False on error
        """
        with self._lock:
            if self._is_logging:
                self.logger.debug("Telemetry logging already active")
                return True

            try:
                self._current_file = self._generate_filename()
                self._file_handle = open(self._current_file, 'w', newline='', encoding='utf-8')
                self._csv_writer = csv.writer(self._file_handle)
                self._write_header()
                self._file_handle.flush()

                self._is_logging = True
                self._flight_start_time = time.time()

                self.logger.info(f"Started telemetry logging to {self._current_file}")
                return True

            except Exception as e:
                self.logger.error(f"Failed to start telemetry logging: {e}")
                self._cleanup()
                return False

    def stop_logging(self) -> bool:
        """Stop logging and close the CSV file.

        Idempotent: safe to call multiple times; subsequent calls return True
        without error if not currently logging.

        Returns:
            True if stopped (or already stopped), False on error
        """
        with self._lock:
            if not self._is_logging:
                self.logger.debug("Telemetry logging not active")
                return True

            try:
                self._cleanup()
                self.logger.info("Stopped telemetry logging")
                return True

            except Exception as e:
                self.logger.error(f"Error stopping telemetry logging: {e}")
                return False

    def _cleanup(self) -> None:
        """Internal cleanup - must be called with lock held."""
        if self._file_handle:
            try:
                self._file_handle.flush()
                self._file_handle.close()
            except Exception:
                pass
            finally:
                self._file_handle = None
                self._csv_writer = None
                self._current_file = None
                self._is_logging = False
                self._flight_start_time = None

    def log_telemetry(self, parsed_data: Dict[str, Any]) -> bool:
        """Write a telemetry data point to the CSV file.

        Thread-safe: can be called from telemetry callback thread.
        No-op if logging is not active.

        Args:
            parsed_data: Parsed telemetry from TelemetryParser

        Returns:
            True if written (or logging not active), False on error
        """
        with self._lock:
            if not self._is_logging or not self._csv_writer:
                return True  # Not logging is not an error

            try:
                row = self._parse_telemetry_to_row(parsed_data)
                self._csv_writer.writerow(row)
                self._file_handle.flush()
                return True

            except Exception as e:
                self.logger.error(f"Error writing telemetry to CSV: {e}")
                return False

    def flush(self) -> None:
        """Flush any buffered data to disk. Thread-safe."""
        with self._lock:
            if self._file_handle:
                try:
                    self._file_handle.flush()
                except Exception as e:
                    self.logger.error(f"Error flushing telemetry log: {e}")

    def is_logging(self) -> bool:
        """Check if currently logging. Thread-safe."""
        with self._lock:
            return self._is_logging

    def get_current_log_file(self) -> Optional[Path]:
        """Get the current log file path if logging. Thread-safe."""
        with self._lock:
            return self._current_file

    def get_flight_duration(self) -> Optional[float]:
        """Get current flight duration in seconds if logging. Thread-safe."""
        with self._lock:
            if self._flight_start_time is not None:
                return time.time() - self._flight_start_time
            return None

    def __enter__(self):
        """Context manager support."""
        self.start_logging()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager cleanup."""
        self.stop_logging()
        return False