SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source .venv/bin/activate

set -a
source .env
set +a

python client/applications.py >> applications.log 2>> applications.log &
for i in $(seq 1 $N_EXPERIMENTS); do
    EXP_DIR="$(pwd)/experiments/$(date +%Y%m%d_%H%M%S)"
    mkdir -p -- "$EXP_DIR"
    echo "Run experiment: $i/$N_EXPERIMENTS - $EXP_DIR"
    python server/Midd4VCServer.py f $EXP_DIR >> $EXP_DIR/server.log 2>> $EXP_DIR/server.log &
    python client/vehicles.py f $EXP_DIR >> $EXP_DIR/vehicles.log 2>> $EXP_DIR/vehicles.log &
    sleep $RUNTIME
    echo "Run experiment: $i/$N_EXPERIMENTS: Finished"
done