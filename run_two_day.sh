#!/bin/bash
set -euo pipefail

cd /data1/pzhang || { echo "Cannot cd to /data1/pzhang"; exit 1; }
echo "Running lwa update script"
touch /data1/pzhang/update_lwaspectra
source /data1/pzhang/miniconda3/bin/activate
conda activate lwa
/data1/pzhang/miniconda3/envs/lwa/bin/python3 /data1/pzhang/lwasolarview/generate_all_spectra.py --mode original --lastnday 3
