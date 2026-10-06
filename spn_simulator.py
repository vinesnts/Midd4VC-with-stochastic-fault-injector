"""Live, lightweight event-driven approximation of the Midd4VC SPN.

The simulator follows the places and transition intent in the Mercury model:
tasks can remain in ``TR`` while a working vehicle has up to ``NTV`` tasks,
which is the behavior that the MQTT implementation currently does not expose.
It is used only for visualization and does not alter the real experiments.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass

import numpy as np


@dataclass
class SPNParameters:
    MTBTA: float = 0.05
    MTBTS: float = 0.5
    MTBVF: float = 3000.0
    MTBVR: float = 1.0
    MTBRR: float = 24.0
    MTBR: float = 6.0
    NV: int = 20
    NTV: int = 3
    NM: int = 1
    TQS: int = 6
    MTBMR: float = 10.0
    MTBMF: float = 2000.0


class SPNSimulator:
    """Discrete-event visualization model for the supplied Midd4VC SPN."""

    PLACE_NAMES = ("AVD", "MD", "MU", "TQ", "TR", "VA", "VD", "VRD", "VW")

    def __init__(self, parameters: SPNParameters | None = None, seed: int | None = None):
        self.parameters = parameters or SPNParameters()
        self.rng = np.random.default_rng(seed)
        self._sequence = itertools.count()
        self.reset()

    def reset(self) -> None:
        p = self.parameters
        self.simulation_time = 0.0
        self.running = False
        self._events: list[tuple[float, int, str, str | None]] = []
        self.vehicles = {
            f"v{i}": {"status": "available", "tasks": 0}
            for i in range(1, p.NV + 1)
        }
        self.manager_up = True
        self.places = {
            "AVD": p.NV,
            "MD": 0,
            "MU": p.NM,
            "TQ": 0,
            "TR": 0,
            "VA": 0,
            "VD": 0,
            "VRD": 0,
            "VW": 0,
        }
        self.metrics = {
            "arrivals": 0,
            "started": 0,
            "completed": 0,
            "discarded": 0,
            "faults": 0,
            "repairs": 0,
            "rentals": 0,
            "returns": 0,
        }
        self.last_events: list[str] = []
        self._apply_immediate_transitions()
        self._schedule("arrival", None, self._sample(p.MTBTA))
        self._schedule("manager_failure", None, self._sample(p.MTBMF))
        for vehicle_id in self.vehicles:
            self._schedule("vehicle_failure", vehicle_id, self._sample(p.MTBVF))
            self._schedule("rental_start", vehicle_id, self._sample(p.MTBR))

    def configure(self, parameters: SPNParameters) -> None:
        self.parameters = parameters
        self.reset()

    def start(self) -> None:
        self.running = True

    def pause(self) -> None:
        self.running = False

    def current_marking(self) -> dict[str, int]:
        """Return the current place marking after refreshing derived places."""
        self._refresh_places()
        return dict(self.places)

    def _sample(self, mean: float) -> float:
        return max(1e-6, float(self.rng.exponential(max(mean, 1e-6))))

    def _schedule(self, event: str, vehicle_id: str | None, delay: float) -> None:
        heapq.heappush(
            self._events,
            (self.simulation_time + max(0.0, delay), next(self._sequence), event, vehicle_id),
        )

    def advance(self, elapsed_seconds: float) -> list[str]:
        """Advance simulated time and return human-readable transition firings."""
        if not self.running:
            return []
        target = self.simulation_time + max(0.0, elapsed_seconds)
        fired: list[str] = []
        while self._events and self._events[0][0] <= target:
            event_time, _, event, vehicle_id = heapq.heappop(self._events)
            self.simulation_time = event_time
            fired.extend(self._fire(event, vehicle_id))
        self.simulation_time = target
        self._apply_immediate_transitions()
        self.last_events = (fired + self.last_events)[-8:]
        return fired

    def _fire(self, event: str, vehicle_id: str | None) -> list[str]:
        p = self.parameters
        messages: list[str] = []
        if event == "arrival":
            self.metrics["arrivals"] += 1
            if self.manager_up and self.places["TQ"] < p.TQS:
                self.places["TQ"] += 1
                messages.append("TA: task arrived")
            else:
                self.metrics["discarded"] += 1
                messages.append("TD: task discarded")
            self._schedule("arrival", None, self._sample(p.MTBTA))
        elif event == "task_complete" and vehicle_id:
            vehicle = self.vehicles.get(vehicle_id)
            if vehicle and vehicle["tasks"] > 0:
                vehicle["tasks"] -= 1
                self.places["TR"] -= 1
                if vehicle["tasks"] == 0:
                    vehicle["status"] = "available"
                self.metrics["completed"] += 1
                messages.append(f"TS: task completed on {vehicle_id}")
        elif event == "manager_failure":
            if self.manager_up:
                self.manager_up = False
                self.places["MU"] = 0
                self.places["MD"] = p.NM
                # Mercury's TD transition is immediately enabled when MU=0;
                # all tokens waiting in TQ are discarded at the failure.
                if self.places["TQ"]:
                    self.metrics["discarded"] += self.places["TQ"]
                    self.places["TQ"] = 0
                messages.append("MF: manager failed")
            self._schedule("manager_repair", None, self._sample(p.MTBMR))
        elif event == "manager_repair":
            if not self.manager_up:
                self.manager_up = True
                self.places["MD"] = 0
                self.places["MU"] = p.NM
                messages.append("MR: manager repaired")
            self.metrics["repairs"] += 1
            self._schedule("manager_failure", None, self._sample(p.MTBMF))
        elif event == "vehicle_failure" and vehicle_id:
            vehicle = self.vehicles.get(vehicle_id)
            if self.manager_up and vehicle and vehicle["status"] in {"available", "working"}:
                if vehicle["tasks"]:
                    self.metrics["discarded"] += vehicle["tasks"]
                    self.places["TR"] -= vehicle["tasks"]
                    vehicle["tasks"] = 0
                vehicle["status"] = "faulted"
                self.metrics["faults"] += 1
                messages.append(f"VCF: {vehicle_id} failed")
            self._schedule("vehicle_repair", vehicle_id, self._sample(p.MTBVR))
        elif event == "vehicle_repair" and vehicle_id:
            vehicle = self.vehicles.get(vehicle_id)
            if vehicle and vehicle["status"] == "faulted":
                vehicle["status"] = "available"
                self.metrics["repairs"] += 1
                messages.append(f"VCR: {vehicle_id} repaired")
            self._schedule("vehicle_failure", vehicle_id, self._sample(p.MTBVF))
        elif event == "rental_start" and vehicle_id:
            vehicle = self.vehicles.get(vehicle_id)
            if vehicle and vehicle["status"] in {"available", "working"}:
                if vehicle["tasks"]:
                    self.metrics["discarded"] += vehicle["tasks"]
                    self.places["TR"] -= vehicle["tasks"]
                    vehicle["tasks"] = 0
                vehicle["status"] = "rented"
                self.metrics["rentals"] += 1
                messages.append(f"VR/VR2: {vehicle_id} rented")
            self._schedule("rental_return", vehicle_id, self._sample(p.MTBRR))
        elif event == "rental_return" and vehicle_id:
            vehicle = self.vehicles.get(vehicle_id)
            if vehicle and vehicle["status"] == "rented":
                vehicle["status"] = "available"
                self.metrics["returns"] += 1
                messages.append(f"VRT: {vehicle_id} returned")
            self._schedule("rental_start", vehicle_id, self._sample(p.MTBR))
        self._apply_immediate_transitions()
        return messages

    def _apply_immediate_transitions(self) -> None:
        p = self.parameters
        if self.manager_up:
            for vehicle in self.vehicles.values():
                if vehicle["status"] == "available":
                    vehicle["status"] = "available"
        for vehicle_id, vehicle in self.vehicles.items():
            if vehicle["status"] != "available":
                continue
            while self.places["TQ"] > 0 and vehicle["tasks"] < p.NTV and self.manager_up:
                self.places["TQ"] -= 1
                self.places["TR"] += 1
                vehicle["tasks"] += 1
                self.metrics["started"] += 1
                self._schedule("task_complete", vehicle_id, self._sample(p.MTBTS))
            if vehicle["tasks"]:
                vehicle["status"] = "working"
        # Working vehicles can receive more tasks, which is the SPN behavior
        # missing from the current MQTT vehicle implementation.
        if self.manager_up:
            for vehicle_id, vehicle in self.vehicles.items():
                while self.places["TQ"] > 0 and vehicle["status"] == "working" and vehicle["tasks"] < p.NTV:
                    self.places["TQ"] -= 1
                    self.places["TR"] += 1
                    vehicle["tasks"] += 1
                    self.metrics["started"] += 1
                    self._schedule("task_complete", vehicle_id, self._sample(p.MTBTS))
        self._refresh_places()

    def _refresh_places(self) -> None:
        self.places["MU"] = self.parameters.NM if self.manager_up else 0
        self.places["MD"] = 0 if self.manager_up else self.parameters.NM
        self.places["VA"] = sum(v["status"] == "available" for v in self.vehicles.values()) if self.manager_up else 0
        self.places["VW"] = sum(v["status"] == "working" for v in self.vehicles.values())
        self.places["VD"] = sum(v["status"] == "faulted" for v in self.vehicles.values())
        self.places["VRD"] = sum(v["status"] == "rented" for v in self.vehicles.values())
        self.places["AVD"] = sum(
            v["status"] == "available" for v in self.vehicles.values()
        ) if not self.manager_up else 0
