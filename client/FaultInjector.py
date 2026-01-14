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
    print(f'[Midd4VCServer] Starting fault injection on vehicle {vehicle.client_id}...')
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
                        vehicle.stop()
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
                        vehicle.start()
                        vehicle_failure = np.random.exponential(scale=MTBVF) + g_time
                        fail = min(vehicle_failure, rental if rental is not None else float('inf'))
                        vehicle_repair = None
                elif rental_return is not None:
                    if g_time >= rental_return:
                        vehicle.start()
                        rental = np.random.exponential(scale=MTBR) + g_time
                        fail = min(rental, vehicle_failure if vehicle_failure is not None else float('inf'))
                        rental_return = None
                vehicle_status = int(vehicle.get_server_status())
                log_file.write((str(vehicle_status) + "\n"))
                if g_time >= RUNTIME:
                    break
        except KeyboardInterrupt:
            vehicle.stop()