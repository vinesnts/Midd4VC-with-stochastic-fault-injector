#!/usr/bin/env bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source .venv/bin/activate

set -a
ENV_FILE="${MIDD4VC_ENV_FILE:-.env}"
if [ ! -f "$ENV_FILE" ]; then
    echo "Configuration file not found: $ENV_FILE" >&2
    exit 1
fi
source "$ENV_FILE"
set +a

export PYTHONUNBUFFERED=1

child_pids=()

cleanup_children() {
    for pid in "${child_pids[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    for pid in "${child_pids[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
    child_pids=()
}

stop_launcher() {
    cleanup_children
    exit 143
}

trap stop_launcher TERM INT

for i in $(seq 1 $N_EXPERIMENTS); do
    EXP_DIR="$(pwd)/experiments/$(date +%Y%m%d_%H%M%S)"
    mkdir -p -- "$EXP_DIR"
    echo "Run experiment: $i/$N_EXPERIMENTS - $EXP_DIR"
    python client/applications.py exp $EXP_DIR >> $EXP_DIR/applications.log 2>&1 & child_pids+=("$!")
    python server/Midd4VCServer.py normal $EXP_DIR no-timeout >> $EXP_DIR/server.log 2>&1 & child_pids+=("$!")
    python client/vehicles.py normal $EXP_DIR >> $EXP_DIR/vehicles.log 2>&1 & child_pids+=("$!")
    python monitor/monitor.py --output-dir "$EXP_DIR" --vehicles $(seq -f 'veh%g' 1 "$NV") --runtime "$RUNTIME" >> "$EXP_DIR/monitor.log" 2>&1 & child_pids+=("$!")
    python injector/fault_injector.py server server --output-dir "$EXP_DIR" --runtime "$RUNTIME" >> "$EXP_DIR/server-faults.log" 2>&1 & child_pids+=("$!")
    for vehicle_id in $(seq -f 'veh%g' 1 "$NV"); do
        python injector/fault_injector.py vehicle "$vehicle_id" --output-dir "$EXP_DIR" --runtime "$RUNTIME" >> "$EXP_DIR/vehicle-faults.log" 2>&1 & child_pids+=("$!")
        python rental/rental_generator.py "$vehicle_id" --output-dir "$EXP_DIR" --runtime "$RUNTIME" >> "$EXP_DIR/rentals.log" 2>&1 & child_pids+=("$!")
    done
    sleep $RUNTIME
    cleanup_children
    echo "Run experiment: $i/$N_EXPERIMENTS: Finished"
    sleep 5
done
