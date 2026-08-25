import os
import json
import time

from jass import least_loaded #, round_robin

from dotenv import load_dotenv


from datetime import datetime

load_dotenv()


ASSIGNMENT_STRATEGIES = {
    "least_loaded": least_loaded.assign_jobs_least_loaded,
    # "round_robin": round_robin.assign_jobs_round_robin
    # new assinment strategies
}

TQS = int(os.getenv("TQS", 40))

class Midd4VCEngine:
    def __init__(self, log_directory = None, with_job_timeout = True):
        self.vehicles = {}  # vehicle_id -> info
        self.jobs_queue = []  # list of pending jobs
        self.jobs_in_progress = {}  # job_id -> vehicle_id
        self.vehicle_load = {}
        self.log_directory = log_directory

        self.jobs_discarded = 0

        # add load balancing
        self.job_assignments = {}
        self.job_timeout = 30 # job timeout in seconds
        self.with_job_timeout = with_job_timeout

        self.vehicle_order = []
        self.next_vehicle_index = 0

        self.tasks_received = 0
        self.tasks_completed = 0
        self.log_tasks = log_directory

        self.mqtt_client = None
        self.current_assignment_strategy = "least_loaded" # (default: least loaded strategy)
        # self.current_assignment_strategy = "round_robin"

    def set_mqtt_client(self, mqtt_client):
        self.mqtt_client = mqtt_client

    def register_vehicle(self, vehicle_info):
        vehicle_id = vehicle_info["vehicle_id"]

        if vehicle_id in self.vehicles:
            self.try_assign_jobs()
            return

        self.vehicles[vehicle_id] = vehicle_info
        self.vehicle_order.append(vehicle_id) # new
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;NULL;VEHICLE_REGISTERED;{vehicle_id};NULL")
        self.mqtt_client.publish(f"vc/vehicle/{vehicle_id}/register/response", "Registration Successful", qos=1)
        self.try_assign_jobs()

    def unregister_vehicle(self, vehicle_info):
        vehicle_id = vehicle_info["vehicle_id"]

        if vehicle_id not in self.vehicles:
            print(f"[Midd4VCServer] Vehicle {vehicle_id} already unregistered.")
            self.try_assign_jobs()
            return

        jobs_killed = [job_id for job_id, v_id in self.jobs_in_progress.items() if v_id == vehicle_id]
        for job in jobs_killed:
            del self.jobs_in_progress[job]

        del self.vehicles[vehicle_id]

    def submit_job(self, job):
        if "job_id" not in job or "function" not in job or "client_id" not in job:
            print(f"[Midd4VCServer] Invalid job format: {job}")
            return
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;NULL;JOB_RECEIVED;{job['job_id']};NULL")

        if len(self.jobs_queue) >= TQS:
            self.jobs_discarded += 1
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER_QUEUE;{len(self.jobs_queue)}/{TQS};JOB_DISCARDED;{job['job_id']};NULL")
            self.try_assign_jobs()
            return
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;NULL;JOB_SUBMITTED;{job['job_id']};NULL")
        self.jobs_queue.append(job)
        self.try_assign_jobs()

    def job_completed(self, result):
        job_id = result["job_id"]
        vehicle_id = result["vehicle_id"]
        client_id = result.get("client_id")
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};SERVER;{vehicle_id};JOB_COMPLETED;{job_id};NULL")
        self.jobs_queue = [j for j in self.jobs_queue if j['job_id'] != job_id]

        success = False
        if job_id in self.jobs_in_progress:
            del self.jobs_in_progress[job_id]
            success = True
        # else:
        #     print(f"[Midd4VCServer] job {job_id} not found in progress for vehicle {vehicle_id}")

        if job_id in self.job_assignments:
            del self.job_assignments[job_id]

         # Update vehicle load counter
        self.vehicle_load[vehicle_id] = self.vehicle_load.get(vehicle_id, 0) + 1

        if self.mqtt_client and client_id:
            # topic = f"vc/application/{client_id}/job/result"
            topic = f"vc/client/{client_id}/job/result"
            result['finished_at'] = time.time()
            self.mqtt_client.publish(topic, json.dumps(result), qos=1)
        #else:
            #print(f"[Midd4VCServer] MQTT client not set or app_id missing. Cannot send result for job {job_id}")

        self.try_assign_jobs()
        return success

    def check_job_timeouts(self):
        now = time.time()
        expired_jobs = []

        if self.with_job_timeout:
            for job_id, info in list(self.job_assignments.items()):
                if now - info["assigned_at"] > self.job_timeout:
                    print(f"[Midd4VC] Job {job_id} timed out. Re-enqueueing.")
                    expired_jobs.append(job_id)

        for job_id in expired_jobs:
            vehicle_id = self.job_assignments[job_id]["vehicle_id"]

            # Re-queue the job
            job = self.job_assignments[job_id]["job_data"]
            self.jobs_queue.insert(0, job)

            # Remove from records
            del self.jobs_in_progress[job_id]
            del self.job_assignments[job_id]

    def set_assignment_strategy(self, strategy_name):
        if strategy_name in ASSIGNMENT_STRATEGIES:
            self.current_assignment_strategy = strategy_name
            # print(f"[Midd4VCEngine] Assignment strategy set to '{strategy_name}'.")
        # else:
        #     print(f"[Midd4VCEngine] Unknown assignment strategy '{strategy_name}', keeping current.")

    def try_assign_jobs(self):
        self.check_job_timeouts()

        strategy_name = self.current_assignment_strategy
        strategy_func = ASSIGNMENT_STRATEGIES.get(strategy_name)

        if strategy_func:
            strategy_func(self)
        else:
            print(f"[Midd4VCEngine] No valid assignment strategy found. Using 'least_loaded' fallback.")
            # ASSIGNMENT_STRATEGIES["least_loaded"](self)

