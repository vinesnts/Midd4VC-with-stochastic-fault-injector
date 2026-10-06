import os
import json
import sys
import time
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import paho.mqtt.client as mqtt
from Midd4VCEngine import Midd4VCEngine
from common.control import ControlListener, publish_event
from datetime import datetime

TOPIC_VEHICLE_REGISTER = "vc/vehicle/+/register/request"
TOPIC_VEHICLE_UNREGISTER = "vc/vehicle/{vehicle_id}/unregister/request"
TOPIC_JOB_SUBMIT = "vc/client/+/job/submit"
TOPIC_JOB_ASSIGN = "vc/vehicle/{vehicle_id}/job/assign"
TOPIC_VEHICLE_RESULT = "vc/vehicle/+/job/result"


BROKER = os.getenv("MQTT_BROKER", "localhost")
PORT = int(os.getenv("MQTT_PORT", 1883))

class Midd4VCServer:
    def __init__(self, log_directory=None, with_job_timeout = True):
        self.engine = Midd4VCEngine(log_directory=log_directory, with_job_timeout=with_job_timeout)
        self.client = mqtt.Client("Midd4VCServer")
        self.engine.set_mqtt_client(self.client)

        self.client.on_connect = self.on_connect
        self.client.on_message = self._internal_on_message
        self.client.on_disconnect = self.on_disconnect
        self.on_message_callback = None
        self._control_reasons = set()

    def on_connect(self, client, userdata, flags, rc):
        # print(f"[{self.client._client_id.decode()}] Connected with result code {rc}")
        return

    def on_disconnect(self, client, userdata, rc):        
        if rc != 0:
            try:
                self.client.reconnect()
            except Exception as e:
                print(f"[Midd4VCServer] Reconnect failed: {e}")

    def start(self):
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;NULL;STARTING;NULL;NULL")
        self.client.connect(BROKER, PORT, 60)
        self.client.loop_start()

        self.client.subscribe(TOPIC_VEHICLE_REGISTER, qos=0); #print(f"[{self.client._client_id.decode()}] Subscribed to {TOPIC_VEHICLE_REGISTER}")
        self.client.subscribe(TOPIC_VEHICLE_UNREGISTER, qos=0); #print(f"[{self.client._client_id.decode()}] Subscribed to {TOPIC_VEHICLE_REGISTER}")
        self.client.subscribe(TOPIC_JOB_SUBMIT, qos=0); #print(f"[{self.client._client_id.decode()}] Subscribed to {TOPIC_JOB_SUBMIT}")
        self.client.subscribe(TOPIC_VEHICLE_RESULT, qos=0); # vehicle-to-manager results only
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;NULL;STARTED;NULL;NULL")
        publish_event("server", "started", "lifecycle", "server")

    def on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode()
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;{msg.topic};MESSAGE_RECEIVED;NULL;NULL")

            if msg.topic.endswith("register/request"):
                vehicle_info = json.loads(payload)
                self.engine.register_vehicle(vehicle_info)

            if msg.topic.endswith("unregister/request"):
                vehicle_info = json.loads(payload)
                cancelled_jobs = self.engine.unregister_vehicle(vehicle_info)
                for job_id in cancelled_jobs:
                    publish_event(
                        vehicle_info["vehicle_id"],
                        "task_discarded",
                        "vehicle_unavailable",
                        "server",
                        job_id=job_id,
                    )

            elif msg.topic.endswith("job/submit"):
                job = json.loads(payload)
                discarded_before = self.engine.jobs_discarded
                self.engine.submit_job(job)
                self.engine.tasks_received += 1
                if self.engine.jobs_discarded > discarded_before:
                    publish_event(
                        "server",
                        "job_discarded",
                        "queue_full",
                        "server",
                        job_id=job.get("job_id", ""),
                    )

            elif msg.topic.endswith("job/result"):
                result = json.loads(payload)
                result = self.engine.job_completed(result)
                if result:
                    self.engine.tasks_completed += 1

            if self.engine.log_tasks:
                with open(f'{self.engine.log_tasks}/tasks.csv', 'a') as file:
                    file.write(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")};{str(self.engine.tasks_received)};{str(self.engine.tasks_completed)};{str(self.engine.jobs_discarded)}\n')
        except Exception as e:
            print(f"[Midd4VCServer] Error processing message: {str(e)}")

    def stop(self):
        if not self.client.is_connected():
            return
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;NULL;STOPPING;NULL;NULL")
        self.client.loop_stop()
        self.client.disconnect()
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;NULL;STOPPED;NULL;NULL")
        publish_event("server", "stopped", "lifecycle", "server")

    def set_on_message_callback(self, callback):
        self.on_message_callback = callback

    def _internal_on_message(self, client, userdata, msg):
        if self.on_message_callback:
            self.on_message_callback(client, userdata, msg)
        else:
            self.on_message(client, userdata, msg)

    def get_server_status(self):
        return self.client.is_connected()

if __name__ == "__main__":
    args = sys.argv[1:]

    server = Midd4VCServer(
        log_directory=args[1] if len(args) > 1 and args[1] != 'no-timeout' else None,
        with_job_timeout='no-timeout' not in args)
    def handle_command(command):
        action = command.get("action")
        reason = command.get("reason", "external")
        if action == "stop":
            server._control_reasons.add(reason)
            if len(server._control_reasons) == 1:
                server.stop()
        elif action == "start":
            server._control_reasons.discard(reason)
            if not server._control_reasons and not server.client.is_connected():
                server.start()

    control = ControlListener("server", handle_command, source="server")
    control.start()
    server.start()
    compatibility_process = None
    if args and args[0] == "f" and len(args) > 1:
        root = Path(__file__).resolve().parents[1]
        compatibility_process = subprocess.Popen([
            sys.executable, str(root / "injector/fault_injector.py"),
            "server", "server", "--output-dir", args[1],
        ])
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
    finally:
        if compatibility_process is not None:
            compatibility_process.terminate()
        control.stop()
