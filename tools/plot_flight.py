#!/usr/bin/env python3
"""Plot a KOMPAS flight log CSV as stacked subplots sharing the x-axis.

Usage:
    python tools/plot_flight.py logs/flight_20261127_101500.csv
    python tools/plot_flight.py logs/flight_20261127_101500.csv -o flight.png

matplotlib is optional; install it with `pip install matplotlib` (see
requirements-dev.txt).
"""

import argparse
import csv
import sys

try:
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover - exercised in tests via importorskip
    plt = None

# (subplot title, y label, columns to draw in that subplot)
SUBPLOTS = [
    ("Altitude", "cm", ["altitude"]),
    ("Battery", "%", ["battery"]),
    ("Attitude", "degrees", ["pitch", "roll", "yaw"]),
    ("Speed", "cm/s", ["speed_total"]),
]


def load_rows(path):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError as e:
        raise SystemExit("error: cannot read %s: %s" % (path, e))
    if not rows:
        raise SystemExit("error: %s has no data rows" % path)
    return rows


def column(rows, name):
    """Return the column as floats, None for missing/unparseable cells."""
    values = []
    for row in rows:
        raw = (row.get(name) or "").strip()
        try:
            values.append(float(raw))
        except ValueError:
            values.append(None)
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Plot a KOMPAS flight log CSV.")
    parser.add_argument("csv", help="flight log CSV, e.g. logs/flight_20261127_101500.csv")
    parser.add_argument("-o", "--output", help="save the chart to this file instead of showing it")
    args = parser.parse_args(argv)

    if plt is None:
        raise SystemExit("matplotlib is required for plotting: pip install matplotlib")

    rows = load_rows(args.csv)

    epochs = column(rows, "timestamp_epoch")
    start = next((e for e in epochs if e is not None), 0.0)
    times = [None if e is None else e - start for e in epochs]

    axes_list = []
    fig = None
    for index, (title, ylabel, cols) in enumerate(SUBPLOTS):
        present = [c for c in cols if any((r.get(c) or "").strip() for r in rows)]
        if not present:
            continue
        if fig is None:
            fig, ax = plt.subplots()
        else:
            ax = fig.add_subplot(len(SUBPLOTS), 1, index + 1, sharex=axes_list[0] if axes_list else None)
        for c in present:
            xs = [t for t, v in zip(times, column(rows, c)) if t is not None and v is not None]
            ys = [v for t, v in zip(times, column(rows, c)) if t is not None and v is not None]
            ax.plot(xs, ys, label=c)
        ax.set_ylabel(ylabel)
        ax.legend(loc="best", fontsize="small")
        if not axes_list:
            ax.set_title("Flight log: %s" % args.csv)
        axes_list.append(ax)

    if not axes_list:
        raise SystemExit("error: %s has no plottable columns" % args.csv)

    axes_list[-1].set_xlabel("Time since takeoff (s)")
    fig.tight_layout()

    if args.output:
        fig.savefig(args.output)
        print("saved %s" % args.output)
    else:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
