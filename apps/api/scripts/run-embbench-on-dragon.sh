#!/usr/bin/env bash
# Benchmark candidate on-prem embedding models on dragon03 (the idle, no-GPU node).
# Needs the VPN. Reports each model's on-topic vs off-topic separation, to compare
# against the +0.209 the hosted embedder gives today.
set -euo pipefail
SRC="${1:?usage: run-embbench-on-dragon.sh <staged-dir>}"
ssh dragon00 'mkdir -p ~/embbench'
scp -q "$SRC"/run.sbatch "$SRC"/bench.py "$SRC"/chunks.jsonl dragon00:~/embbench/
JOB=$(ssh dragon00 'cd ~/embbench && sbatch --parsable run.sbatch')
echo "submitted job $JOB on dragon03"
echo "  watch:  ssh dragon00 \"tail -f ~/embbench/embbench-$JOB.out\""
echo "  cancel: ssh dragon00 \"scancel $JOB\""
