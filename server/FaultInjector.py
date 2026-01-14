from datetime import datetime
import os
import time
import numpy as np

from dotenv import load_dotenv

load_dotenv()

MTMBF=float(os.getenv("MTMBF", 720))
MTBBR=float(os.getenv("MTBBR", 3.6))
RUNTIME=float(os.getenv("RUNTIME", 3600))

def inject_faults_on_broker(server, folder=None):
    print('[Midd4VCServer] Starting fault injection on broker...')
    if not folder:
        folder = os.getcwd()
    os.makedirs(folder, exist_ok=True)
    with open(f"{folder}/broker_status_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", "a") as log_file:
        try:
            g_time = 0
            repair = None
            failure = np.random.exponential(scale=MTMBF)
            while True:
                time.sleep(1)
                g_time += 1
                if failure is not None:
                    if g_time >= failure:
                        server.stop()
                        repair = np.random.exponential(scale=MTBBR) + g_time
                        failure = None
                elif repair is not None:
                    if g_time >= repair:
                        server.start()
                        failure = np.random.exponential(scale=MTMBF) + g_time
                        repair = None
                broker_status = int(server.get_server_status())
                log_file.write((str(broker_status) + "\n"))
                if g_time >= RUNTIME:
                    break
        except KeyboardInterrupt:
            server.stop()