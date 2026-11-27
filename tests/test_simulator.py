#!/usr/bin/env python3
"""
Flight Simulator Mode tests.

The physics tests step the simulator by hand, so they're deterministic.
The integration tests run the ground station's real DroneConnection and
DroneController against the simulator over real UDP sockets, so they
exercise the actual telemetry loop end to end.
"""

import csv
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub cv2 before any source module that transitively imports it is loaded;
# src/utils/__init__.py re-exports VideoRecorder which pulls in OpenCV.
# The simulator and telemetry path itself has no cv2 dependency.
class _FakeCV2:
    def __getattr__(self, name):
        return None
    def __call__(self, *a, **kw):
        return None
sys.modules.setdefault('cv2', _FakeCV2())

from simulator import SimulatedDrone                      # noqa: E402
from utils.telemetry_logger import TelemetryLogger        # noqa: E402
from utils.telemetry_parser import TelemetryParser        # noqa: E402
from control.drone_controller import DroneController      # noqa: E402


def fly(drone, command, max_seconds=30.0, dt=0.02):
    """Send a command to a hand-stepped simulator; step until it's answered."""
    reply = drone.handle_command(command)
    if reply is not None:
        return reply
    maneuver = drone._maneuver
    for _ in range(int(max_seconds / dt)):
        drone.step(dt)
        if maneuver.done.is_set():
            return maneuver.result
    return "NOT FINISHED"


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestSimPhysics(unittest.TestCase):

    def setUp(self):
        self.drone = SimulatedDrone(seed=1, command_watchdog_s=0)
        self.parser = TelemetryParser()

    def state(self):
        return self.parser.parse(self.drone.telemetry_string())

    def test_requires_sdk_mode(self):
        self.assertEqual(fly(self.drone, "takeoff"), "error Not in SDK mode")
        self.assertEqual(fly(self.drone, "command"), "ok")

    def test_takeoff_climbs_and_lands(self):
        fly(self.drone, "command")
        self.assertEqual(fly(self.drone, "takeoff"), "ok")
        self.assertAlmostEqual(self.state()['altitude'], 80, delta=4)
        self.assertEqual(fly(self.drone, "land"), "ok")
        self.assertEqual(self.state()['altitude'], 0)
        self.assertEqual(self.drone.state.phase, "landed")

    def test_moves_follow_heading(self):
        fly(self.drone, "command")
        fly(self.drone, "takeoff")
        self.assertEqual(fly(self.drone, "cw 90"), "ok")        # now facing east
        self.assertEqual(fly(self.drone, "forward 100"), "ok")
        self.assertAlmostEqual(self.drone.state.x, 100, delta=3)  # moved east...
        self.assertAlmostEqual(self.drone.state.y, 0, delta=3)    # ...not north
        self.assertEqual(fly(self.drone, "up 50"), "ok")
        self.assertAlmostEqual(self.drone.state.z, 130, delta=3)

    def test_telemetry_shows_motion_while_moving(self):
        fly(self.drone, "command")
        fly(self.drone, "takeoff")
        self.drone.handle_command("forward 300")
        for _ in range(50):          # 1 s into the move
            self.drone.step(0.02)
        state = self.state()
        self.assertGreater(state['speed_x'], 20)   # flying forward
        self.assertLess(state['pitch'], -3)        # nose down to accelerate

    def test_rejects_invalid_commands(self):
        fly(self.drone, "command")
        self.assertEqual(fly(self.drone, "forward 50"), "error Not flying")
        fly(self.drone, "takeoff")
        self.assertEqual(fly(self.drone, "forward 5"), "error Out of range")
        self.assertEqual(fly(self.drone, "speed 500"), "error Out of range")
        self.assertEqual(fly(self.drone, "barrelroll"), "error Unknown command")

    def test_emergency_drops_aircraft(self):
        fly(self.drone, "command")
        fly(self.drone, "takeoff")
        fly(self.drone, "up 100")
        self.drone.handle_command("forward 300")
        self.assertEqual(self.drone.handle_command("emergency"), "ok")
        for _ in range(100):
            self.drone.step(0.02)
        self.assertEqual(self.drone.state.phase, "landed")
        self.assertEqual(self.state()['altitude'], 0)
        self.assertFalse(self.drone.state.motors_on)

    def test_low_battery_forces_landing(self):
        drone = SimulatedDrone(seed=1, command_watchdog_s=0, start_battery=12,
                               battery_drain_per_min=60)  # 1 %/s
        fly(drone, "command")
        self.assertEqual(fly(drone, "takeoff"), "ok")
        for _ in range(int(15 / 0.02)):
            drone.step(0.02)
        self.assertEqual(drone.state.phase, "landed")

    def test_takeoff_refused_on_low_battery(self):
        drone = SimulatedDrone(seed=1, start_battery=8)
        fly(drone, "command")
        self.assertEqual(fly(drone, "takeoff"), "error Low battery")

    def test_command_watchdog_auto_lands(self):
        drone = SimulatedDrone(seed=1, command_watchdog_s=5)
        fly(drone, "command")
        fly(drone, "takeoff")
        for _ in range(int(10 / 0.02)):   # 10 s with no commands
            drone.step(0.02)
        self.assertEqual(drone.state.phase, "landed")

    def test_queries(self):
        fly(self.drone, "command")
        self.assertEqual(fly(self.drone, "battery?"), "100")
        self.assertEqual(fly(self.drone, "height?"), "0dm")


class TestTelemetryParserTelloFormat(unittest.TestCase):

    def test_temph_is_not_read_as_altitude(self):
        line = ("pitch:-3;roll:2;yaw:45;vgx:30;vgy:-40;vgz:0;templ:58;temph:61;"
                "tof:92;h:80;bat:87;baro:12.34;time:15;agx:-12.00;agy:4.00;agz:-998.00;\r\n")
        parsed = TelemetryParser().parse(line)
        self.assertEqual(parsed['altitude'], 80)
        self.assertEqual(parsed['temperature'], 61)
        self.assertEqual((parsed['pitch'], parsed['roll'], parsed['yaw']), (-3, 2, 45))
        self.assertEqual(parsed['speed_total'], 50.0)
        self.assertEqual(parsed['battery'], 87)


class TestTelemetryLoopOverUDP(unittest.TestCase):
    """Real sockets, real ground-station classes, simulated aircraft."""

    def setUp(self):
        from communication.wifi_connection import DroneConnection
        from control.drone_controller import DroneController

        self.state_port = free_udp_port()
        self.drone = SimulatedDrone(command_port=0, state_port=self.state_port,
                                    video_enabled=False, telemetry_rate_hz=20,
                                    time_scale=4.0, seed=1).start()
        self.connection = DroneConnection("127.0.0.1", self.drone.command_port, self.state_port)
        self.parser = TelemetryParser()
        self.telemetry = []
        self.connection.set_telemetry_callback(self.telemetry.append)
        self.controller = DroneController(self.connection)

    def tearDown(self):
        self.connection.disconnect()
        self.drone.stop()

    def latest(self):
        return self.parser.parse(self.telemetry[-1])

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.telemetry and predicate(self.latest()):
                return True
            time.sleep(0.05)
        return False

    def feed_controller(self):
        """Stand-in for the GUI: route telemetry into the controller."""
        def route(line):
            self.telemetry.append(line)
            self.controller.update_from_telemetry(self.parser.parse(line))
        self.connection.set_telemetry_callback(route)

    def test_connect_and_receive_telemetry(self):
        self.assertTrue(self.connection.connect())
        self.assertTrue(self.connection.is_connected)
        self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))
        time.sleep(1.0)
        self.assertGreaterEqual(len(self.telemetry), 10)   # ~20 Hz

    def test_full_flight_through_controller(self):
        self.feed_controller()
        self.assertTrue(self.connection.connect())
        self.assertTrue(self.controller.initialize())
        self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))
        self.assertEqual(self.controller.current_battery, self.latest()['battery'])

        self.assertTrue(self.controller.takeoff())
        self.assertTrue(self.wait_for(lambda s: s['altitude'] >= 70))
        self.assertTrue(self.controller.move_up(50))
        self.assertTrue(self.wait_for(lambda s: s['altitude'] >= 120))
        self.assertTrue(self.controller.rotate_clockwise(90))
        self.assertTrue(self.wait_for(lambda s: abs(s['yaw'] - 90) <= 2))
        self.assertTrue(self.controller.land())
        self.assertTrue(self.wait_for(lambda s: s['altitude'] == 0))

    def test_takeoff_refused_before_battery_is_known(self):
        self.assertTrue(self.connection.connect())
        # Telemetry isn't routed to the controller, so battery stays unknown.
        self.assertIsNone(self.controller.current_battery)
        self.assertFalse(self.controller.takeoff())

    def test_emergency_not_blocked_by_move_in_progress(self):
        self.feed_controller()
        self.assertTrue(self.connection.connect())
        self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))
        self.assertTrue(self.controller.takeoff())
        self.connection.send_command("speed 10")

        results = {}
        mover = threading.Thread(
            target=lambda: results.setdefault("move", self.connection.send_command("forward 500", timeout=60)))
        mover.start()
        time.sleep(0.5)                    # the move now holds the command lock

        started = time.time()
        ack = self.connection.send_command("emergency")
        self.assertLess(time.time() - started, 1.5)   # didn't wait behind the move
        self.assertEqual(ack, "ok")

        mover.join(timeout=5)
        self.assertEqual(results.get("move"), "error Motor stop")
        self.assertTrue(self.wait_for(lambda s: s['altitude'] == 0, timeout=5))

    def test_keepalive_prevents_watchdog_landing(self):
        self.drone.command_watchdog_s = 8    # sim seconds = 2 s real at 4x
        self.connection.keepalive_interval = 0.5
        self.feed_controller()
        self.assertTrue(self.connection.connect())
        self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))
        self.assertTrue(self.controller.takeoff())
        time.sleep(4.0)                      # 16 sim-seconds with no pilot input
        self.assertEqual(self.drone.state.phase, "flying")
        self.assertGreater(self.latest()['altitude'], 60)

    def test_controller_notices_auto_landing(self):
        self.drone.command_watchdog_s = 4
        self.connection.keepalive_interval = 1000   # no keepalive: watchdog fires
        self.feed_controller()
        self.assertTrue(self.connection.connect())
        self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))
        self.assertTrue(self.controller.takeoff())
        self.assertTrue(self.wait_for(lambda s: s['altitude'] == 0, timeout=10))
        deadline = time.time() + 5
        while self.controller.is_flying and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(self.controller.is_flying)

    # -- CSV logging integration tests ----------------------------------------

    def test_csv_logging_integration(self):
        """Full end-to-end CSV logging: simulator -> UDP -> parser -> controller -> logger -> CSV."""
        # Create a temporary directory for logs
        tmpdir = tempfile.mkdtemp()
        try:
            # Disable command watchdog to avoid auto-land during test
            self.drone.command_watchdog_s = 0
            # Create a new controller with a TelemetryLogger
            self.controller = DroneController(self.connection, telemetry_logger=TelemetryLogger(log_dir=tmpdir))
            self.feed_controller()

            self.assertTrue(self.connection.connect())
            self.assertTrue(self.controller.initialize())
            self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))

            # Takeoff should start logging
            self.assertTrue(self.controller.takeoff())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] >= 70))

            # Allow telemetry to flow for a bit to generate multiple packets
            time.sleep(0.5)

            # Verify logging is active
            self.assertTrue(self.controller.telemetry_logger.is_logging())
            log_file = self.controller.telemetry_logger.get_current_log_file()
            self.assertIsNotNone(log_file)
            self.assertTrue(log_file.exists())

            # Verify logging is active before landing - check flight_duration BEFORE landing
            # because stop_logging() clears _flight_start_time
            self.assertTrue(self.controller.telemetry_logger.is_logging())
            flight_duration = self.controller.telemetry_logger.get_flight_duration()
            self.assertIsNotNone(flight_duration)
            self.assertGreater(flight_duration, 0)

            # Land should stop logging
            self.assertTrue(self.controller.land())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] == 0))
            self.assertFalse(self.controller.telemetry_logger.is_logging())

            # Verify CSV file contents
            csv_files = sorted(Path(tmpdir).glob("flight_*.csv"))
            self.assertEqual(len(csv_files), 1, "Exactly one CSV file should exist")

            with open(csv_files[0], newline='') as f:
                reader = csv.reader(f)
                rows = list(reader)

            # Header + at least some data rows (takeoff generates telemetry)
            self.assertGreaterEqual(len(rows), 2, "CSV should have header + data rows")
            self.assertEqual(rows[0], TelemetryLogger.CSV_COLUMNS)

            # Verify all data rows have correct column count
            for row in rows[1:]:
                self.assertEqual(len(row), len(TelemetryLogger.CSV_COLUMNS))

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_csv_multiple_packets_recorded(self):
        """Verify multiple telemetry packets produce expected CSV rows (no deduplication)."""
        tmpdir = tempfile.mkdtemp()
        try:
            self.drone.command_watchdog_s = 0
            self.controller = DroneController(self.connection, telemetry_logger=TelemetryLogger(log_dir=tmpdir))
            self.feed_controller()

            self.assertTrue(self.connection.connect())
            self.assertTrue(self.controller.initialize())
            self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))

            self.assertTrue(self.controller.takeoff())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] >= 70))

            # Let telemetry flow for ~1 second at ~20Hz = ~20 packets
            time.sleep(1.2)

            self.assertTrue(self.controller.land())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] == 0))

            csv_files = sorted(Path(tmpdir).glob("flight_*.csv"))
            self.assertEqual(len(csv_files), 1)

            with open(csv_files[0], newline='') as f:
                reader = csv.reader(f)
                rows = list(reader)

            # Header + data rows. At 20Hz for ~1.7s flight (takeoff + sleep + land) = ~34+ rows
            # The exact count varies, but should be well over 10
            data_rows = len(rows) - 1
            self.assertGreater(data_rows, 10, f"Expected >10 data rows, got {data_rows}")

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_csv_identical_consecutive_packets_not_deduplicated(self):
        """Identical consecutive telemetry packets are not incorrectly discarded."""
        tmpdir = tempfile.mkdtemp()
        try:
            self.controller = DroneController(self.connection, telemetry_logger=TelemetryLogger(log_dir=tmpdir))

            # Manually feed identical packets multiple times to the logger
            self.controller.telemetry_logger.start_logging()
            identical_packet = {
                'battery': 85,
                'altitude': 100,
                'speed_x': 10,
                'speed_y': 0,
                'speed_z': 0,
                'speed_total': 10,
                'pitch': -5,
                'roll': 0,
                'yaw': 90,
                'acceleration_x': 0.1,
                'acceleration_y': 0.0,
                'acceleration_z': -1.0,
                'barometer': 1013.25,
                'time_of_flight': 100,
                'motor_time': 120,
                'temperature': 25,
                'temperature_low': 24,
                'timestamp': time.time(),
            }

            # Write the same packet 5 times
            for _ in range(5):
                self.controller.telemetry_logger.log_telemetry(identical_packet)

            self.controller.telemetry_logger.stop_logging()

            csv_files = sorted(Path(tmpdir).glob("flight_*.csv"))
            self.assertEqual(len(csv_files), 1)

            with open(csv_files[0], newline='') as f:
                reader = csv.reader(f)
                rows = list(reader)

            # Header + 5 identical data rows
            self.assertEqual(len(rows), 6, "Should have header + 5 identical rows")
            for row in rows[1:]:
                self.assertEqual(row[2], '85')  # battery
                self.assertEqual(row[3], '100')  # altitude

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_csv_landing_stops_logging(self):
        """Landing command stops logging."""
        tmpdir = tempfile.mkdtemp()
        try:
            self.drone.command_watchdog_s = 0
            self.controller = DroneController(self.connection, telemetry_logger=TelemetryLogger(log_dir=tmpdir))
            self.feed_controller()

            self.assertTrue(self.connection.connect())
            self.assertTrue(self.controller.initialize())
            self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))

            self.assertTrue(self.controller.takeoff())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] >= 70))
            self.assertTrue(self.controller.telemetry_logger.is_logging())

            self.assertTrue(self.controller.land())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] == 0))

            # Logging should be stopped
            self.assertFalse(self.controller.telemetry_logger.is_logging())

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_csv_final_landing_packet_handled(self):
        """The final telemetry packet that triggers landing is handled correctly."""
        tmpdir = tempfile.mkdtemp()
        try:
            self.drone.command_watchdog_s = 0
            self.controller = DroneController(self.connection, telemetry_logger=TelemetryLogger(log_dir=tmpdir))
            self.feed_controller()

            self.assertTrue(self.connection.connect())
            self.assertTrue(self.controller.initialize())
            self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))

            self.assertTrue(self.controller.takeoff())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] >= 70))

            # Land - the landing packet (altitude=0) should be logged before stopping
            self.assertTrue(self.controller.land())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] == 0))

            csv_files = sorted(Path(tmpdir).glob("flight_*.csv"))
            self.assertEqual(len(csv_files), 1)

            with open(csv_files[0], newline='') as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            # Last row should have altitude 0 (or very low - simulator lands at ~1cm)
            if rows:
                last_row = rows[-1]
                self.assertLessEqual(int(last_row['altitude']), 2, f"Last row altitude: {last_row['altitude']}")

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_csv_separate_files_per_flight(self):
        """A subsequent flight creates a separate CSV file."""
        tmpdir = tempfile.mkdtemp()
        try:
            self.drone.command_watchdog_s = 0
            self.controller = DroneController(self.connection, telemetry_logger=TelemetryLogger(log_dir=tmpdir))
            self.feed_controller()

            self.assertTrue(self.connection.connect())
            self.assertTrue(self.controller.initialize())
            self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))

            # Flight 1
            self.assertTrue(self.controller.takeoff())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] >= 70))
            time.sleep(0.2)
            self.assertTrue(self.controller.land())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] == 0))

            csv_files_after_flight1 = sorted(Path(tmpdir).glob("flight_*.csv"))
            self.assertEqual(len(csv_files_after_flight1), 1)
            first_file = csv_files_after_flight1[0]

            # Flight 2
            self.assertTrue(self.controller.takeoff())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] >= 70))
            time.sleep(0.2)
            self.assertTrue(self.controller.land())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] == 0))

            csv_files_after_flight2 = sorted(Path(tmpdir).glob("flight_*.csv"))
            self.assertEqual(len(csv_files_after_flight2), 2, "Second flight should create second file")

            # Files should be different
            self.assertNotEqual(csv_files_after_flight2[0], csv_files_after_flight2[1])

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_csv_auto_landing_stops_logging(self):
        """Altitude <= 5 cm for >= 2 s causes the controller to stop logging."""
        tmpdir = tempfile.mkdtemp()
        try:
            self.drone.command_watchdog_s = 0          # takeoff has a 3 s sleep; would fire otherwise
            self.connection.keepalive_interval = 1000   # irrelevant once watchdog is off
            self.controller = DroneController(self.connection, telemetry_logger=TelemetryLogger(log_dir=tmpdir))

            self.feed_controller()
            self.assertTrue(self.connection.connect())
            self.assertTrue(self.controller.initialize())
            self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))

            self.assertTrue(self.controller.takeoff())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] >= 70))
            self.assertTrue(self.controller.telemetry_logger.is_logging())

            # Trigger emergency to force immediate descent - sim will drop rapidly
            self.assertTrue(self.connection.send_command("emergency"))
            self.assertTrue(self.wait_for(lambda s: s['altitude'] <= 5, timeout=10))

            # Give the controller's 2 s ground-check time to fire after drone settles
            deadline = time.time() + 4
            while self.controller.is_flying and time.time() < deadline:
                time.sleep(0.1)
            self.assertFalse(self.controller.is_flying)
            self.assertFalse(self.controller.telemetry_logger.is_logging())

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_csv_emergency_stop_stops_logging(self):
        """Emergency stop command stops CSV logging and prevents further writes."""
        tmpdir = tempfile.mkdtemp()
        try:
            self.drone.command_watchdog_s = 0
            self.controller = DroneController(self.connection, telemetry_logger=TelemetryLogger(log_dir=tmpdir))
            self.feed_controller()

            self.assertTrue(self.connection.connect())
            self.assertTrue(self.controller.initialize())
            self.assertTrue(self.wait_for(lambda s: 'battery' in s, timeout=3))

            # Takeoff starts logging
            self.assertTrue(self.controller.takeoff())
            self.assertTrue(self.wait_for(lambda s: s['altitude'] >= 70))
            # Wait for telemetry to accumulate several packets
            time.sleep(0.3)
            self.assertTrue(self.controller.telemetry_logger.is_logging())
            log_file_before = self.controller.telemetry_logger.get_current_log_file()
            self.assertIsNotNone(log_file_before)

            # Emergency stop should stop logging immediately
            self.assertTrue(self.controller.emergency_stop())
            self.assertFalse(self.controller.is_flying)
            self.assertFalse(self.controller.telemetry_logger.is_logging())

            # Verify CSV file is properly closed and exists
            self.assertTrue(log_file_before.exists())

            # Read the CSV and verify it has content
            with open(log_file_before, newline='') as f:
                reader = csv.reader(f)
                rows = list(reader)

            self.assertGreaterEqual(len(rows), 2, "CSV should have header + data rows")
            self.assertEqual(rows[0], TelemetryLogger.CSV_COLUMNS)

            # Verify telemetry AFTER emergency stop does NOT get appended
            # (emergency_stop() calls stop_logging() which clears the file handle)
            post_emergency_packet = {'battery': 80, 'altitude': 0, 'timestamp': time.time()}
            self.controller.update_from_telemetry(post_emergency_packet)

            with open(log_file_before, newline='') as f:
                reader = csv.reader(f)
                rows_after = list(reader)

            # Row count should be unchanged
            self.assertEqual(len(rows_after), len(rows), "No new rows should be added after emergency stop")

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
