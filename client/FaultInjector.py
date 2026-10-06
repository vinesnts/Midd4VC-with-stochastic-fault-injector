"""Compatibility wrapper for the standalone vehicle fault injector."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from injector.fault_injector import run_fault_injector


def inject_faults_on_vehicle(vehicle, folder=None):
    return run_fault_injector(vehicle.client_id, "vehicle", folder)
