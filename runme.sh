SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source .venv/bin/activate

set -a
source .env
set +a

export PYTHONUNBUFFERED=1

for i in $(seq 1 $N_EXPERIMENTS); do
    EXP_DIR="$(pwd)/experiments/$(date +%Y%m%d_%H%M%S)"
    mkdir -p -- "$EXP_DIR"
    echo "Run experiment: $i/$N_EXPERIMENTS - $EXP_DIR"
    python client/applications.py exp $EXP_DIR >> $EXP_DIR/applications.log 2>&1 &
    python server/Midd4VCServer.py f $EXP_DIR no-timeout >> $EXP_DIR/server.log 2>&1 &
    python client/vehicles.py f $EXP_DIR >> $EXP_DIR/vehicles.log 2>&1 &
    sleep $RUNTIME
    pkill -f client/application
    pkill -f client/vehicle
    pkill -f server/Midd4VC
    echo "Run experiment: $i/$N_EXPERIMENTS: Finished"
    sleep 5
done