"""Interactive Tkinter dashboard for launching and observing Midd4VC experiments.

The dashboard is deliberately separate from the simulation services.  It starts
the existing ``runme.sh`` batch launcher and observes the same MQTT control and
job topics used by the monitor.  All MQTT callbacks are handed to Tk's main
thread through a queue so the UI remains responsive while experiments run.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from spn_simulator import SPNParameters, SPNSimulator

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError as exc:  # pragma: no cover - depends on the host OS
    raise SystemExit("Tkinter is required. Install python3-tk on Debian/Ubuntu.") from exc

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover - dependency installation issue
    raise SystemExit("paho-mqtt is required. Install the project requirements.") from exc


ROOT = Path(__file__).resolve().parent
JOB_ID_RE = re.compile(r"/vehicle/([^/]+)/job/assign$")
RUN_START_RE = re.compile(r"Run experiment: (\d+)/(\d+) - (.+)$")
RUN_FINISHED_RE = re.compile(r"Run experiment: (\d+)/(\d+): Finished$")

PARAMETER_TYPES = {
    "JOB_TYPE": str,
    "IMAGE_FOLDER": str,
    "IMAGE_MODEL_PATH": str,
    "IMAGE_DAMAGED_PROBABILITY": float,
    "MTMBF": float,
    "MTBBR": float,
    "MTBVF": float,
    "MTBVR": float,
    "MTBTS": float,
    "MTBR": float,
    "MTBRR": float,
    "NV": int,
    "MTBTA": float,
    "AF_MTMBF": float,
    "AF_MTBBR": float,
    "AF_MTBVF": float,
    "AF_MTBVR": float,
    "AF_MTBR": float,
    "AF_MTBRR": float,
    "AF_MTBTA": float,
    "AF_MTBTS": float,
    "TQS": int,
    "RUNTIME": float,
    "N_EXPERIMENTS": int,
    "MQTT_PORT": int,
}

PARAMETER_LABELS = {
    "JOB_TYPE": "Job type",
    "IMAGE_FOLDER": "Image folder",
    "IMAGE_MODEL_PATH": "Keras model path",
    "IMAGE_DAMAGED_PROBABILITY": "Damaged probability",
    "MTMBF": "Server failure mean (s)",
    "MTBBR": "Server repair mean (s)",
    "MTBVF": "Vehicle failure mean (s)",
    "MTBVR": "Vehicle repair mean (s)",
    "MTBTS": "Task service mean (s)",
    "MTBR": "Rental interval mean (s)",
    "MTBRR": "Rental duration mean (s)",
    "NV": "Vehicles",
    "MTBTA": "Task arrival mean (s)",
    "AF_MTMBF": "AF server failure",
    "AF_MTBBR": "AF server repair",
    "AF_MTBVF": "AF vehicle failure",
    "AF_MTBVR": "AF vehicle repair",
    "AF_MTBR": "AF rental interval",
    "AF_MTBRR": "AF rental duration",
    "AF_MTBTA": "AF task arrival",
    "AF_MTBTS": "AF task service",
    "TQS": "Queue size",
    "RUNTIME": "Experiment runtime (s)",
    "N_EXPERIMENTS": "Experiments",
    "MQTT_PORT": "MQTT port",
}

PARAMETER_DEFAULTS = {
    "JOB_TYPE": "math.factorial",
    "IMAGE_DAMAGED_PROBABILITY": "0.5",
    "MTBTS": "30",
    "AF_MTMBF": "1",
    "AF_MTBBR": "1",
    "AF_MTBVF": "1",
    "AF_MTBVR": "1",
    "AF_MTBR": "1",
    "AF_MTBRR": "1",
    "AF_MTBTA": "1",
    "AF_MTBTS": "1",
    "MQTT_PORT": "1883",
}


def load_env_file(path: Path) -> dict[str, str]:
    """Read the small KEY=VALUE subset used by the shell launcher."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


@dataclass
class VehicleState:
    vehicle_id: str
    rented: bool = False
    faulted: bool = False
    online: bool = False
    running_jobs: set[str] = field(default_factory=set)
    last_event: str = "waiting for registration"
    last_event_at: str = ""

    @property
    def status(self) -> str:
        if self.faulted:
            return "FAULT"
        if self.rented:
            return "RENTED"
        if self.running_jobs:
            return "WORKING"
        if self.online:
            return "AVAILABLE"
        return "OFFLINE"


class SimulationDashboard:
    """Tkinter application and MQTT observer."""

    COLORS = {
        "AVAILABLE": "#2e9d62",
        "WORKING": "#3277c7",
        "RENTED": "#d58a18",
        "FAULT": "#c74747",
        "OFFLINE": "#7a8491",
    }

    def __init__(self, root: tk.Tk, *, launch_on_start: bool = False) -> None:
        self.root = root
        self.root.title("Midd4VC Simulation Dashboard")
        self.root.geometry("1280x820")
        self.root.minsize(980, 680)

        env = load_env_file(ROOT / ".env")
        self.env_values = env
        self.broker_host = os.getenv("MQTT_BROKER", env.get("MQTT_BROKER", "localhost"))
        self.broker_port = int(os.getenv("MQTT_PORT", env.get("MQTT_PORT", "1883")))
        self.vehicle_count = int(env.get("NV", "2"))
        self.runtime_seconds = float(env.get("RUNTIME", "1800"))
        self.experiment_count = int(env.get("N_EXPERIMENTS", "16"))
        self.vehicle_ids = [f"veh{i}" for i in range(1, self.vehicle_count + 1)]

        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.process_lock = threading.Lock()
        self.stop_deadline: float | None = None
        self.batch_started_at: float | None = None
        self.experiment_started_at: float | None = None
        self.clock_elapsed = 0.0
        self.clock_running = False
        self.current_experiment = 0
        self.current_experiment_dir = ""
        self.mqtt_connected = False
        self.closing = False
        self.manager_failed = False
        self.manager_online = False
        self.vehicles = {vid: VehicleState(vid) for vid in self.vehicle_ids}
        self.running_jobs: dict[str, str] = {}
        self.pending_jobs: set[str] = set()
        self.discarded_jobs: set[str] = set()
        self.job_experiment: dict[str, int] = {}
        self.completed_jobs: set[str] = set()
        self.counters = defaultdict(int)
        self.parameter_vars: dict[str, tk.StringVar] = {}
        self.spn = SPNSimulator()
        self.spn_last_tick = time.monotonic()
        self.spn_transition_flash: dict[str, float] = {}
        self.spn_monitoring = False
        self.show_counters_var = tk.BooleanVar(value=True)
        self.show_event_log_var = tk.BooleanVar(value=True)
        self.show_spn_log_var = tk.BooleanVar(value=True)
        self.counters_window: tk.Toplevel | None = None
        self.event_log_window: tk.Toplevel | None = None
        self.spn_log_window: tk.Toplevel | None = None
        self.show_partial_metrics_var = tk.BooleanVar(value=True)
        self.partial_metrics_labels: dict[str, ttk.Label] = {}
        self.partial_started_at: float | None = None
        self.partial_last_update = time.monotonic()
        self.partial_observed_seconds = 0.0
        self.partial_vehicle_available_area = 0.0
        self.partial_manager_available_area = 0.0
        self.partial_submitted = 0
        self.partial_completed = 0
        self.partial_discarded = 0
        self.spn_place_labels: dict[str, tk.Label] = {}
        self.spn_metric_labels: dict[str, ttk.Label] = {}

        self._build_widgets()
        self._reset_spn()
        self._connect_mqtt()
        self.root.after(100, self._drain_events)
        self.root.after(500, self._refresh_clock_and_view)
        self.root.after(100, self._tick_spn)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        if launch_on_start:
            self.root.after(700, self.start_batch)

    # ------------------------------------------------------------------ UI
    def _build_widgets(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(6, weight=1)

        self.start_button = ttk.Button(toolbar, text="Start batch", command=self.start_batch)
        self.start_button.grid(row=0, column=0, padx=(0, 6))
        self.stop_button = ttk.Button(toolbar, text="Stop batch", command=self.stop_batch, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(0, 6))
        ttk.Button(toolbar, text="Reset live state", command=lambda: self.clear_observation("manual button")).grid(row=0, column=2, padx=(0, 12))

        self.connection_label = ttk.Label(toolbar, text="MQTT: connecting…")
        self.connection_label.grid(row=0, column=3, padx=(0, 14))
        self.batch_label = ttk.Label(toolbar, text="Simulation: idle")
        self.batch_label.grid(row=0, column=4, padx=(0, 14))
        self.clock_label = ttk.Label(toolbar, text="Time: 00:00:00")
        self.clock_label.grid(row=0, column=5, padx=(0, 14))
        self.path_label = ttk.Label(toolbar, text="", anchor="e")
        self.path_label.grid(row=0, column=6, sticky="e")
        view_button = ttk.Menubutton(toolbar, text="View")
        view_button.grid(row=0, column=7, padx=(10, 0))
        view_menu = tk.Menu(view_button, tearoff=False)
        view_menu.add_checkbutton(label="Counters", variable=self.show_counters_var, command=self._toggle_counters)
        view_menu.add_checkbutton(label="Live event log", variable=self.show_event_log_var, command=self._toggle_event_log)
        view_menu.add_checkbutton(label="SPN transition log", variable=self.show_spn_log_var, command=self._toggle_spn_log)
        view_menu.add_checkbutton(label="Partial metrics", variable=self.show_partial_metrics_var, command=self._toggle_partial_metrics)
        view_menu.add_separator()
        view_menu.add_command(label="Float counters", command=self._float_counters)
        view_menu.add_command(label="Float live event log", command=self._float_event_log)
        view_menu.add_command(label="Float SPN transition log", command=self._float_spn_log)
        view_button.configure(menu=view_menu)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self.monitor_tab = ttk.Frame(self.notebook, padding=4)
        self.spn_tab = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(self.monitor_tab, text="Experiment monitor")
        self.notebook.add(self.spn_tab, text="SPN live model")
        self.monitor_tab.columnconfigure(0, weight=1)
        self.monitor_tab.rowconfigure(3, weight=1)

        self._build_parameter_panel()

        self.manager_frame = ttk.LabelFrame(self.monitor_tab, text="Manager / server", padding=8)
        self.manager_frame.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 6))
        self.manager_frame.columnconfigure(1, weight=1)
        self.manager_status = tk.Label(self.manager_frame, text="  ONLINE  ", bg=self.COLORS["AVAILABLE"], fg="white", font=("TkDefaultFont", 10, "bold"))
        self.manager_status.grid(row=0, column=0, padx=(0, 12))
        self.manager_event_label = ttk.Label(self.manager_frame, text="Waiting for server lifecycle events")
        self.manager_event_label.grid(row=0, column=1, sticky="w")

        self.vehicle_frame = ttk.LabelFrame(self.monitor_tab, text="Vehicles", padding=8)
        self.vehicle_frame.grid(row=2, column=0, sticky="nsew", padx=4, pady=(0, 6))
        self.vehicle_frame.columnconfigure(0, weight=1)
        self.vehicle_frame.rowconfigure(0, weight=1)
        self.vehicle_canvas = tk.Canvas(self.vehicle_frame, background="#f5f7fa", highlightthickness=0, height=230)
        self.vehicle_canvas.grid(row=0, column=0, sticky="nsew")

        self.lower_paned = ttk.PanedWindow(self.monitor_tab, orient="horizontal")
        self.lower_paned.grid(row=3, column=0, sticky="nsew", padx=4, pady=(0, 4))

        self.counter_frame = ttk.LabelFrame(self.lower_paned, text="Live counters", padding=8)
        counter_frame = self.counter_frame
        counter_frame.columnconfigure(1, weight=1)
        self.counter_labels: dict[str, ttk.Label] = {}
        counter_names = [
            ("task_submitted", "Tasks submitted"),
            ("task_started", "Tasks started"),
            ("success", "Successful task results"),
            ("task_failed", "Task failures / discards"),
            ("rental", "Rentals started"),
            ("return", "Returns"),
            ("vehicle_fault", "Vehicle failures"),
            ("vehicle_repair", "Vehicle repairs"),
            ("manager_fault", "Manager failures"),
            ("manager_repair", "Manager repairs"),
        ]
        for row, (key, label) in enumerate(counter_names):
            ttk.Label(counter_frame, text=label + ":").grid(row=row, column=0, sticky="w", padx=(0, 18), pady=2)
            # Reserve enough space for multi-digit totals.  Without a fixed
            # width, a label initially sized for "0" can visually clip values
            # such as 10 or 100 while the counter itself remains correct.
            value = ttk.Label(
                counter_frame,
                text="0",
                width=8,
                anchor="e",
                font=("TkDefaultFont", 10, "bold"),
            )
            value.grid(row=row, column=1, sticky="e", pady=2)
            self.counter_labels[key] = value
        self.docked_counter_labels = dict(self.counter_labels)
        self.lower_paned.add(counter_frame, weight=0)

        self.partial_metrics_frame = ttk.LabelFrame(self.lower_paned, text="Partial metrics", padding=8)
        self.partial_metrics_frame.columnconfigure(1, weight=1)
        metric_names = [
            ("vehicle_available", "Vehicle availability"),
            ("vehicle_unavailable", "Vehicle unavailability"),
            ("manager_available", "Manager availability"),
            ("manager_unavailable", "Manager unavailability"),
            ("discard_proportion", "Discard proportion"),
            ("completion_proportion", "Completion proportion"),
            ("partial_observed", "Observed time"),
            ("partial_jobs", "Submitted / completed / discarded"),
        ]
        for row, (key, label) in enumerate(metric_names):
            ttk.Label(self.partial_metrics_frame, text=label + ":").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=2)
            value = ttk.Label(self.partial_metrics_frame, text="--", width=24, anchor="e", font=("TkDefaultFont", 10, "bold"))
            value.grid(row=row, column=1, sticky="e", pady=2)
            self.partial_metrics_labels[key] = value
        self.lower_paned.add(self.partial_metrics_frame, weight=0)

        self.event_log_frame = ttk.LabelFrame(self.lower_paned, text="Live event log", padding=8)
        log_frame = self.event_log_frame
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=12, width=80, state="disabled", wrap="none", font=("TkFixedFont", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.docked_log_text = self.log_text
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.lower_paned.add(log_frame, weight=1)
        self._build_spn_tab()

    def _build_parameter_panel(self) -> None:
        """Create editable controls without changing the checked-in .env."""
        frame = ttk.LabelFrame(self.monitor_tab, text="Simulation parameters (used at next start)", padding=6)
        frame.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 6))
        for column in range(6):
            frame.columnconfigure(column, weight=1)

        ordered_keys = [
            "JOB_TYPE", "IMAGE_FOLDER", "IMAGE_MODEL_PATH", "IMAGE_DAMAGED_PROBABILITY",
            "MTMBF", "MTBBR", "MTBVF", "MTBVR", "MTBTS", "MTBR",
            "MTBRR", "NV", "MTBTA", "AF_MTMBF", "AF_MTBBR", "AF_MTBVF",
            "AF_MTBVR", "AF_MTBR", "AF_MTBRR", "AF_MTBTA", "AF_MTBTS",
            "TQS", "RUNTIME", "N_EXPERIMENTS", "MQTT_PORT",
        ]
        for index, key in enumerate(ordered_keys):
            row, column = divmod(index, 6)
            ttk.Label(frame, text=PARAMETER_LABELS[key]).grid(
                row=row * 2, column=column, sticky="w", padx=4, pady=(0, 1)
            )
            default = self.env_values.get(key, PARAMETER_DEFAULTS.get(key, ""))
            variable = tk.StringVar(value=default)
            self.parameter_vars[key] = variable
            ttk.Entry(frame, textvariable=variable, width=14).grid(
                row=row * 2 + 1, column=column, sticky="ew", padx=4, pady=(0, 4)
            )

        broker_row = ((len(ordered_keys) + 5) // 6) * 2
        broker_frame = ttk.Frame(frame)
        broker_frame.grid(row=broker_row, column=0, columnspan=5, sticky="ew", padx=4, pady=(2, 0))
        broker_frame.columnconfigure(1, weight=1)
        ttk.Label(broker_frame, text="MQTT broker host").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.parameter_vars["MQTT_BROKER"] = tk.StringVar(value=self.broker_host)
        ttk.Entry(broker_frame, textvariable=self.parameter_vars["MQTT_BROKER"], width=24).grid(row=0, column=1, sticky="ew")
        self.broker_probe_label = ttk.Label(broker_frame, text="not checked")
        self.broker_probe_label.grid(row=0, column=2, padx=(12, 6))
        ttk.Button(broker_frame, text="Check broker", command=self.check_broker).grid(row=0, column=3, padx=4)
        ttk.Button(frame, text="Reload .env values", command=self._reload_parameters).grid(row=broker_row, column=5, padx=4, pady=(2, 0), sticky="e")

    def _build_spn_tab(self) -> None:
        """Build the live visualization of the Mercury Midd4VC SPN."""
        self.spn_tab.columnconfigure(0, weight=1)
        self.spn_tab.rowconfigure(1, weight=1)
        self.spn_tab.rowconfigure(2, weight=0)

        actions = ttk.Frame(self.spn_tab)
        actions.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(actions, text="Start SPN", command=self._start_spn).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text="Pause SPN", command=self._pause_spn).pack(side="left", padx=5)
        ttk.Button(actions, text="Reset SPN", command=self._reset_spn).pack(side="left", padx=5)
        self.spn_status_label = ttk.Label(actions, text="SPN: paused")
        self.spn_status_label.pack(side="left", padx=16)
        ttk.Label(actions, text="Uses the selected experiment parameters").pack(side="right")

        self.spn_paned = ttk.PanedWindow(self.spn_tab, orient="vertical")
        self.spn_paned.grid(row=1, column=0, sticky="nsew")
        spn_view = ttk.Frame(self.spn_paned)
        spn_view.rowconfigure(0, weight=1)
        spn_view.columnconfigure(0, weight=1)
        self.spn_canvas = tk.Canvas(
            spn_view, background="white", highlightthickness=0, height=650,
            scrollregion=(0, 0, 1000, 760),
        )
        self.spn_canvas.grid(row=0, column=0, sticky="nsew")
        vertical_scroll = ttk.Scrollbar(spn_view, orient="vertical", command=self.spn_canvas.yview)
        vertical_scroll.grid(row=0, column=1, sticky="ns")
        horizontal_scroll = ttk.Scrollbar(spn_view, orient="horizontal", command=self.spn_canvas.xview)
        horizontal_scroll.grid(row=1, column=0, sticky="ew")
        self.spn_canvas.configure(
            xscrollcommand=horizontal_scroll.set,
            yscrollcommand=vertical_scroll.set,
        )

        self.spn_log_frame = ttk.LabelFrame(self.spn_paned, text="SPN transition log", padding=5)
        log_frame = self.spn_log_frame
        self.spn_log = tk.Text(log_frame, height=5, state="disabled", wrap="none", font=("TkFixedFont", 9))
        self.spn_log.pack(fill="both", expand=True)
        self.docked_spn_log = self.spn_log
        self.spn_paned.add(spn_view, weight=5)
        self.spn_paned.add(log_frame, weight=1)

    def _toggle_counters(self) -> None:
        if self.show_counters_var.get():
            if self.counters_window is not None:
                self._dock_counters()
                return
            self.lower_paned.add(self.counter_frame, weight=0)
        else:
            self.lower_paned.forget(self.counter_frame)

    def _toggle_event_log(self) -> None:
        if self.show_event_log_var.get():
            if self.event_log_window is not None:
                self._dock_event_log()
                return
            self.lower_paned.add(self.event_log_frame, weight=1)
        else:
            self.lower_paned.forget(self.event_log_frame)

    def _toggle_spn_log(self) -> None:
        if self.show_spn_log_var.get():
            if self.spn_log_window is not None:
                self._dock_spn_log()
                return
            self.spn_paned.add(self.spn_log_frame, weight=1)
        else:
            self.spn_paned.forget(self.spn_log_frame)

    def _toggle_partial_metrics(self) -> None:
        if self.show_partial_metrics_var.get():
            self.lower_paned.add(self.partial_metrics_frame, weight=0)
        else:
            self.lower_paned.forget(self.partial_metrics_frame)

    def _float_counters(self) -> None:
        if self.counters_window is not None and self.counters_window.winfo_exists():
            self.counters_window.deiconify()
            self.counters_window.lift()
            return
        self.show_counters_var.set(False)
        self._toggle_counters()
        window = tk.Toplevel(self.root)
        self.counters_window = window
        window.title("Midd4VC - Live counters")
        window.geometry("280x330")
        window.minsize(220, 220)
        ttk.Button(window, text="Dock counters", command=self._dock_counters).pack(anchor="ne", padx=6, pady=6)
        frame = ttk.LabelFrame(window, text="Live counters", padding=8)
        frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        frame.columnconfigure(1, weight=1)
        counter_names = [
            ("task_submitted", "Tasks submitted"), ("task_started", "Tasks started"),
            ("success", "Successful task results"), ("task_failed", "Task failures / discards"),
            ("rental", "Rentals started"), ("return", "Returns"),
            ("vehicle_fault", "Vehicle failures"), ("vehicle_repair", "Vehicle repairs"),
            ("manager_fault", "Manager failures"), ("manager_repair", "Manager repairs"),
        ]
        self.counter_labels = {}
        for row, (key, label) in enumerate(counter_names):
            ttk.Label(frame, text=label + ":").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=2)
            value = ttk.Label(frame, text="0", width=8, anchor="e", font=("TkDefaultFont", 10, "bold"))
            value.grid(row=row, column=1, sticky="e", pady=2)
            self.counter_labels[key] = value
        window.protocol("WM_DELETE_WINDOW", self._dock_counters)

    def _dock_counters(self) -> None:
        if self.counters_window is not None:
            self.counters_window.destroy()
            self.counters_window = None
        self.counter_labels = self.docked_counter_labels
        self.show_counters_var.set(True)
        self._toggle_counters()

    @staticmethod
    def _copy_text(source: tk.Text, target: tk.Text) -> None:
        source.configure(state="normal")
        content = source.get("1.0", "end-1c")
        source.configure(state="disabled")
        target.configure(state="normal")
        target.delete("1.0", "end")
        target.insert("1.0", content)
        target.see("end")
        target.configure(state="disabled")

    def _float_event_log(self) -> None:
        if self.event_log_window is not None and self.event_log_window.winfo_exists():
            self.event_log_window.deiconify()
            self.event_log_window.lift()
            return
        self.show_event_log_var.set(False)
        self._toggle_event_log()
        window = tk.Toplevel(self.root)
        self.event_log_window = window
        window.title("Midd4VC - Live event log")
        window.geometry("760x360")
        window.minsize(360, 180)
        ttk.Button(window, text="Dock event log", command=self._dock_event_log).pack(anchor="ne", padx=6, pady=6)
        frame = ttk.Frame(window, padding=6)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text_widget = tk.Text(frame, wrap="none", state="disabled", font=("TkFixedFont", 9))
        text_widget.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text_widget.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text_widget.configure(yscrollcommand=scroll.set)
        self._copy_text(self.docked_log_text, text_widget)
        self.log_text = text_widget
        window.protocol("WM_DELETE_WINDOW", self._dock_event_log)

    def _dock_event_log(self) -> None:
        if self.event_log_window is not None:
            self._copy_text(self.log_text, self.docked_log_text)
            self.event_log_window.destroy()
            self.event_log_window = None
        self.log_text = self.docked_log_text
        self.show_event_log_var.set(True)
        self._toggle_event_log()

    def _float_spn_log(self) -> None:
        if self.spn_log_window is not None and self.spn_log_window.winfo_exists():
            self.spn_log_window.deiconify()
            self.spn_log_window.lift()
            return
        self.show_spn_log_var.set(False)
        self._toggle_spn_log()
        window = tk.Toplevel(self.root)
        self.spn_log_window = window
        window.title("Midd4VC - SPN transition log")
        window.geometry("760x260")
        window.minsize(360, 150)
        ttk.Button(window, text="Dock SPN log", command=self._dock_spn_log).pack(anchor="ne", padx=6, pady=6)
        frame = ttk.Frame(window, padding=6)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text_widget = tk.Text(frame, wrap="none", state="disabled", font=("TkFixedFont", 9))
        text_widget.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text_widget.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text_widget.configure(yscrollcommand=scroll.set)
        self._copy_text(self.docked_spn_log, text_widget)
        self.spn_log = text_widget
        window.protocol("WM_DELETE_WINDOW", self._dock_spn_log)

    def _dock_spn_log(self) -> None:
        if self.spn_log_window is not None:
            self._copy_text(self.spn_log, self.docked_spn_log)
            self.spn_log_window.destroy()
            self.spn_log_window = None
        self.spn_log = self.docked_spn_log
        self.show_spn_log_var.set(True)
        self._toggle_spn_log()

    def _read_spn_parameters(self) -> SPNParameters:
        def number(key: str, integer: bool = False) -> float | int:
            raw = self.parameter_vars[key].get().strip()
            if not raw:
                raise ValueError(f"Experiment parameter {key} cannot be empty")
            try:
                value = int(raw) if integer else float(raw)
            except ValueError as exc:
                raise ValueError(f"Experiment parameter {key} must be numeric") from exc
            if value <= 0:
                raise ValueError(f"Experiment parameter {key} must be greater than zero")
            return value

        def accelerated(key: str) -> float:
            original = float(number(key))
            factor = float(number(f"AF_{key}"))
            return original / factor

        return SPNParameters(
            MTBTA=accelerated("MTBTA"),
            MTBTS=accelerated("MTBTS"),
            MTBVF=accelerated("MTBVF"),
            MTBVR=accelerated("MTBVR"),
            MTBRR=accelerated("MTBRR"),
            MTBR=accelerated("MTBR"),
            NV=int(number("NV", integer=True)),
            # NTV and NM are SPN structural constants, not experiment inputs.
            NTV=3,
            NM=1,
            TQS=int(number("TQS", integer=True)),
            MTBMR=accelerated("MTBBR"),
            MTBMF=accelerated("MTMBF"),
        )

    def _reset_spn(self) -> None:
        try:
            self.spn.configure(self._read_spn_parameters())
        except ValueError as exc:
            messagebox.showerror("Invalid SPN parameters", str(exc))
            return
        self.spn_last_tick = time.monotonic()
        self.spn_monitoring = False
        self.spn.pause()
        self.spn_status_label.configure(text="SPN: paused")
        self._append_spn_log("SPN reset to the configured initial marking.")
        self._draw_spn()

    def _start_spn(self) -> None:
        try:
            self.spn.configure(self._read_spn_parameters())
        except ValueError as exc:
            messagebox.showerror("Invalid SPN parameters", str(exc))
            return
        self.spn.start()
        self.spn_monitoring = True
        self.spn_last_tick = time.monotonic()
        self.spn_status_label.configure(text="SPN: running")
        self._append_spn_log("SPN started.")
        self._draw_spn()

    def _pause_spn(self) -> None:
        self.spn.pause()
        self.spn_monitoring = False
        self.spn_status_label.configure(text="SPN: paused")
        self._append_spn_log("SPN paused.")

    def _append_spn_log(self, text: str) -> None:
        if not hasattr(self, "spn_log"):
            return
        self.spn_log.configure(state="normal")
        self.spn_log.insert("end", f"{self.spn.simulation_time:9.3f}s  {text}\n")
        self.spn_log.see("end")
        self.spn_log.configure(state="disabled")

    def _tick_spn(self) -> None:
        now = time.monotonic()
        self.spn_last_tick = now
        if self.spn_monitoring:
            self._draw_spn()
        self.root.after(100, self._tick_spn)

    def _draw_spn(self) -> None:
        if not hasattr(self, "spn_canvas"):
            return
        canvas = self.spn_canvas
        canvas.delete("all")
        canvas.configure(scrollregion=(0, 0, 1000, 760))
        marking = self._live_spn_marking()
        # Fixed coordinates intentionally follow the supplied Mercury drawing.
        # The viewport is scrollable rather than scaling the model into a
        # different layout.
        places = {
            "TQ": (170, 245), "TR": (340, 245), "VW": (438, 245),
            "VA": (802, 245), "AVD": (920, 245), "VD": (602, 160),
            "VRD": (602, 340), "MU": (870, 525), "MD": (870, 650),
        }
        transitions = {
            "TA": (70, 245, 18, 38, True), "TAV": (255, 245, 11, 38, False),
            "TD": (170, 122, 11, 38, False),
            "TS": (340, 363, 18, 38, True), "VART": (438, 40, 11, 38, False),
            "VDART": (438, 440, 11, 38, False), "VCF2": (510, 160, 18, 38, True),
            "VCR": (693, 100, 18, 38, True), "VCF": (693, 200, 18, 38, True),
            "VR": (693, 290, 18, 38, True), "VRT": (693, 385, 18, 38, True),
            "VR2": (510, 340, 18, 38, True), "AVR": (862, 165, 11, 38, False),
            "AVU": (862, 310, 11, 38, False), "MF": (925, 580, 18, 38, True),
            "MR": (815, 580, 18, 38, True),
        }

        def arrow(points: list[tuple[float, float]], label: str = "", label_at: tuple[float, float] | None = None) -> None:
            flattened = [coordinate for point in points for coordinate in point]
            canvas.create_line(*flattened, arrow="last", fill="black", width=1.5)
            if label and label_at:
                canvas.create_text(*label_at, text=label, fill="black", font=("TkDefaultFont", 8))

        # Main task-flow and vehicle-flow arcs.
        arrow([(88, 245), (150, 245)])
        arrow([(190, 245), (244, 245)])
        arrow([(266, 245), (320, 245)])
        # TD is an immediate discard transition from the queue.
        arrow([(170, 225), (170, 141)])
        arrow([(340, 265), (340, 344)])
        arrow([(360, 245), (428, 245)])
        arrow([(448, 225), (500, 178)], "((#TR)-(((#VW)-1)*NTV))", (470, 185))
        arrow([(520, 160), (582, 160)])
        arrow([(622, 160), (683, 118)])
        arrow([(711, 118), (790, 225)])
        arrow([(683, 218), (622, 170)])
        arrow([(711, 218), (790, 235)])
        arrow([(790, 260), (711, 308)])
        arrow([(683, 308), (622, 340)])
        arrow([(622, 340), (683, 403)])
        arrow([(711, 403), (790, 265)])
        arrow([(448, 265), (500, 322)], "((#TR)-(((#VW)-1)*NTV))", (470, 320))
        arrow([(520, 340), (582, 340)])
        # Availability loop: AVD -> AVR -> VA -> AVU -> AVD.
        arrow([(900, 231), (873, 183)], "#AVD", (875, 205))
        arrow([(851, 183), (820, 230)], "#AVD", (835, 190))
        arrow([(820, 260), (851, 330)], "#VA", (835, 300))
        arrow([(873, 330), (900, 259)], "#VA", (875, 305))
        # VART/VDART connect the working-vehicle state back to VA, as in the
        # reference drawing.  These arcs must not enter the manager loop.
        arrow([(438, 225), (438, 59)])
        arrow([(449, 40), (800, 40), (800, 225)])
        arrow([(438, 265), (438, 459)])
        arrow([(449, 460), (800, 460), (800, 265)])
        # Manager loop: MU -> MF -> MD -> MR -> MU.
        arrow([(888, 540), (916, 562)])
        arrow([(916, 598), (888, 635)])
        arrow([(852, 635), (824, 598)])
        arrow([(824, 562), (852, 540)])

        place_colors = {"TQ": "white", "TR": "white", "VW": "white", "VA": "white", "AVD": "white", "VD": "white", "VRD": "white", "MU": "white", "MD": "white"}
        for name, (x, y) in places.items():
            radius = 20
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=place_colors[name], outline="black", width=2)
            token_text = str(marking[name])
            canvas.create_text(x, y, text=token_text, font=("TkDefaultFont", 10, "bold"), fill="black")
            canvas.create_text(x, y + 31, text=name, font=("TkDefaultFont", 9), fill="black")

        now = time.monotonic()
        for name, (x, y, width, height, timed) in transitions.items():
            active = self.spn_transition_flash.get(name, 0.0) > now
            fill = "#ffe699" if active else ("white" if timed else "black")
            outline = "#c00000" if active else "black"
            line_width = 3 if active else 1
            canvas.create_rectangle(x - width / 2, y - height / 2, x + width / 2, y + height / 2, fill=fill, outline=outline, width=line_width)
            canvas.create_text(x, y + height / 2 + 15, text=name, font=("TkDefaultFont", 9), fill="black")


    def _reload_parameters(self) -> None:
        values = load_env_file(ROOT / ".env")
        for key, variable in self.parameter_vars.items():
            if key == "MQTT_BROKER":
                variable.set(values.get(key, "localhost"))
            elif key in values:
                variable.set(values[key])
        self._append_log("Parameter controls reloaded from .env.")

    def _read_parameters(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key, converter in PARAMETER_TYPES.items():
            raw_value = self.parameter_vars[key].get().strip()
            if not raw_value and key not in {"IMAGE_FOLDER", "IMAGE_MODEL_PATH"}:
                raise ValueError(f"{PARAMETER_LABELS[key]} cannot be empty")
            if not raw_value:
                values[key] = ""
                continue
            try:
                value = converter(raw_value)
            except ValueError as exc:
                raise ValueError(f"{PARAMETER_LABELS[key]} has an invalid value") from exc
            if converter is not str and value <= 0:
                raise ValueError(f"{PARAMETER_LABELS[key]} must be greater than zero")
            values[key] = value

        if values["JOB_TYPE"] not in {"math.factorial", "math.add", "math.multiply", "math.fibonacci", "image.classification"}:
            raise ValueError("Job type must be a supported math job or image.classification")
        if not 0 <= values["IMAGE_DAMAGED_PROBABILITY"] <= 1:
            raise ValueError("Damaged probability must be between 0 and 1")
        if values["JOB_TYPE"] == "image.classification" and (not values["IMAGE_FOLDER"] or not values["IMAGE_MODEL_PATH"]):
            raise ValueError("Image folder and Keras model path are required for image.classification")

        host = self.parameter_vars["MQTT_BROKER"].get().strip()
        if not host:
            raise ValueError("MQTT broker host cannot be empty")
        values["MQTT_BROKER"] = host
        if not 1 <= values["MQTT_PORT"] <= 65535:
            raise ValueError("MQTT port must be between 1 and 65535")
        return values

    @staticmethod
    def _probe_broker(host: str, port: int) -> tuple[bool, str]:
        """Check that a TCP listener accepts connections at the selected endpoint."""
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return True, f"reachable at {host}:{port}"
        except (OSError, ValueError) as exc:
            return False, f"unreachable at {host}:{port} ({exc})"

    def check_broker(self) -> None:
        """Probe the selected broker endpoint without blocking Tk's event loop."""
        try:
            values = self._read_parameters()
        except ValueError as exc:
            messagebox.showerror("Invalid broker parameters", str(exc))
            return
        host = values["MQTT_BROKER"]
        port = int(values["MQTT_PORT"])
        self.broker_probe_label.configure(text="checking…")

        def probe() -> None:
            reachable, detail = self._probe_broker(host, port)
            self.events.put({"kind": "broker_probe", "reachable": reachable, "detail": detail})

        threading.Thread(target=probe, daemon=True).start()

    def _write_gui_config(self, values: dict[str, Any]) -> Path:
        config_dir = ROOT / "experiments" / "gui_configs"
        config_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        config_path = config_dir / f"simulation_{stamp}.env"
        lines = [
            "# Generated by simulation_gui.py; the repository .env was not modified.",
            "# Values are exported to all services in this batch.",
        ]
        for key in ("MQTT_BROKER", "MQTT_PORT", "JOB_TYPE", "IMAGE_FOLDER", "IMAGE_MODEL_PATH", "IMAGE_DAMAGED_PROBABILITY", "MTMBF", "MTBBR", "MTBVF", "MTBVR", "MTBTS", "MTBR", "MTBRR", "NV", "MTBTA", "AF_MTMBF", "AF_MTBBR", "AF_MTBVF", "AF_MTBVR", "AF_MTBR", "AF_MTBRR", "AF_MTBTA", "AF_MTBTS", "TQS", "RUNTIME", "N_EXPERIMENTS"):
            lines.append(f"{key}={values[key]}")
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return config_path

    # ------------------------------------------------------------- MQTT
    def _connect_mqtt(self) -> None:
        try:
            self.mqtt_client = mqtt.Client(client_id=f"midd4vc-gui-{os.getpid()}")
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            self.mqtt_client.on_message = self._on_mqtt_message
            self.mqtt_client.connect_async(self.broker_host, self.broker_port, 60)
            self.mqtt_client.loop_start()
        except Exception as exc:
            self.events.put({"kind": "log", "text": f"MQTT connection setup failed: {exc}"})

    def _reconnect_mqtt(self, host: str, port: int) -> None:
        if host == self.broker_host and port == self.broker_port:
            return
        try:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        except Exception:
            pass
        self.broker_host = host
        self.broker_port = port
        self.connection_label.configure(text="MQTT: reconnecting…")
        self._connect_mqtt()

    def _on_mqtt_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            self.mqtt_connected = True
            client.subscribe("vc/#", qos=0)
            self.events.put({"kind": "mqtt", "connected": True})
        else:
            self.events.put({"kind": "log", "text": f"MQTT connection refused (rc={rc})"})

    def _on_mqtt_disconnect(self, client, userdata, rc) -> None:
        self.mqtt_connected = False
        self.events.put({"kind": "mqtt", "connected": False})

    def _on_mqtt_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        topic = message.topic
        now = datetime.now().strftime("%H:%M:%S")

        if topic.endswith("job/submit"):
            self.events.put({"kind": "metric", "name": "task_submitted", "job_id": payload.get("job_id", ""), "text": f"{now}  task submitted {payload.get('job_id', '')}"})
            return

        assign_match = JOB_ID_RE.search(topic)
        if assign_match:
            vehicle_id = assign_match.group(1)
            job_id = payload.get("job_id", f"job-{time.monotonic_ns()}")
            self.events.put({"kind": "task_started", "vehicle": vehicle_id, "job_id": job_id, "text": f"{now}  task started on {vehicle_id} {job_id}"})
            return

        # Consume only manager-forwarded results.  Vehicle results are
        # manager input and must not be counted as completed twice.
        if topic.startswith("vc/client/") and topic.endswith("job/result"):
            job_id = payload.get("job_id", "")
            vehicle_id = payload.get("vehicle_id", "")
            failed = bool(payload.get("error"))
            self.events.put({"kind": "task_finished", "vehicle": vehicle_id, "job_id": job_id, "failed": failed, "text": f"{now}  task {'failed' if failed else 'finished'} on {vehicle_id} {job_id}"})
            return

        if topic.endswith("register/request") or topic.endswith("unregister/request"):
            vehicle_id = payload.get("vehicle_id")
            if vehicle_id:
                online = topic.endswith("register/request")
                self.events.put({"kind": "vehicle_online", "vehicle": vehicle_id, "online": online, "text": f"{now}  {vehicle_id} {'registered' if online else 'unregistered'}"})
            return

        if "/control/" in topic and topic.endswith("/event"):
            target = payload.get("target_id", "")
            event = payload.get("event", "")
            self.events.put({
                "kind": "control",
                "target": target,
                "event": event,
                "reason": payload.get("reason", ""),
                "job_id": payload.get("job_id", ""),
                "text": f"{now}  {target}: {event} ({payload.get('reason', '')})",
            })

    # ------------------------------------------------------------- launcher
    def start_batch(self) -> None:
        with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                self._append_log("A batch is already running.")
                return
            try:
                parameters = self._read_parameters()
                config_path = self._write_gui_config(parameters)
            except (OSError, ValueError) as exc:
                messagebox.showerror("Invalid simulation parameters", str(exc))
                return

            broker_ok, broker_detail = self._probe_broker(
                parameters["MQTT_BROKER"], int(parameters["MQTT_PORT"])
            )
            self.broker_probe_label.configure(
                text="reachable" if broker_ok else "unreachable",
                foreground="#2e9d62" if broker_ok else "#c74747",
            )
            if not broker_ok:
                self._append_log(f"Broker check failed: {broker_detail}")
                messagebox.showerror("Broker unavailable", broker_detail + "\nStart the broker or select another port before launching.")
                return

            self.vehicle_count = int(parameters["NV"])
            self.runtime_seconds = float(parameters["RUNTIME"])
            self.experiment_count = int(parameters["N_EXPERIMENTS"])
            self.vehicle_ids = [f"veh{i}" for i in range(1, self.vehicle_count + 1)]
            self._reconnect_mqtt(parameters["MQTT_BROKER"], int(parameters["MQTT_PORT"]))
            self.clear_observation("new batch")
            try:
                process_environment = os.environ.copy()
                process_environment["MIDD4VC_ENV_FILE"] = str(config_path)
                self.process = subprocess.Popen(
                    ["bash", str(ROOT / "runme.sh")],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                    env=process_environment,
                )
            except OSError as exc:
                self._append_log(f"Unable to start simulation: {exc}")
                return
            self.batch_started_at = time.monotonic()
            self.experiment_started_at = None
            self.clock_elapsed = 0.0
            self.clock_running = True
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.batch_label.configure(text=f"Simulation: running 0/{self.experiment_count}")
            self._start_spn()
            threading.Thread(target=self._read_process_output, args=(self.process,), daemon=True).start()
            self._append_log(f"Simulation batch started using runme.sh with {config_path}")

    def _read_process_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if line:
                    self.events.put({"kind": "launcher", "line": line})
        return_code = process.wait()
        self.events.put({"kind": "launcher_exit", "return_code": return_code})

    def stop_batch(self) -> None:
        with self.process_lock:
            process = self.process
            if process is None or process.poll() is not None:
                self._append_log("No running simulation launcher found.")
                return
            try:
                os.killpg(process.pid, signal.SIGTERM)
                self._append_log("Stop requested for the simulation process group.")
            except ProcessLookupError:
                self._append_log("Simulation launcher process group already exited.")
            self.stop_deadline = time.monotonic() + 5.0
            self.stop_button.configure(state="disabled")
            self._pause_spn()
            self.root.after(100, self._finish_stop_if_needed)

    def _finish_stop_if_needed(self) -> None:
        """Escalate a graceful stop without blocking Tk's event loop."""
        process = self.process
        if process is None or process.poll() is not None:
            self.stop_deadline = None
            return
        if self.stop_deadline is not None and time.monotonic() >= self.stop_deadline:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                self._append_log("Simulation launcher did not stop gracefully; SIGKILL sent to its process group.")
            except ProcessLookupError:
                pass
            self.stop_deadline = None
            return
        self.root.after(100, self._finish_stop_if_needed)

    # ------------------------------------------------------------- state/UI
    def _reset_partial_metrics(self, *, start: bool = False) -> None:
        """Reset measurements for the current experiment without touching totals."""
        now = time.monotonic()
        self.partial_started_at = now if start else None
        self.partial_last_update = now
        self.partial_observed_seconds = 0.0
        self.partial_vehicle_available_area = 0.0
        self.partial_manager_available_area = 0.0
        self.partial_submitted = 0
        self.partial_completed = 0
        self.partial_discarded = 0
        for label in self.partial_metrics_labels.values():
            label.configure(text="--")

    def _accumulate_partial_time(self) -> None:
        """Accumulate time-weighted availability using the current live marking."""
        if self.partial_started_at is None:
            return
        now = time.monotonic()
        delta = max(0.0, now - self.partial_last_update)
        if delta:
            available = sum(
                state.online and not state.rented and not state.faulted
                for state in self.vehicles.values()
            )
            self.partial_vehicle_available_area += available * delta
            if self.manager_online and not self.manager_failed:
                self.partial_manager_available_area += delta
            self.partial_observed_seconds += delta
        self.partial_last_update = now

    def _update_partial_metrics(self) -> None:
        observed = self.partial_observed_seconds
        vehicle_total = max(len(self.vehicle_ids), 1)
        if observed > 0:
            vehicle_availability = self.partial_vehicle_available_area / (observed * vehicle_total) * 100.0
            manager_availability = self.partial_manager_available_area / observed * 100.0
        else:
            vehicle_availability = manager_availability = 0.0
        vehicle_availability = min(100.0, max(0.0, vehicle_availability))
        manager_availability = min(100.0, max(0.0, manager_availability))
        discard_proportion = (
            self.partial_discarded / self.partial_submitted * 100.0
            if self.partial_submitted else 0.0
        )
        completion_proportion = (
            self.partial_completed / self.partial_submitted * 100.0
            if self.partial_submitted else 0.0
        )
        values = {
            "vehicle_available": f"{vehicle_availability:.2f}%",
            "vehicle_unavailable": f"{100.0 - vehicle_availability:.2f}%",
            "manager_available": f"{manager_availability:.2f}%",
            "manager_unavailable": f"{100.0 - manager_availability:.2f}%",
            "discard_proportion": f"{discard_proportion:.2f}%",
            "completion_proportion": f"{completion_proportion:.2f}%",
            "partial_observed": f"{observed:.1f} s",
            "partial_jobs": f"{self.partial_submitted} / {self.partial_completed} / {self.partial_discarded}",
        }
        for key, value in values.items():
            self.partial_metrics_labels[key].configure(text=value)

    def _record_discard(self, job_id: str) -> bool:
        """Record one discarded job and prevent duplicate discard notifications."""
        if job_id and job_id in self.discarded_jobs:
            return False
        if job_id:
            self.discarded_jobs.add(job_id)
        self.counters["task_failed"] += 1
        if self.job_experiment.get(job_id) == self.current_experiment and self.partial_started_at is not None:
            self.partial_discarded += 1
        return True

    def clear_observation(self, reason: str = "manual") -> None:
        # Counters are cumulative for the lifetime of this dashboard.  A batch
        # or experiment boundary must not make submitted/finished totals jump
        # backwards; only transient vehicle and lifecycle state is reset here.
        self.running_jobs.clear()
        self.pending_jobs.clear()
        self.discarded_jobs.clear()
        self.spn_transition_flash.clear()
        self.manager_failed = False
        self.manager_online = False
        self.current_experiment = 0
        self.current_experiment_dir = ""
        self.batch_started_at = None
        self.experiment_started_at = None
        self.clock_elapsed = 0.0
        self.clock_running = False
        self.vehicles = {vid: VehicleState(vid) for vid in self.vehicle_ids}
        self._reset_partial_metrics()
        self.manager_status.configure(text="  WAITING  ", bg=self.COLORS["OFFLINE"])
        self.manager_event_label.configure(text="Waiting for server lifecycle events")
        self.path_label.configure(text="")
        self._append_log(f"Live state reset ({reason}); cumulative counters retained.")
        self._redraw_vehicles()

    def _drain_events(self) -> None:
        processed = 0
        max_events_per_tick = 200
        try:
            while processed < max_events_per_tick:
                event = self.events.get_nowait()
                processed += 1
                self._accumulate_partial_time()
                kind = event.get("kind")
                if kind == "mqtt":
                    connected = event["connected"]
                    self.connection_label.configure(text=f"MQTT: {'connected' if connected else 'disconnected'}")
                elif kind == "broker_probe":
                    self.broker_probe_label.configure(
                        text="reachable" if event["reachable"] else "unreachable",
                        foreground="#2e9d62" if event["reachable"] else "#c74747",
                    )
                    self._append_log("Broker check: " + event["detail"])
                elif kind == "log":
                    self._append_log(event["text"])
                elif kind == "launcher":
                    self._handle_launcher_line(event["line"])
                elif kind == "launcher_exit":
                    self._handle_launcher_exit(event["return_code"])
                elif kind == "metric":
                    self.counters[event["name"]] += 1
                    if event["name"] == "task_submitted":
                        if event.get("job_id"):
                            self.job_experiment[event["job_id"]] = self.current_experiment
                            if self.partial_started_at is not None:
                                self.partial_submitted += 1
                            self.pending_jobs.add(event["job_id"])
                            self._trigger_spn_transition("TA", event["text"])
                    self._append_log(event["text"])
                elif kind == "task_started":
                    self._handle_task_started(event)
                elif kind == "task_finished":
                    self._handle_task_finished(event)
                elif kind == "vehicle_online":
                    self._handle_vehicle_online(event)
                elif kind == "control":
                    self._handle_control_event(event)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _handle_launcher_line(self, line: str) -> None:
        start = RUN_START_RE.match(line)
        finished = RUN_FINISHED_RE.match(line)
        if start:
            self.current_experiment = int(start.group(1))
            self.experiment_count = int(start.group(2))
            self.current_experiment_dir = start.group(3)
            self.running_jobs.clear()
            self.pending_jobs.clear()
            self.discarded_jobs.clear()
            self.manager_failed = False
            # The launcher starts the server before announcing an experiment.
            # This also covers the case where the one-shot MQTT `started`
            # lifecycle event was published before the GUI subscribed.
            self.manager_online = True
            self.vehicles = {vid: VehicleState(vid) for vid in self.vehicle_ids}
            self._reset_partial_metrics(start=True)
            self.experiment_started_at = time.monotonic()
            self.clock_elapsed = 0.0
            self.clock_running = True
            self.path_label.configure(text=self.current_experiment_dir)
            self.batch_label.configure(text=f"Simulation: running {self.current_experiment}/{self.experiment_count}")
        elif finished:
            self._accumulate_partial_time()
            self._update_partial_metrics()
            self.partial_started_at = None
            self._append_log(line)
        else:
            self._append_log(line)

    def _handle_launcher_exit(self, return_code: int) -> None:
        self._accumulate_partial_time()
        self._update_partial_metrics()
        self._pause_spn()
        reference = self.experiment_started_at or self.batch_started_at
        if reference is not None:
            self.clock_elapsed = min(
                self.runtime_seconds,
                max(0.0, time.monotonic() - reference),
            )
        self.experiment_started_at = None
        self.partial_started_at = None
        self.batch_started_at = None
        self.clock_running = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.batch_label.configure(text=f"Simulation: finished (exit {return_code})")
        self._append_log(f"Simulation launcher exited with code {return_code}.")

    def _handle_task_started(self, event: dict[str, Any]) -> None:
        vehicle_id = event["vehicle"]
        job_id = event["job_id"]
        state = self.vehicles.setdefault(vehicle_id, VehicleState(vehicle_id))
        self.pending_jobs.discard(job_id)
        if job_id in self.discarded_jobs or state.rented or state.faulted:
            self._record_discard(job_id)
            self._trigger_spn_transition("TD", event["text"])
            self._append_log(f"{event['text']} ignored because {vehicle_id} is unavailable")
            return
        was_working = bool(state.running_jobs)
        self.counters["task_started"] += 1
        self.running_jobs[job_id] = vehicle_id
        state.running_jobs.add(job_id)
        state.last_event = "task started"
        state.last_event_at = datetime.now().strftime("%H:%M:%S")
        self._trigger_spn_transition("TAV", event["text"])
        if not was_working:
            self._trigger_spn_transition("VART", event["text"])
        self._append_log(event["text"])

    def _handle_task_finished(self, event: dict[str, Any]) -> None:
        vehicle_id = event["vehicle"]
        job_id = event["job_id"]
        failed = event["failed"]
        self.pending_jobs.discard(job_id)
        if job_id in self.discarded_jobs:
            self.running_jobs.pop(job_id, None)
            state = self.vehicles.get(vehicle_id)
            if state is not None:
                state.running_jobs.discard(job_id)
            self._append_log(f"{event['text']} ignored because the task was already discarded")
            return
        if job_id in self.completed_jobs:
            self._append_log(f"{event['text']} ignored because the result was already counted")
            return
        self.completed_jobs.add(job_id)
        self.counters["task_failed" if failed else "success"] += 1
        if not failed and self.job_experiment.get(job_id) == self.current_experiment and self.partial_started_at is not None:
            self.partial_completed += 1
        self.running_jobs.pop(job_id, None)
        state = self.vehicles.setdefault(vehicle_id, VehicleState(vehicle_id))
        state.running_jobs.discard(job_id)
        self._trigger_spn_transition("TS", event["text"])
        if not state.running_jobs:
            self._trigger_spn_transition("VDART", event["text"])
        state.last_event = "task failed" if failed else "task finished"
        state.last_event_at = datetime.now().strftime("%H:%M:%S")
        self._append_log(event["text"])

    def _discard_vehicle_tasks(self, vehicle_id: str, reason: str) -> bool:
        """Remove live assignments when a vehicle becomes unavailable."""
        state = self.vehicles.get(vehicle_id)
        if state is None:
            return False
        job_ids = set(state.running_jobs)
        job_ids.update(job_id for job_id, assigned_vehicle in self.running_jobs.items() if assigned_vehicle == vehicle_id)
        for job_id in job_ids:
            self.pending_jobs.discard(job_id)
            self._record_discard(job_id)
            self.running_jobs.pop(job_id, None)
        state.running_jobs.clear()
        if job_ids:
            self._append_log(f"{vehicle_id}: discarded {len(job_ids)} running task(s) ({reason})")
        return bool(job_ids)

    def _handle_vehicle_online(self, event: dict[str, Any]) -> None:
        state = self.vehicles.setdefault(event["vehicle"], VehicleState(event["vehicle"]))
        state.online = event["online"]
        state.last_event = "registered" if event["online"] else "unregistered"
        state.last_event_at = datetime.now().strftime("%H:%M:%S")
        self._trigger_spn_transition("AVR" if event["online"] else "AVU", event["text"])
        self._append_log(event["text"])

    def _handle_control_event(self, event: dict[str, Any]) -> None:
        target = event["target"]
        name = event["event"]
        self._append_log(event["text"])
        if target == "server":
            if name == "fault_started":
                self.manager_failed = True
                self.manager_online = False
                self.counters["manager_fault"] += 1
                self._trigger_spn_transition("MF", event["text"])
            elif name == "stopped":
                self.manager_failed = True
                self.manager_online = False
            elif name == "job_discarded":
                self.pending_jobs.discard(event.get("job_id", ""))
                self._record_discard(event.get("job_id", ""))
                self._trigger_spn_transition("TD", event["text"])
            elif name in {"fault_repaired", "started"}:
                if name == "fault_repaired":
                    self.counters["manager_repair"] += 1
                self.manager_failed = False
                self.manager_online = True
                if name == "fault_repaired" or event.get("reason") == "fault":
                    self._trigger_spn_transition("MR", event["text"])
            self.manager_status.configure(
                text="  FAILED  " if self.manager_failed else "  ONLINE  ",
                bg=self.COLORS["FAULT"] if self.manager_failed else self.COLORS["AVAILABLE"],
            )
            self.manager_event_label.configure(text=f"Last event: {name} ({event.get('reason', '')})")
            return

        if not target.startswith("veh"):
            return
        state = self.vehicles.setdefault(target, VehicleState(target))
        if name == "rental_started":
            had_running = self._discard_vehicle_tasks(target, "rental_started")
            state.rented = True
            self.counters["rental"] += 1
            self._trigger_spn_transition("VR2" if had_running else "VR", event["text"])
        elif name == "task_discarded":
            job_id = event.get("job_id", "")
            if job_id:
                self._record_discard(job_id)
                self.running_jobs.pop(job_id, None)
                self.pending_jobs.discard(job_id)
                state.running_jobs.discard(job_id)
                self._trigger_spn_transition("TD", event["text"])
            else:
                self._discard_vehicle_tasks(target, event.get("reason", "vehicle_unavailable"))
        elif name == "rental_returned":
            state.rented = False
            self.counters["return"] += 1
            self._trigger_spn_transition("VRT", event["text"])
        elif name == "fault_started":
            had_running = self._discard_vehicle_tasks(target, "fault_started")
            state.faulted = True
            self.counters["vehicle_fault"] += 1
            self._trigger_spn_transition("VCF2" if had_running else "VCF", event["text"])
        elif name == "fault_repaired":
            state.faulted = False
            self.counters["vehicle_repair"] += 1
            self._trigger_spn_transition("VCR", event["text"])
        elif name == "stopped" and event.get("reason") == "fault":
            state.faulted = True
        elif name == "started" and event.get("reason") == "fault":
            state.faulted = False
        state.last_event = name
        state.last_event_at = datetime.now().strftime("%H:%M:%S")

    def _trigger_spn_transition(self, transition: str, message: str = "") -> None:
        self.spn_transition_flash[transition] = time.monotonic() + 0.45
        if message:
            self._append_spn_log(f"{transition}: {message}")

    def _live_spn_marking(self) -> dict[str, int]:
        """Build the SPN marking from the same live state used by the monitor."""
        running_vehicle_ids = set(self.running_jobs.values())
        available = sum(
            state.online and not state.rented and not state.faulted and vehicle_id not in running_vehicle_ids
            for vehicle_id, state in self.vehicles.items()
        )
        working = sum(
            state.online and not state.rented and not state.faulted and vehicle_id in running_vehicle_ids
            for vehicle_id, state in self.vehicles.items()
        )
        rented = sum(state.rented for state in self.vehicles.values())
        failed = sum(state.faulted for state in self.vehicles.values())
        running = len(self.running_jobs)
        queue = len(self.pending_jobs)
        manager_down = int(self.manager_failed)
        return {
            "AVD": available if manager_down else 0,
            "MD": manager_down,
            "MU": 0 if manager_down else 1,
            "TQ": queue,
            "TR": running,
            "VA": 0 if manager_down else available,
            "VD": failed,
            "VRD": rented,
            "VW": working,
        }

    def _refresh_clock_and_view(self) -> None:
        self._accumulate_partial_time()
        self._update_partial_metrics()
        reference = self.experiment_started_at or self.batch_started_at
        if self.clock_running and reference is not None:
            self.clock_elapsed = min(
                self.runtime_seconds,
                max(0.0, time.monotonic() - reference),
            )
        elapsed = self.clock_elapsed
        self.clock_label.configure(text=f"Time: {self._format_duration(elapsed)} / {self._format_duration(self.runtime_seconds)}")
        self._redraw_vehicles()
        self._draw_spn()
        for key, label in self.counter_labels.items():
            label.configure(text=str(self.counters[key]))
        self.root.after(500, self._refresh_clock_and_view)

    def _redraw_vehicles(self) -> None:
        canvas = self.vehicle_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 600)
        height = max(canvas.winfo_height(), 230)
        count = max(len(self.vehicles), 1)
        card_width = max(220, min(330, (width - 30) / count))
        for index, state in enumerate(self.vehicles.values()):
            x = 15 + index * card_width
            y = 20
            color = self.COLORS[state.status]
            canvas.create_rectangle(x, y, x + card_width - 15, height - 18, fill="white", outline="#d6dce5", width=2)
            # A simple car glyph: body, cabin, and wheels.
            car_x = x + 28
            car_y = y + 38
            canvas.create_rectangle(car_x, car_y + 18, car_x + 92, car_y + 45, fill=color, outline=color)
            canvas.create_polygon(car_x + 18, car_y + 18, car_x + 34, car_y, car_x + 70, car_y, car_x + 82, car_y + 18, fill=color, outline=color)
            canvas.create_oval(car_x + 14, car_y + 37, car_x + 30, car_y + 53, fill="#27303b", outline="#27303b")
            canvas.create_oval(car_x + 68, car_y + 37, car_x + 84, car_y + 53, fill="#27303b", outline="#27303b")
            canvas.create_text(x + 135, y + 28, text=state.vehicle_id, anchor="w", font=("TkDefaultFont", 12, "bold"), fill="#172033")
            canvas.create_text(x + 135, y + 57, text=state.status, anchor="w", font=("TkDefaultFont", 10, "bold"), fill=color)
            canvas.create_text(x + 28, y + 115, text=f"Running tasks: {len(state.running_jobs)}", anchor="w", fill="#3f4858")
            canvas.create_text(x + 28, y + 142, text=f"Last: {state.last_event}", anchor="w", fill="#3f4858")
            canvas.create_text(x + 28, y + 166, text=state.last_event_at, anchor="w", fill="#7a8491")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 600:
            self.log_text.delete("1.0", "100.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = int(seconds)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self._pause_spn()
        process = self.process
        if process is not None and process.poll() is None:
            if not messagebox.askyesno("Exit dashboard", "A simulation is running. Stop it and exit?"):
                self.closing = False
                return
            self.stop_batch()
        try:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true", help="start runme.sh immediately")
    args = parser.parse_args()
    root = tk.Tk()
    SimulationDashboard(root, launch_on_start=args.launch)
    root.mainloop()


if __name__ == "__main__":
    main()
