"""Task requester for mathematical and blind image-classification workloads."""

from __future__ import annotations

import csv
import json
import os
import random
import shutil
import sys
import time
import uuid
from datetime import datetime
from multiprocessing import Process
from pathlib import Path
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from dotenv import load_dotenv

from Midd4VCClient import Midd4VCClient
from common.acceleration import accelerated_time


load_dotenv()

MTBTA = float(os.getenv("MTBTA", 2.88))
MTBTA_ACCELERATED = accelerated_time(MTBTA, "MTBTA")
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
CLASS_NAMES = ("damaged", "not_damaged")


def discover_images(image_folder: str | Path) -> dict[str, list[Path]]:
    """Discover sorted class images; labels never leave this requester."""
    root = Path(image_folder).expanduser().resolve()
    images: dict[str, list[Path]] = {}
    for class_name in CLASS_NAMES:
        class_folder = root / class_name
        if not class_folder.is_dir():
            raise ValueError(f"Missing classification folder: {class_folder}")
        images[class_name] = sorted(
            path for path in class_folder.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        )
        if not images[class_name]:
            raise ValueError(f"No supported images found in {class_folder}")
    return images


class ApplicationClient:
    def __init__(self, client_id, role="client", log_directory=None):
        self.client = Midd4VCClient(role=role, client_id=client_id)
        self.client.set_result_handler(self.on_job_result)
        self.log_directory = Path(log_directory).resolve() if log_directory else None
        self.job_type = os.getenv("JOB_TYPE", "math.factorial").strip()
        self.image_catalog: dict[str, list[Path]] = {}
        self.image_cursors = {class_name: 0 for class_name in CLASS_NAMES}
        self.ground_truth: dict[str, dict[str, str]] = {}
        self.evaluated_jobs: set[str] = set()
        self.metrics_lock = threading.Lock()
        self.metrics = {
            "tp": 0, "tn": 0, "fp": 0, "fn": 0, "evaluated": 0,
            "score_count": 0, "score_sum": 0.0, "score_min": None, "score_max": None,
        }
        self.classification_results_path: Path | None = None
        self.classification_metrics_path: Path | None = None
        self.staging_directory: Path | None = None

        if self.job_type == "image.classification":
            image_folder = os.getenv("IMAGE_FOLDER", "").strip()
            if not image_folder:
                raise ValueError("IMAGE_FOLDER is required for image.classification")
            self.image_catalog = discover_images(image_folder)
            self.staging_directory = (self.log_directory or Path.cwd()) / "classification_staging"
            self.staging_directory.mkdir(parents=True, exist_ok=True)
            if self.log_directory:
                self.classification_results_path = self.log_directory / "classification_results.csv"
                self.classification_metrics_path = self.log_directory / "classification_metrics.json"
                with self.classification_results_path.open("w", newline="", encoding="utf-8") as output:
                    csv.writer(output).writerow([
                        "job_id", "image_id", "actual_class", "predicted_class", "score",
                        "vehicle_id", "correct", "started_at", "finished_at", "status",
                    ])

    def _write_metrics(self) -> None:
        if self.classification_metrics_path is None:
            return
        with self.metrics_lock:
            metrics = dict(self.metrics)
        tp, tn, fp, fn = (metrics[key] for key in ("tp", "tn", "fp", "fn"))
        total = metrics["evaluated"]
        recall = tp / (tp + fn) if tp + fn else None
        specificity = tn / (tn + fp) if tn + fp else None
        precision = tp / (tp + fp) if tp + fp else None
        f1 = (2 * precision * recall / (precision + recall)) if precision is not None and recall is not None and precision + recall else None
        balanced_accuracy = (recall + specificity) / 2 if recall is not None and specificity is not None else None
        score_count = metrics["score_count"]
        payload = {
            "positive_class": "damaged",
            "negative_class": "not_damaged",
            "true_positive": tp,
            "true_negative": tn,
            "false_positive": fp,
            "false_negative": fn,
            "evaluated": total,
            "accuracy": (tp + tn) / total if total else None,
            "precision": precision,
            "recall": recall,
            "sensitivity": recall,
            "specificity": specificity,
            "f1": f1,
            "balanced_accuracy": balanced_accuracy,
            "score_count": score_count,
            "score_mean": metrics["score_sum"] / score_count if score_count else None,
            "score_min": metrics["score_min"],
            "score_max": metrics["score_max"],
        }
        self.classification_metrics_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _next_classification_job(self) -> dict:
        probability = float(os.getenv("IMAGE_DAMAGED_PROBABILITY", "0.5"))
        if not 0 <= probability <= 1:
            raise ValueError("IMAGE_DAMAGED_PROBABILITY must be between 0 and 1")
        actual_class = "damaged" if random.random() < probability else "not_damaged"
        source = self.image_catalog[actual_class][self.image_cursors[actual_class]]
        self.image_cursors[actual_class] = (self.image_cursors[actual_class] + 1) % len(self.image_catalog[actual_class])
        job_id = str(uuid.uuid4())
        image_id = f"{uuid.uuid4().hex}{source.suffix.lower()}"
        if self.staging_directory is None:
            raise RuntimeError("Classification staging directory is not initialized")
        staged_path = self.staging_directory / image_id
        shutil.copy2(source, staged_path)
        self.ground_truth[job_id] = {"image_id": image_id, "actual_class": actual_class}
        return {"job_id": job_id, "function": "image.classification", "args": [str(staged_path)]}

    def _next_job(self) -> dict:
        if self.job_type == "image.classification":
            return self._next_classification_job()
        args = [random.randint(1, 10)] if self.job_type == "math.factorial" else []
        return {"job_id": str(uuid.uuid4()), "function": self.job_type, "args": args}

    def on_job_result(self, data):
        if self.log_directory:
            with (self.log_directory / "application.csv").open("a", encoding="utf-8") as output:
                output.write(f"{data['job_id']};{data['vehicle_id']};{data['started_at']};{data['finished_at']}\n")
        if self.job_type != "image.classification":
            return

        job_id = data["job_id"]
        truth = self.ground_truth.get(job_id)
        result = data.get("result") or {}
        predicted = result.get("predicted_class")
        score = result.get("score")
        completed = predicted in CLASS_NAMES and truth is not None
        correct = predicted == truth["actual_class"] if completed else None

        if completed:
            with self.metrics_lock:
                if job_id in self.evaluated_jobs:
                    return
                self.evaluated_jobs.add(job_id)
                self.metrics["evaluated"] += 1
                actual = truth["actual_class"]
                if actual == "damaged" and predicted == "damaged":
                    self.metrics["tp"] += 1
                elif actual == "not_damaged" and predicted == "not_damaged":
                    self.metrics["tn"] += 1
                elif actual == "not_damaged" and predicted == "damaged":
                    self.metrics["fp"] += 1
                else:
                    self.metrics["fn"] += 1
                if isinstance(score, (int, float)):
                    score = float(score)
                    self.metrics["score_count"] += 1
                    self.metrics["score_sum"] += score
                    self.metrics["score_min"] = score if self.metrics["score_min"] is None else min(self.metrics["score_min"], score)
                    self.metrics["score_max"] = score if self.metrics["score_max"] is None else max(self.metrics["score_max"], score)

        if self.classification_results_path and truth:
            with self.classification_results_path.open("a", newline="", encoding="utf-8") as output:
                csv.writer(output).writerow([
                    job_id, truth["image_id"], truth["actual_class"], predicted or "",
                    score if score is not None else "", data.get("vehicle_id", ""),
                    correct if correct is not None else "", data.get("started_at", ""),
                    data.get("finished_at", ""), "completed" if completed else "error",
                ])
            self._write_metrics()

    def start(self):
        self.client.start()

    def stop(self):
        self._write_metrics()
        self.client.stop()

    def _submit_next(self, app_id):
        job = self._next_job()
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};TASK_REQUESTER;{app_id};SUBMITTING_JOB;{job['job_id']};NULL")
        self.client.submit_job(job)

    def send_job_periodically(self, min_time=2, max_time=5, app_id=None):
        try:
            while True:
                try:
                    self._submit_next(app_id)
                except Exception as exc:
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};TASK_REQUESTER;{app_id};SUBMISSION_ERROR;NULL;{exc}", flush=True)
                    time.sleep(1)
                time.sleep(random.uniform(min_time, max_time))
        except KeyboardInterrupt:
            print(f"[{self.client.client_id}] stopping...")

    def send_job_exp(self, mean_time, app_id):
        try:
            time.sleep(2)
            while True:
                try:
                    self._submit_next(app_id)
                except Exception as exc:
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};TASK_REQUESTER;{app_id};SUBMISSION_ERROR;NULL;{exc}", flush=True)
                    time.sleep(1)
                time.sleep(np.random.exponential(scale=mean_time))
        except KeyboardInterrupt:
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')};TASK_REQUESTER;{app_id};STOPPING;NULL;NULL")


def run_app(client_id, exponential=False, log_dir=None):
    app_client = ApplicationClient(client_id=client_id, log_directory=log_dir)
    app_client.start()
    try:
        if not exponential:
            app_client.send_job_periodically(min_time=5, max_time=10, app_id=client_id)
        else:
            app_client.send_job_exp(mean_time=MTBTA_ACCELERATED, app_id=client_id)
    finally:
        app_client.stop()


if __name__ == "__main__":
    args = sys.argv[1:]
    client_ids = [f"Application_{i}" for i in range(1, 2)]
    processes = []
    for cid in client_ids:
        if args and args[0] == "exp":
            process = Process(target=run_app, args=(cid, True, args[1] if len(args) > 1 else None))
        else:
            process = Process(target=run_app, args=(cid,))
        process.start()
        processes.append(process)
    try:
        for process in processes:
            process.join()
    except KeyboardInterrupt:
        print("Stopping all applications...")
        for process in processes:
            process.terminate()
