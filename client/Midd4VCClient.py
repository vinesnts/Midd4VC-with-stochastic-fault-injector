import json
import time
from multiprocessing import Process
import threading
import os
from uuid import uuid4
from jobs import job_catalog

import paho.mqtt.client as mqtt

from datetime import datetime

TOPIC_VEHICLE_REGISTER = "vc/vehicle/{vehicle_id}/register/request"
TOPIC_VEHICLE_UNREGISTER = "vc/vehicle/{vehicle_id}/unregister/request"
TOPIC_JOB_ASSIGN = "vc/vehicle/{vehicle_id}/job/assign"
TOPIC_JOB_SUBMIT = "vc/client/{client_id}/job/submit"
TOPIC_CLIENT_RESULT = "vc/client/{client_id}/job/result"
TOPIC_VEHICLE_RESULT = "vc/vehicle/{vehicle_id}/job/result"

BROKER = os.getenv("MQTT_BROKER", "localhost")
PORT = int(os.getenv("MQTT_PORT", 1883))

class Midd4VCClient:
    def __init__(self, role, client_id, model=None, make=None, year=None):
        self.client_id = client_id
        self.role = role
        self.client = mqtt.Client(client_id=self.client_id, clean_session=False)
        self.client.on_message = self._internal_on_message
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)

        self.result_handler = None
        self.job_handler = None
        self.on_message_callback = None

        self.processed_jobs = set()
        self.running = False
        self.jobs_in_progress = dict()
        self.cancelled_jobs = set()
        self.jobs_lock = threading.Lock()

        if self.role == "vehicle":
            self.info = {
                "vehicle_id": self.client_id,
                "model": model or "generic",
                "make": make or "generic",
                "year": year or 2000,
            }

    def set_result_handler(self, handler_fn):
        self.result_handler = handler_fn

    def set_job_handler(self, handler_fn):
        self.job_handler = handler_fn

    def set_on_message_callback(self, callback):
        self.on_message_callback = callback

    def start(self):
        print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")};VEHICLE;{self.client_id};STARTING;NULL;NULL')
        try:
            self.client.connect(BROKER, PORT, 60)
        except Exception as e:
            print(f"[{self.role.capitalize()} {self.client_id}] Error connecting to MQTT: {e}")
            return

        self.client.loop_start()
        self.running = True

        if self.role == "client":
            self.client.subscribe(TOPIC_CLIENT_RESULT.format(client_id=self.client_id), qos=0)

        elif self.role == "vehicle":
            self.client.subscribe(TOPIC_JOB_ASSIGN.format(vehicle_id=self.client_id), qos=0)
            time.sleep(1)
            self.register()
        print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")};VEHICLE;{self.client_id};STARTED;NULL;NULL')

    def stop(self):
        print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")};VEHICLE;{self.client_id};STOPPING;NULL;NULL')
        with self.jobs_lock:
            self.cancelled_jobs.update(self.jobs_in_progress)

        self.running = False
        self.client.publish(TOPIC_VEHICLE_UNREGISTER, json.dumps(self.info), qos=0)
        self.client.loop_stop()
        self.client.disconnect()
        print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")};VEHICLE;{self.client_id};STOPPED;NULL;NULL')

    def register(self):
        if self.role == "vehicle":
            self.client.publish(TOPIC_VEHICLE_REGISTER, json.dumps(self.info), qos=0)

    def submit_job(self, job):
        if "job_id" not in job:
            job["job_id"] = str(uuid4())
        job["client_id"] = self.client_id
        topic = TOPIC_JOB_SUBMIT.format(client_id=self.client_id)
        if not self.client.is_connected():
            try:
                self.client.reconnect()
            except Exception:
                return job["job_id"]
        self.client.publish(topic, json.dumps(job), qos=0)
        return job["job_id"]

    def _internal_on_message(self, client, userdata, msg):
        if self.on_message_callback:
            self.on_message_callback(client, userdata, msg)
        else:
            self.on_message(client, userdata, msg)

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            print(f"[{self.role.capitalize()} {self.client_id}] Invalid JSON message: {payload}")
            return

        if self.role == "client":
            if self.result_handler:
                result = {
                    'job_id': data['job_id'],
                    'vehicle_id': data['vehicle_id'],
                    'started_at': data['started_at'],
                    'finished_at': data['finished_at'],
                    'result': data['result']
                }
                # self.result_handler(data)
                self.result_handler(result)
            else:
                print(f"[Client {self.client_id}] Result received: {data}")
        elif self.role == "vehicle":
            process = threading.Thread(
                target=self.execute_job,
                args=(data,),
                daemon=True
            )
            self.jobs_in_progress[data['job_id']] = process
            process.start()

    def execute_job(self, job):
        if not self.client.is_connected():
            print(f"[Vehicle {self.client_id}] Cannot execute job. MQTT client not connected.")
            return

        job_id = job.get("job_id")
        client_id = job.get("client_id")

        if job_id is None:
            print(f"[Vehicle {self.client_id}] Job received without job_id, ignoring.")
            return

        with self.jobs_lock:
            if job_id in self.cancelled_jobs or not self.running:
                print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")};VEHICLE;{self.client_id};DISCARDED_TASK;{job_id};vehicle_unavailable')
                self.jobs_in_progress.pop(job_id, None)
                return

        if job_id in self.processed_jobs:
            print(f"[Vehicle {self.client_id}] Duplicate job {job_id} ignored.")
            return

        self.processed_jobs.add(job_id)

        if client_id is None:
            print(f"[Vehicle {self.client_id}] Warning: client_id missing in job. Result will not be sent.")
            return

        try:
            started_at = time.time()
            if self.job_handler:
                print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE;{self.client_id};STARTED_TASK;{job.get('job_id')};NULL")
                result = self.job_handler(job)
            else:
                function_name = job.get("function")
                args = job.get("args", [])
                try:
                    func = job_catalog.JOBS_CATALOG.get(function_name)
                    if func is None:
                        raise ValueError(f"Unknown job function: {function_name}")
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE;{self.client_id};STARTED_TASK;{job.get('job_id')};{func}")
                    result_value = func(*args)
                    result = {
                        "job_id": job_id,
                        "started_at": started_at,
                        "finished_at": time.time(),
                        "vehicle_id": self.client_id,
                        "result": result_value,
                    }
                except Exception as e:
                    result = {
                        "job_id": job_id,
                        "started_at": started_at,
                        "finished_at": time.time(),
                        "vehicle_id": self.client_id,
                        "error": f"Error executing job: {str(e)}"
                    }

            # Handlers are allowed to return only their domain result.  The
            # transport envelope is completed here so the manager can route
            # the response without knowing anything about its contents.
            result.setdefault("job_id", job_id)
            result.setdefault("vehicle_id", self.client_id)
            result.setdefault("client_id", client_id)
            result.setdefault("started_at", started_at)
            result.setdefault("finished_at", time.time())

            with self.jobs_lock:
                cancelled = job_id in self.cancelled_jobs
            if cancelled:
                print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE;{self.client_id};DISCARDED_TASK;{job_id};vehicle_unavailable")
                return
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE;{self.client_id};FINISHED_TASK;{job_id};NULL")
            self.client.publish(TOPIC_VEHICLE_RESULT.format(vehicle_id=self.client_id), json.dumps(result), qos=0)
        finally:
            with self.jobs_lock:
                self.jobs_in_progress.pop(job_id, None)
                self.cancelled_jobs.discard(job_id)

    def _on_connect(self, client, userdata, flags, rc):
        # print(f"[{self.role.capitalize()} {self.client_id}] Connected to broker with code: {rc}")
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE;{self.client_id};CONNECTING_TO_BROKER;NULL;NULL")
        if rc == 0:
            if self.role == "client":
                self.client.subscribe(TOPIC_CLIENT_RESULT.format(client_id=self.client_id), qos=0)
            elif self.role == "vehicle":
                self.client.subscribe(TOPIC_JOB_ASSIGN.format(vehicle_id=self.client_id), qos=0)
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE;{self.client_id};CONNECTED_TO_BROKER;NULL;NULL")
        else:
            print(f"[{self.role.capitalize()} {self.client_id}] Connection error: code: {rc}")

    def _on_disconnect(self, client, userdata, rc):
        if self.running and rc != 0:
            try:
                self.client.reconnect()
            except Exception as e:
                print(f"[{self.role.capitalize()} {self.client_id}] Reconnection failed: {e}")

    def get_server_status(self):
        return self.client.is_connected()
