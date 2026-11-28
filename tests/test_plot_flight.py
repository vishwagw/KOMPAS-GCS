"""Tests for tools/plot_flight.py."""

import csv
import os
import sys
import tempfile
import unittest

import pytest

plt = pytest.importorskip("matplotlib", reason="plot_flight needs matplotlib")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools import plot_flight

COLUMNS = ["timestamp_iso", "timestamp_epoch", "battery", "altitude",
           "speed_x", "speed_y", "speed_z", "speed_total",
           "pitch", "roll", "yaw",
           "acceleration_x", "acceleration_y", "acceleration_z",
           "barometer", "time_of_flight", "motor_time",
           "temperature", "temperature_low"]


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for i, row in enumerate(rows):
            full = {c: "" for c in COLUMNS}
            full.update(row)
            writer.writerow(full)


class TestPlotFlight(unittest.TestCase):
    def test_output_png_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "flight.csv")
            png_path = os.path.join(tmp, "flight.png")
            rows = []
            for i in range(5):
                rows.append({
                    "timestamp_epoch": 1700000000 + 10 * i,
                    "altitude": 50 + 10 * i,
                    "battery": 90 - i,
                    "pitch": 1.5, "roll": -1.0, "yaw": 45.0,
                    "speed_total": 60.0,
                })
            _write_csv(csv_path, rows)

            plot_flight.main([csv_path, "-o", png_path])

            self.assertTrue(os.path.exists(png_path))
            self.assertGreater(os.path.getsize(png_path), 0)

    def test_empty_csv_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "flight.csv")
            _write_csv(csv_path, [])
            with self.assertRaises(SystemExit):
                plot_flight.main([csv_path, "-o", os.path.join(tmp, "x.png")])


if __name__ == "__main__":
    unittest.main()
