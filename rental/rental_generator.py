"""Standalone rental/return event generator."""

import argparse
import csv
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from dotenv import load_dotenv

from common.control import publish_control, publish_event
from common.acceleration import accelerated_time, acceleration_factor

load_dotenv()


def run_rental_generator(vehicle_id, output_dir=None, runtime=None, seed=None):
    output_dir = output_dir or os.getcwd()
    os.makedirs(output_dir, exist_ok=True)
    runtime = runtime or float(os.getenv("RUNTIME", 3600))
    rental_parameter = "MTBR"
    return_parameter = "MTBRR"
    rental_mean_original = float(os.getenv(rental_parameter, 8.64))
    return_mean_original = float(os.getenv(return_parameter, 17.28))
    rental_mean = accelerated_time(rental_mean_original, rental_parameter)
    return_mean = accelerated_time(return_mean_original, return_parameter)
    rental_factor = acceleration_factor(rental_parameter)
    return_factor = acceleration_factor(return_parameter)
    rng = np.random.default_rng(seed)
    path = os.path.join(output_dir, f"rental_{vehicle_id}.csv")
    started = time.monotonic()
    rented = False
    next_event = rng.exponential(rental_mean)
    with open(path, "a", newline="") as stream:
        writer = csv.writer(stream)
        if stream.tell() == 0:
            writer.writerow([
                "elapsed_seconds_accelerated", "elapsed_seconds_original",
                "vehicle_id", "event", "time_parameter",
                "mean_seconds_original", "mean_seconds_accelerated",
                "acceleration_factor", "compression_factor",
            ])
        try:
            last_tick = time.monotonic()
            original_elapsed = 0.0
            while runtime <= 0 or time.monotonic() - started < runtime:
                now = time.monotonic()
                elapsed = now - started
                active_factor = return_factor if rented else rental_factor
                original_elapsed += (now - last_tick) * active_factor
                last_tick = now
                if elapsed >= next_event:
                    event = "rental_started" if not rented else "rental_returned"
                    action = "stop" if not rented else "start"
                    if not publish_control(vehicle_id, action, "rental", "rental-generator"):
                        # Keep the local rental state unchanged and retry on
                        # the next loop if the broker is temporarily down.
                        time.sleep(0.5)
                        continue
                    publish_event(vehicle_id, event, "rental", "rental-generator")
                    parameter = rental_parameter if event == "rental_started" else return_parameter
                    mean_original = rental_mean_original if event == "rental_started" else return_mean_original
                    mean_accelerated = rental_mean if event == "rental_started" else return_mean
                    factor = rental_factor if event == "rental_started" else return_factor
                    writer.writerow([
                        elapsed, original_elapsed, vehicle_id, event, parameter,
                        mean_original, mean_accelerated, factor, 1.0 / factor,
                    ])
                    stream.flush()
                    rented = not rented
                    next_event = elapsed + rng.exponential(return_mean if rented else rental_mean)
                time.sleep(0.2)
        except KeyboardInterrupt:
            if rented:
                publish_control(vehicle_id, "start", "rental", "rental-generator")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("vehicle_id")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--runtime", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    run_rental_generator(args.vehicle_id, args.output_dir, args.runtime, args.seed)


if __name__ == "__main__":
    main()
