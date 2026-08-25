import os
import sys
from multiprocessing import Process
import uuid
import time
import random
from Midd4VCClient import Midd4VCClient

import numpy as np
from dotenv import load_dotenv

from datetime import datetime


load_dotenv()


MTBTA=float(os.getenv("MTBTA", 2.88))

class ApplicationClient:
    def __init__(self, client_id, role="client", log_directory=None):
        self.client = Midd4VCClient(role=role, client_id=client_id)
        self.client.set_result_handler(self.on_job_result)

        self.log_directory = log_directory

    def on_job_result(self, data):
        if self.log_directory:
            log_dir = f'{self.log_directory}/application.csv'
            with open(log_dir, 'a') as file:
                file.write(f"{data['job_id']};{data['vehicle_id']};{data['started_at']};{data['finished_at']}\n")

    def start(self):
        self.client.start()

    def stop(self):
        self.client.stop()

    def send_job_periodically(self, min_time=2, max_time=5, app_id=None):
        try:
            while True:

                job = {
                    "job_id": str(uuid.uuid4()),
                    "function": "math.factorial",
                    "args": [random.randint(1, 10)]
                }

                print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};TASK_REQUESTER;{app_id};SUBMITTING_JOB;{job['job_id']};NULL")
                self.client.submit_job(job)

                wait_time = random.uniform(min_time, max_time)
                time.sleep(wait_time)

        except KeyboardInterrupt:
            print(f"[{self.client.client_id}] stopping...")

    def send_job_exp(self, mean_time, app_id):
        try:
            time.sleep(2)
            while True:

                job = {
                    "job_id": str(uuid.uuid4()),
                    "function": "math.factorial",
                    "args": [random.randint(1, 10)]
                }

                print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};TASK_REQUESTER;{app_id};SUBMITTING_JOB;{job['job_id']};NULL")
                self.client.submit_job(job)

                wait_time = np.random.exponential(scale=mean_time)
                time.sleep(wait_time)

        except KeyboardInterrupt:
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};TASK_REQUESTER;{app_id};STOPPING;NULL;NULL")


def run_app(client_id, exponential=False, log_dir=None):
    app_client = ApplicationClient(client_id=client_id, log_directory=log_dir)
    app_client.start()
    if not exponential:
        app_client.send_job_periodically(min_time=5, max_time=10, app_id=client_id)
    else:
        app_client.send_job_exp(mean_time=MTBTA, app_id=client_id)
    app_client.stop()

if __name__ == "__main__":
    args = sys.argv[1:]
    client_ids = [f"Application_{i}" for i in range(1, 2)]
    processes = []

    for cid in client_ids:
        if args and args[0] == 'exp':
            p = Process(target=run_app, args=(cid, True, args[1] if len(args) > 1 else None))
        else:
            p = Process(target=run_app, args=(cid))

        p.start()
        processes.append(p)

    try:
        for p in processes:
            p.join()
    except KeyboardInterrupt:
        print("Stopping all applications...")
        for p in processes:
            p.terminate()
