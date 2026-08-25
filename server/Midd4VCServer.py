import os
import json
import sys
import time
import numpy as np
import paho.mqtt.client as mqtt
from Midd4VCEngine import Midd4VCEngine
from FaultInjector import inject_faults_on_broker
from datetime import datetime

TOPIC_VEHICLE_REGISTER = "vc/vehicle/+/register/request"
TOPIC_VEHICLE_UNREGISTER = "vc/vehicle/{vehicle_id}/unregister/request"
TOPIC_JOB_SUBMIT = "vc/client/+/job/submit"
TOPIC_JOB_ASSIGN = "vc/vehicle/{vehicle_id}/job/assign"
TOPIC_JOB_RESULT = "vc/client/+/job/result"


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
        self.client.subscribe(TOPIC_JOB_RESULT, qos=0); #print(f"[{self.client._client_id.decode()}] Subscribed to {TOPIC_JOB_RESULT}")
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;NULL;STARTED;NULL;NULL")

    def on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode()
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;{msg.topic};MESSAGE_RECEIVED;NULL;NULL")

            if msg.topic.endswith("register/request"):
                vehicle_info = json.loads(payload)
                self.engine.register_vehicle(vehicle_info)

            if msg.topic.endswith("unregister/request"):
                vehicle_info = json.loads(payload)
                self.engine.unregister_vehicle(vehicle_info)

            elif msg.topic.endswith("job/submit"):
                job = json.loads(payload)
                self.engine.submit_job(job)
                self.engine.tasks_received += 1

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
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;NULL;STOPPING;NULL;NULL")
        self.client.loop_stop()
        self.client.disconnect()
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;NULL;STOPPED;NULL;NULL")

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
        log_directory=args[1] if args and args[0] == 'f' else None,
        with_job_timeout='no-timeout' not in args)
    server.start()

    if args and args[0] == 'f' and args[1]:
        inject_faults_on_broker(server, args[1])
    else:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.stop()
