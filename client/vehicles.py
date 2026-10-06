import os
import sys
import time
import threading
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Midd4VCClient import Midd4VCClient
from common.control import ControlListener, publish_event
from jobs import job_catalog
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

NV = int(os.getenv("NV", 40))

class Vehicle:
    def __init__(self, vehicle_id, model, make, year):
        self.vehicle_id = vehicle_id
        self.model = model
        self.make = make
        self.year = year

    def job_handler(self, job):
        function_name = job.get("function")
        args = job.get("args", [])

        started_at = time.time()
        try:
            func = job_catalog.JOBS_CATALOG.get(function_name)
            result_value = func(*args)
            return {
                "job_id": job["job_id"],
                'started_at': started_at,
                'finished_at': time.time(),
                "vehicle_id": self.vehicle_id,
                "result": result_value
            }
        except Exception as e:
            print(f"[Vehicle] Function execution failed: '{function_name}': {e}")
            return {
                "job_id": job.get("job_id", "unknown"),
                'started_at': started_at,
                "vehicle_id": self.vehicle_id,
                "error": f"Function execution failed: {str(e)}"
            }

def run_vehicle(vehicle_id, with_fault=False, folder=None):
    vehicle = Vehicle(vehicle_id=vehicle_id, model="ModelX", make="MakeY", year=2020)
    vc = Midd4VCClient(role="vehicle", client_id=vehicle.vehicle_id, model=vehicle.model, make=vehicle.make, year=vehicle.year)
    vc.set_job_handler(vehicle.job_handler)
    reasons = set()
    state_lock = threading.Lock()

    def handle_command(command):
        action = command.get("action")
        reason = command.get("reason", "external")
        with state_lock:
            if action == "stop":
                was_available = not reasons
                reasons.add(reason)
                if was_available:
                    vc.stop()
                    publish_event(vehicle_id, "stopped", reason, "vehicle")
            elif action == "start":
                reasons.discard(reason)
                if not reasons and not vc.get_server_status():
                    vc.start()
                    publish_event(vehicle_id, "started", reason, "vehicle")

    control = ControlListener(vehicle_id, handle_command, source="vehicle")
    control.start()
    vc.start()
    publish_event(vehicle_id, "started", "lifecycle", "vehicle")
    compatibility_processes = []
    if with_fault:
        root = Path(__file__).resolve().parents[1]
        for script in ("injector/fault_injector.py", "rental/rental_generator.py"):
            command = [sys.executable, str(root / script), vehicle_id]
            if script.startswith("injector"):
                command.insert(2, "vehicle")
            if folder:
                command.extend(["--output-dir", folder])
            compatibility_processes.append(subprocess.Popen(command))
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print(f"Stopping vehicle {vehicle_id}...")
    finally:
        for process in compatibility_processes:
            process.terminate()
        control.stop()
        if vc.get_server_status():
            vc.stop()

if __name__ == "__main__":
    args = sys.argv[1:]
    vehicle_ids = [f"veh{i}" for i in range(1, NV + 1)]  # 500 vehicles
    threads = []

    for vid in vehicle_ids:
        t = threading.Thread(target=run_vehicle, args=(vid,args and args[0] == 'f', args and args[1]))
        t.start()
        threads.append(t)

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("Stopping all vehicles...")
        # Threads não tem método stop, então precisa de outra estratégia para parar (por exemplo, flags)
