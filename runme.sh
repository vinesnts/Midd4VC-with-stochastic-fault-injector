cd /home/vinesnts/Projetos/Midd4VC/
source .venv/bin/activate

python client/applications.py >> applications.log 2>> applications.log &
N_EXP=10
SECONDS_IN_HOUR=3600
for i in $(seq 1 $N_EXP); do
    EXP_DIR="$(pwd)/experiments/$(date +%Y%m%d_%H%M%S)"
    echo "Run experiment: $i/$N_EXP - $EXP_DIR"
    python server/Midd4VCServer.py f $EXP_DIR >> $EXP_DIR/server.log 2>> $EXP_DIR/server.log &
    python client/vehicles.py f $EXP_DIR >> $EXP_DIR/vehicles.log 2>> $EXP_DIR/vehicles.log &
    sleep $SECONDS_IN_HOUR
    echo "Run experiment: $i/$N_EXP: Finished"
done