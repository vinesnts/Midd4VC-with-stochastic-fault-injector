from datetime import datetime
import os
import time
import numpy as np

from dotenv import load_dotenv


load_dotenv()


MTBVF=float(os.getenv("MTBVF", 1080))
MTBVR=float(os.getenv("MTBVR", 0.36))
MTBR=float(os.getenv("MTBR", 8.64))
MTBRR=float(os.getenv("MTBRR", 17.28))
RUNTIME=float(os.getenv("RUNTIME", 3600))

def inject_faults_on_vehicle(vehicle, folder=None):
    if not folder:
        folder = os.getcwd()
    os.makedirs(folder, exist_ok=True)
    with open(f"{folder}/vehicle_{vehicle.client_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", "a") as log_file:
        try:
            g_time = 0
            vehicle_repair = None
            vehicle_failure = np.random.exponential(scale=MTBVF)
            rental_return = None
            rental = np.random.exponential(scale=MTBR)
            fail = min(vehicle_failure, rental)
            while True:
                time.sleep(1)
                g_time += 1
                if fail is not None:
                    if g_time >= fail:
                        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE_FAULT_INJECTOR;NULL;STOPPING;{vehicle.client_id};NULL")
                        vehicle.stop()
                        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE_FAULT_INJECTOR;NULL;STOPPED;{vehicle.client_id};NULL")
                        if fail == vehicle_failure:
                            vehicle_repair = np.random.exponential(scale=MTBVR) + g_time
                            fail = None
                            vehicle_failure = None
                        else:
                            rental_return = np.random.exponential(scale=MTBRR) + g_time
                            fail = None
                            rental = None
                elif vehicle_repair is not None:
                    if g_time >= vehicle_repair:
                        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE_FAULT_INJECTOR;NULL;STARTING;{vehicle.client_id};NULL")
                        vehicle.start()
                        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE_FAULT_INJECTOR;NULL;STARTED;{vehicle.client_id};NULL")
                        vehicle_failure = np.random.exponential(scale=MTBVF) + g_time
                        rental = rental \
                            if rental is not None and g_time < rental \
                            else np.random.exponential(scale=MTBR) + g_time
                        fail = min(vehicle_failure, rental)
                        vehicle_repair = None
                elif rental_return is not None:
                    if g_time >= rental_return:
                        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE_FAULT_INJECTOR;NULL;STARTING;{vehicle.client_id};NULL")
                        vehicle.start()
                        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};VEHICLE_FAULT_INJECTOR;NULL;STARTED;{vehicle.client_id};NULL")
                        rental = np.random.exponential(scale=MTBR) + g_time
                        vehicle_failure = vehicle_failure \
                            if vehicle_failure is not None and g_time < vehicle_failure \
                            else np.random.exponential(scale=MTBVF) + g_time
                        fail = min(rental, vehicle_failure)
                        rental_return = None
                vehicle_status = int(vehicle.get_server_status())
                log_file.write((str(vehicle_status) + "\n"))
                # if g_time >= RUNTIME:
                #     break
        except KeyboardInterrupt:
            vehicle.stop()