#!/usr/bin/env python3
"""
Tests for TelemetryLogger — CSV flight logging with timestamps.
"""

import csv
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils.telemetry_logger import TelemetryLogger


# Skip GUI tests on headless Linux (no DISPLAY)
needs_display = pytest.mark.skipif(
    sys.platform.startswith("linux") and not os.environ.get("DISPLAY"),
    reason="needs a display",
)


class TestTelemetryLogger(unittest.TestCase):
    """Unit tests for the TelemetryLogger class."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.logger = TelemetryLogger(log_dir=self.tmpdir)

    def tearDown(self):
        # Make sure any open file handles are closed.
        if self.logger.is_logging():
            self.logger.stop_logging()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- filename generation -------------------------------------------------

    def test_filename_format(self):
        """Filenaem must follow flight_YYYYMMDD_HHMMSS.csv pattern."""
        path = self.logger._generate_filename()
        name = path.name
        self.assertTrue(name.startswith("flight_"))
        self.assertTrue(name.endswith(".csv"))
        # Strip the extension and check date-time portion length (15 chars).
        dt_part = name[len("flight_"):-4]
        self.assertEqual(len(dt_part), 15)

    def test_collision_resistance(self):
        """Two rapid calls to start_logging produce distinct files (different counters)."""
        self.logger.start_logging()
        first = self.logger.get_current_log_file()
        self.logger.stop_logging()

        # Start again - should get a new file with counter suffix
        self.logger.start_logging()
        second = self.logger.get_current_log_file()
        self.logger.stop_logging()

        self.assertNotEqual(first, second)
        self.assertIn("_1", second.name)

    def test_skips_existing(self):
        """If a file already exists, we skip past it."""
        existing = Path(self.tmpdir) / "flight_20260101_000000.csv"
        existing.write_text("stub\n")
        path = self.logger._generate_filename()
        self.assertNotEqual(path, existing)

    # -- start / stop idempotency ------------------------------------------

    def test_start_logging_creates_file(self):
        self.logger.start_logging()
        self.assertTrue(self.logger.is_logging())
        self.assertIsNotNone(self.logger.get_current_log_file())
        self.assertTrue(self.logger.get_current_log_file().exists())

    def test_stop_logging_closes_file(self):
        self.logger.start_logging()
        self.logger.stop_logging()
        self.assertFalse(self.logger.is_logging())
        self.assertIsNone(self.logger.get_current_log_file())

    def test_start_twice_is_idempotent(self):
        """Calling start_logging() twice does not create two files."""
        self.logger.start_logging()
        first = self.logger.get_current_log_file()
        self.logger.start_logging()
        self.assertTrue(self.logger.is_logging())
        self.assertEqual(self.logger.get_current_log_file(), first)

    def test_stop_twice_is_idempotent(self):
        self.logger.stop_logging()
        self.assertFalse(self.logger.is_logging())
        self.logger.stop_logging()
        self.assertFalse(self.logger.is_logging())

    def test_stop_without_start_succeeds(self):
        """stop_logging when nothing is logged returns True, no error."""
        result = self.logger.stop_logging()
        self.assertTrue(result)

    # -- CSV structure ------------------------------------------------------

    def test_header_written_on_start(self):
        """Header row must be present when logging stops."""
        self.logger.start_logging()
        self.logger.stop_logging()
        csv_files = sorted(Path(self.tmpdir).glob("flight_*.csv"))
        self.assertEqual(len(csv_files), 1)
        with open(csv_files[0], newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
        self.assertEqual(header, self.logger.CSV_COLUMNS)

    def test_csv_columns_match_spec(self):
        expected = [
            'timestamp_iso', 'timestamp_epoch', 'battery', 'altitude',
            'speed_x', 'speed_y', 'speed_z', 'speed_total',
            'pitch', 'roll', 'yaw',
            'acceleration_x', 'acceleration_y', 'acceleration_z',
            'barometer', 'time_of_flight', 'motor_time',
            'temperature', 'temperature_low',
        ]
        self.assertEqual(self.logger.CSV_COLUMNS, expected)

    def test_row_count_matches_columns(self):
        self.logger.start_logging()
        sample = {'battery': 85, 'altitude': 120, 'timestamp': time.time()}
        self.logger.log_telemetry(sample)
        self.logger.stop_logging()
        filepath = Path(self.tmpdir)
        csv_files = sorted(filepath.glob("flight_*.csv"))
        self.assertEqual(len(csv_files), 1)
        with open(csv_files[0], newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            row = next(reader)
        self.assertEqual(len(header), len(self.logger.CSV_COLUMNS))
        self.assertEqual(len(row), len(self.logger.CSV_COLUMNS))

    def test_empty_telemetry_produces_empty_fields(self):
        """log_telemetry with a bare dict writes a row of empty strings (except timestamp)."""
        self.logger.start_logging()
        self.logger.log_telemetry({})
        self.logger.stop_logging()
        csv_files = sorted(Path(self.tmpdir).glob("flight_*.csv"))
        with open(csv_files[0], newline="") as f:
            reader = csv.DictReader(f)
            row = next(reader)
        self.assertNotEqual(row['timestamp_iso'], '')
        self.assertNotEqual(row['timestamp_epoch'], '')
        for col in self.logger.CSV_COLUMNS[2:]:
            self.assertEqual(row[col], '')

    # -- no-op when not logging --------------------------------------------

    def test_log_before_start_returns_true(self):
        """log_telemetry before start_logging is a no-op that returns True."""
        result = self.logger.log_telemetry({'battery': 90})
        self.assertTrue(result)
        csv_files = list(Path(self.tmpdir).glob("flight_*.csv"))
        self.assertEqual(len(csv_files), 0)

    # -- flight duration ----------------------------------------------------

    def test_flight_duration_none_when_not_logging(self):
        self.assertIsNone(self.logger.get_flight_duration())

    def test_flight_duration_increases(self):
        self.logger.start_logging()
        time.sleep(0.05)
        dur = self.logger.get_flight_duration()
        self.logger.stop_logging()
        self.assertIsNotNone(dur)
        self.assertGreater(dur, 0.0)

    # -- context manager ---------------------------------------------------

    def test_context_manager_starts_and_stops(self):
        with self.logger:
            self.assertTrue(self.logger.is_logging())
        self.assertFalse(self.logger.is_logging())
        # File path is cleared after stop, but file itself persists on disk
        # Verify the file exists by checking the log directory
        csv_files = sorted(Path(self.tmpdir).glob("flight_*.csv"))
        self.assertEqual(len(csv_files), 1)

    # -- thread safety -----------------------------------------------------

    def test_flush_called_after_each_write(self):
        """flush() is called after each successful writerow()."""
        import unittest.mock as mock
        self.logger.start_logging()

        # Patch flush BEFORE writing
        with mock.patch.object(self.logger._file_handle, 'flush', wraps=self.logger._file_handle.flush) as mock_flush:
            for i in range(5):
                self.logger.log_telemetry({'battery': 80 + i * 2, 'altitude': 100 + i * 10, 'timestamp': time.time()})

            self.assertEqual(mock_flush.call_count, 5)

        self.logger.stop_logging()

    def test_concurrent_writes(self):
        """Multiple threads writing simultaneously should not raise."""
        self.logger.start_logging()
        errors = []

        def writer(tid):
            try:
                for _ in range(50):
                    self.logger.log_telemetry({
                        'battery': 80 + tid,
                        'altitude': 100 + tid * 10,
                        'timestamp': time.time(),
                    })
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.logger.stop_logging()
        self.assertEqual(len(errors), 0)

        csv_files = sorted(Path(self.tmpdir).glob("flight_*.csv"))
        self.assertEqual(len(csv_files), 1)
        with open(csv_files[0], newline="") as f:
            reader = csv.reader(f)
            next(reader)  # skip header
            rows = list(reader)
        self.assertEqual(len(rows), 200)  # 4 threads × 50 writes


# -- GUI shutdown path integration tests ------------------------------------

@needs_display
class TestGUIShutdownPaths(unittest.TestCase):
    """Integration tests for main_window.py shutdown paths (logger stop + disconnect)."""

    def setUp(self):
        # Create a mock tkinter root to avoid blocking mainloop
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()  # Hide the window
        self.root.protocol('WM_DELETE_WINDOW', lambda: None)  # Prevent destruction during test

    def tearDown(self):
        self.root.destroy()

    def _create_mock_components(self):
        """Create mock drone connection + telemetry logger for GUI component tests."""
        import unittest.mock as mock

        # Mock DroneConnection
        mock_connection = mock.MagicMock()
        mock_connection.disconnect = mock.MagicMock(return_value=True)

        # TelemetryLogger instance (simulating save_csv=True)
        from utils import telemetry_logger
        logger = telemetry_logger.TelemetryLogger()
        logger.start_logging()

        return mock_connection, logger

    @needs_display
    def test_disconnect_stops_logging_but_not_safety(self):
        """disconnect_drone() stops logger and disconnects, but preserves safety."""
        from gui.main_window import DroneControllerGUI

        mock_conn, logger = self._create_mock_components()

        # We'll construct just enough to test the disconnect path
        # Create a minimal config
        config = {
            'network': {'drone_ip': '192.168.1.1', 'command_port': 8889, 'state_port': 8890},
            'logging': {'save_flight_csv': True},
            'safety': {'min_battery_level': 20},
            'flight': {'max_altitude': 500, 'max_speed': 100},
            'simulator': {'enabled': False}
        }

        gui = DroneControllerGUI(config)
        gui.connection = mock_conn
        gui.telemetry_logger = logger
        gui.safety = mock.MagicMock()  # Mock safety manager

        # Verify initial state
        self.assertFalse(mock_conn.disconnect.called, "disconnect should not be called yet")
        self.assertTrue(logger.is_logging(), "Logger should be active")

        # Call disconnect_drone() first time
        gui.disconnect_drone()

        # Verify logger stopped and connection disconnected
        self.assertFalse(logger.is_logging(), "Logger should be stopped")
        self.assertTrue(mock_conn.disconnect.called, "Connection should be disconnected")

        # Safety should NOT have been stopped
        gui.safety.stop_monitoring.assert_not_called()

        # Call disconnect_drone() again — should be safe
        gui.disconnect_drone()

        # Verify no harm done (disconnect is idempotent)
        self.assertFalse(logger.is_logging(), "Logger still stopped")
        self.assertTrue(mock_conn.disconnect.called, "Connection still disconnected")
        gui.safety.stop_monitoring.assert_not_called()

    @needs_display
    def test_app_shutdown_stops_logging_disconnects_and_stops_safety(self):
        """_app_shutdown() stops logger, disconnects, and stops safety monitoring."""
        from gui.main_window import DroneControllerGUI

        mock_conn, logger = self._create_mock_components()

        config = {
            'network': {'drone_ip': '192.168.1.1', 'command_port': 8889, 'state_port': 8890},
            'logging': {'save_flight_csv': True},
            'safety': {'min_battery_level': 20},
            'flight': {'max_altitude': 500, 'max_speed': 100},
            'simulator': {'enabled': False}
        }

        gui = DroneControllerGUI(config)
        gui.connection = mock_conn
        gui.telemetry_logger = logger
        gui.safety = mock.MagicMock()

        # Verify initial state
        self.assertFalse(mock_conn.disconnect.called, "disconnect should not be called yet")
        self.assertTrue(logger.is_logging(), "Logger should be active")

        # Call _app_shutdown() first time
        gui._app_shutdown()

        # Verify all three cleanup operations occurred
        self.assertFalse(logger.is_logging(), "Logger should be stopped")
        self.assertTrue(mock_conn.disconnect.called, "Connection should be disconnected")
        gui.safety.stop_monitoring.assert_called_once()

        # Call _app_shutdown() again — should be idempotent (noop)
        gui._app_shutdown()

        # Verify no harm done (double-call should be safe)
        self.assertFalse(logger.is_logging(), "Logger still stopped")
        self.assertTrue(mock_conn.disconnect.called, "Connection still disconnected")
        # Safety stop_monitoring should NOT have been called twice
        gui.safety.stop_monitoring.assert_called_once()

    @needs_display
    def test_app_shutdown_continues_if_logger_raises(self):
        """If stop_logging raises, disconnect and stop_monitoring still execute."""
        from gui.main_window import DroneControllerGUI

        mock_conn = mock.MagicMock()
        mock_conn.disconnect.return_value = True
        mock_safety = mock.MagicMock()
        failing_logger = mock.MagicMock()
        failing_logger.is_logging.side_effect = AssertionError("should not be checked")
        failing_logger.stop_logging.side_effect = RuntimeError("logger broken")

        config = {
            'network': {'drone_ip': '192.168.1.1', 'command_port': 8889, 'state_port': 8890},
            'logging': {'save_flight_csv': True},
            'safety': {'min_battery_level': 20},
            'flight': {'max_altitude': 500, 'max_speed': 100},
            'simulator': {'enabled': False}
        }
        gui = DroneControllerGUI(config)
        gui.connection = mock_conn
        gui.telemetry_logger = failing_logger
        gui.safety = mock_safety
        gui._app_shutdown()

        mock_conn.disconnect.assert_called_once()
        mock_safety.stop_monitoring.assert_called_once()

    @needs_display
    def test_app_shutdown_continues_if_disconnect_raises(self):
        """If disconnect raises, stop_monitoring still executes."""
        from gui.main_window import DroneControllerGUI
        import tempfile as _tf

        mock_conn = mock.MagicMock()
        mock_conn.disconnect.side_effect = ConnectionError("link lost")
        mock_safety = mock.MagicMock()
        tmpdir = _tf.mkdtemp()
        logger = TelemetryLogger(log_dir=tmpdir)
        logger.start_logging()
        logger.stop_logging()

        config = {
            'network': {'drone_ip': '192.168.1.1', 'command_port': 8889, 'state_port': 8890},
            'logging': {'save_flight_csv': True},
            'safety': {'min_battery_level': 20},
            'flight': {'max_altitude': 500, 'max_speed': 100},
            'simulator': {'enabled': False}
        }
        gui = DroneControllerGUI(config)
        gui.connection = mock_conn
        gui.telemetry_logger = logger
        gui.safety = mock_safety
        gui._app_shutdown()

        mock_safety.stop_monitoring.assert_called_once()
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)

    @needs_display
    def test_app_shutdown_logger_failure_then_retry_succeeds(self):
        """Logger shutdown failure on first call is retried and succeeds on second call."""
        from gui.main_window import DroneControllerGUI
        import tempfile as _tf

        mock_conn = mock.MagicMock()
        mock_conn.disconnect.return_value = True
        mock_safety = mock.MagicMock()
        tmpdir = _tf.mkdtemp()

        # Create a logger that fails once, then succeeds
        call_count = [0]
        def failing_then_succeeding():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first attempt fails")
            # Second call succeeds - use real logger
            return None

        failing_logger = mock.MagicMock()
        failing_logger.stop_logging.side_effect = failing_then_succeeding

        config = {
            'network': {'drone_ip': '192.168.1.1', 'command_port': 8889, 'state_port': 8890},
            'logging': {'save_flight_csv': True},
            'safety': {'min_battery_level': 20},
            'flight': {'max_altitude': 500, 'max_speed': 100},
            'simulator': {'enabled': False}
        }
        gui = DroneControllerGUI(config)
        gui.connection = mock_conn
        gui.telemetry_logger = failing_logger
        gui.safety = mock_safety

        # First call: logger fails, but connection and safety still run
        gui._app_shutdown()
        mock_conn.disconnect.assert_called_once()
        mock_safety.stop_monitoring.assert_called_once()
        self.assertFalse(gui._shutdown_logger_done)  # Logger not marked done
        self.assertTrue(gui._shutdown_connection_done)
        self.assertTrue(gui._shutdown_safety_done)

        # Second call: logger should retry and succeed
        gui._app_shutdown()
        self.assertTrue(gui._shutdown_logger_done)  # Now marked done
        self.assertTrue(gui._shutdown_complete)  # All done, overall complete
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)

    @needs_display
    def test_app_shutdown_successful_operations_not_repeated(self):
        """Operations that succeed on first call are not repeated on subsequent calls."""
        from gui.main_window import DroneControllerGUI
        import tempfile as _tf

        mock_conn = mock.MagicMock()
        mock_conn.disconnect.return_value = True
        mock_safety = mock.MagicMock()
        tmpdir = _tf.mkdtemp()
        logger = TelemetryLogger(log_dir=tmpdir)
        logger.start_logging()
        logger.stop_logging()

        config = {
            'network': {'drone_ip': '192.168.1.1', 'command_port': 8889, 'state_port': 8890},
            'logging': {'save_flight_csv': True},
            'safety': {'min_battery_level': 20},
            'flight': {'max_altitude': 500, 'max_speed': 100},
            'simulator': {'enabled': False}
        }
        gui = DroneControllerGUI(config)
        gui.connection = mock_conn
        gui.telemetry_logger = logger
        gui.safety = mock_safety

        # First call: all succeed
        gui._app_shutdown()
        first_conn_calls = mock_conn.disconnect.call_count
        first_safety_calls = mock_safety.stop_monitoring.call_count
        self.assertTrue(gui._shutdown_logger_done)
        self.assertTrue(gui._shutdown_connection_done)
        self.assertTrue(gui._shutdown_safety_done)
        self.assertTrue(gui._shutdown_complete)

        # Second call: nothing should be retried
        gui._app_shutdown()
        self.assertEqual(mock_conn.disconnect.call_count, first_conn_calls)
        self.assertEqual(mock_safety.stop_monitoring.call_count, first_safety_calls)
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)

    @needs_display
    def test_app_shutdown_multiple_calls_safe(self):
        """Multiple shutdown calls from different paths (WM_DELETE_WINDOW + finally) remain safe."""
        from gui.main_window import DroneControllerGUI
        import tempfile as _tf

        mock_conn = mock.MagicMock()
        mock_conn.disconnect.return_value = True
        mock_safety = mock.MagicMock()
        tmpdir = _tf.mkdtemp()
        logger = TelemetryLogger(log_dir=tmpdir)
        logger.start_logging()
        logger.stop_logging()

        config = {
            'network': {'drone_ip': '192.168.1.1', 'command_port': 8889, 'state_port': 8890},
            'logging': {'save_flight_csv': True},
            'safety': {'min_battery_level': 20},
            'flight': {'max_altitude': 500, 'max_speed': 100},
            'simulator': {'enabled': False}
        }
        gui = DroneControllerGUI(config)
        gui.connection = mock_conn
        gui.telemetry_logger = logger
        gui.safety = mock_safety

        # Simulate WM_DELETE_WINDOW path
        gui._app_shutdown()
        # Simulate run() finally block path
        gui._app_shutdown()
        # Third call for good measure
        gui._app_shutdown()

        # All operations run exactly once
        mock_conn.disconnect.assert_called_once()
        mock_safety.stop_monitoring.assert_called_once()
        self.assertTrue(gui._shutdown_complete)
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
