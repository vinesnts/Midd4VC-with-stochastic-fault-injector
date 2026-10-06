import os
import time
import numpy as np
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.acceleration import accelerated_time

from dotenv import load_dotenv

from datetime import datetime

load_dotenv()


MTBTS=float(os.getenv("MTBTS", 30))
MTBTS_ACCELERATED=accelerated_time(MTBTS, "MTBTS")

def add(a, b):
    return a + b

def multiply(a, b):
    return a * b

def factorial(n):
    service_time = np.random.exponential(scale=MTBTS_ACCELERATED)
    time.sleep(service_time)
    # if n == 0 or n == 1:
    #     return 1
    # return n * factorial(n - 1)

def fibonacci(n):
    if n <= 0:
        return 0
    elif n == 1:
        return 1
    else:
        return fibonacci(n - 1) + fibonacci(n - 2)


def classification(n):
    if n <= 0:
        return 0
    elif n == 1:
        return 1
    else:
        return fibonacci(n - 1) + fibonacci(n - 2)
