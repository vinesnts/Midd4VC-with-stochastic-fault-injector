"""Standalone stochastic server and vehicle fault injector."""

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


def _value(name, default):
    return float(os.getenv(name, default))


def run_fault_injector(target_id, target_type, output_dir=None, runtime=None, seed=None):
    output_dir = output_dir or os.getcwd()
    os.makedirs(output_dir, exist_ok=True)
    runtime = runtime or _value("RUNTIME", 3600)
    rng = np.random.default_rng(seed)
    failure_parameter = "MTMBF" if target_type == "server" else "MTBVF"
    repair_parameter = "MTBBR" if target_type == "server" else "MTBVR"
    failure_mean_original = _value(failure_parameter, 720 if target_type == "server" else 1080)
    repair_mean_original = _value(repair_parameter, 3.6 if target_type == "server" else 0.36)
    failure_mean = accelerated_time(failure_mean_original, failure_parameter)
    repair_mean = accelerated_time(repair_mean_original, repair_parameter)
    failure_factor = acceleration_factor(failure_parameter)
    repair_factor = acceleration_factor(repair_parameter)
    path = os.path.join(output_dir, f"fault_{target_type}_{target_id}.csv")
    started = time.monotonic()
    next_failure = rng.exponential(failure_mean)
    failed = False
    with open(path, "a", newline="") as stream:
        writer = csv.writer(stream)
        if stream.tell() == 0:
            writer.writerow([
                "elapsed_seconds_accelerated", "elapsed_seconds_original",
                "target_id", "target_type", "event", "reason", "time_parameter",
                "mean_seconds_original", "mean_seconds_accelerated",
                "acceleration_factor", "compression_factor",
            ])
        try:
            last_tick = time.monotonic()
            original_elapsed = 0.0
            while runtime <= 0 or time.monotonic() - started < runtime:
                now = time.monotonic()
                elapsed = now - started
                active_factor = repair_factor if failed else failure_factor
                original_elapsed += (now - last_tick) * active_factor
                last_tick = now
                if not failed and elapsed >= next_failure:
                    if not publish_control(target_id, "stop", "fault", "fault-injector"):
                        time.sleep(0.5)
                        continue
                    publish_event(target_id, "fault_started", "fault", "fault-injector")
                    writer.writerow([
                        elapsed, original_elapsed, target_id, target_type,
                        "fault_started", "fault", failure_parameter,
                        failure_mean_original, failure_mean, failure_factor,
                        1.0 / failure_factor,
                    ])
                    stream.flush()
                    failed = True
                    next_failure = elapsed + rng.exponential(repair_mean)
                elif failed and elapsed >= next_failure:
                    if not publish_control(target_id, "start", "fault", "fault-injector"):
                        time.sleep(0.5)
                        continue
                    publish_event(target_id, "fault_repaired", "fault", "fault-injector")
                    writer.writerow([
                        elapsed, original_elapsed, target_id, target_type,
                        "fault_repaired", "fault", repair_parameter,
                        repair_mean_original, repair_mean, repair_factor,
                        1.0 / repair_factor,
                    ])
                    stream.flush()
                    failed = False
                    next_failure = elapsed + rng.exponential(failure_mean)
                time.sleep(0.2)
        except KeyboardInterrupt:
            if failed:
                publish_control(target_id, "start", "fault", "fault-injector")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target_type", choices=["server", "vehicle"])
    parser.add_argument("target_id")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--runtime", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    run_fault_injector(args.target_id, args.target_type, args.output_dir, args.runtime, args.seed)


if __name__ == "__main__":
    main()
