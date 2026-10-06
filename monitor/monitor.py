"""External availability and performance monitor."""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()


class Monitor:
    def __init__(self, output_dir, vehicle_ids, interval=1.0):
        self.output_dir = output_dir
        self.vehicle_ids = vehicle_ids
        self.interval = interval
        self.started = {}
        self.completed = 0
        self.submitted = 0
        self.discarded = 0
        self.rtt_total = 0.0
        self.rtt_count = 0
        self.per_vehicle = defaultdict(int)
        self.connected = False
        self.available_vehicles = set()
        self.server_available = False
        self.client = mqtt.Client(client_id="midd4vc-monitor")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = rc == 0
        if self.connected:
            client.subscribe("vc/#", qos=0)

    def _on_message(self, client, userdata, message):
        if message.topic.endswith("unregister/request"):
            try:
                vehicle_id = json.loads(message.payload.decode()).get("vehicle_id")
                self.available_vehicles.discard(vehicle_id)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        elif message.topic.endswith("register/request"):
            try:
                vehicle_id = json.loads(message.payload.decode()).get("vehicle_id")
                if vehicle_id:
                    self.available_vehicles.add(vehicle_id)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        elif "/control/" in message.topic and message.topic.endswith("/event"):
            try:
                event = json.loads(message.payload.decode())
                target_id = event.get("target_id")
                if target_id == "server":
                    event_name = event.get("event")
                    if event_name in {"started", "stopped"}:
                        self.server_available = event_name == "started"
                    elif event_name == "job_discarded":
                        self.discarded += 1
                elif event.get("event") == "stopped":
                    self.available_vehicles.discard(target_id)
                elif event.get("event") == "started":
                    self.available_vehicles.add(target_id)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        try:
            data = json.loads(message.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        topic = message.topic
        if topic.endswith("job/submit"):
            self.submitted += 1
            if data.get("job_id"):
                self.started[data["job_id"]] = time.time()
        elif topic.endswith("job/result"):
            self.completed += 1
            job_id = data.get("job_id")
            if job_id in self.started:
                self.rtt_total += time.time() - self.started.pop(job_id)
                self.rtt_count += 1
            vehicle_id = data.get("vehicle_id")
            if vehicle_id:
                self.per_vehicle[vehicle_id] += 1
        elif "JOB_DISCARDED" in message.payload.decode(errors="ignore"):
            self.discarded += 1

    def run(self, runtime=None):
        os.makedirs(self.output_dir, exist_ok=True)
        status_path = os.path.join(self.output_dir, "availability.csv")
        metrics_path = os.path.join(self.output_dir, "performance.csv")
        timestamp_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        legacy_streams = {
            "broker": open(os.path.join(self.output_dir, f"broker_status_{timestamp_suffix}.csv"), "w")
        }
        legacy_streams.update({
            vehicle_id: open(os.path.join(self.output_dir, f"vehicle_{vehicle_id}_{timestamp_suffix}.csv"), "w")
            for vehicle_id in self.vehicle_ids
        })
        self.client.connect(os.getenv("MQTT_BROKER", "localhost"), int(os.getenv("MQTT_PORT", 1883)), 60)
        self.client.loop_start()
        started = time.monotonic()
        with open(status_path, "w", newline="") as status_stream, open(metrics_path, "w", newline="") as metrics_stream:
            status = csv.writer(status_stream)
            metrics = csv.writer(metrics_stream)
            status.writerow(["timestamp", "broker_status"] + self.vehicle_ids)
            metrics.writerow(["timestamp", "jobs_submitted", "jobs_completed", "jobs_discarded", "avg_rtt", "messages_per_vehicle"])
            while runtime is None or time.monotonic() - started < runtime:
                timestamp = datetime.now().isoformat(timespec="microseconds")
                row = [timestamp, int(self.server_available or self.connected)] + [
                    int(vehicle_id in self.available_vehicles) for vehicle_id in self.vehicle_ids
                ]
                status.writerow(row)
                legacy_streams["broker"].write(f"{int(self.server_available or self.connected)}\n")
                for vehicle_id in self.vehicle_ids:
                    legacy_streams[vehicle_id].write(f"{int(vehicle_id in self.available_vehicles)}\n")
                metrics.writerow([timestamp, self.submitted, self.completed, self.discarded,
                                  self.rtt_total / self.rtt_count if self.rtt_count else 0,
                                  "; ".join(f"{k}:{v}" for k, v in sorted(self.per_vehicle.items()))])
                status_stream.flush()
                metrics_stream.flush()
                for stream in legacy_streams.values():
                    stream.flush()
                time.sleep(self.interval)
        for stream in legacy_streams.values():
            stream.close()
        self.client.loop_stop()
        self.client.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--vehicles", nargs="*", default=[])
    parser.add_argument("--runtime", type=float, default=None)
    parser.add_argument("--interval", type=float, default=float(os.getenv("MONITOR_INTERVAL", 1)))
    args = parser.parse_args()
    Monitor(args.output_dir, args.vehicles, args.interval).run(args.runtime)


if __name__ == "__main__":
    main()
