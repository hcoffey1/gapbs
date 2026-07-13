#!/bin/bash
export HOST="${HOST:-}"
cd /users/hjcoffey/zray/customPass && . ./setupEnv.sh
export TOOL_INST=1 TOOL_PATCH_ID=0 XRAY_OPTIONS="patch_premain=true" OMP_NUM_THREADS=16
BASE=/users/hjcoffey/zray/gapbs
for r in 1 2 3; do
  make -C "$BASE" clean >/dev/null 2>&1
  make -C "$BASE" POSTDOMSET=false LOOPHOIST=false pr >/dev/null 2>&1 || { echo "build $r failed"; exit 1; }
  zlog=$(md5sum "$BASE/pr/test_log.zlog" | cut -d' ' -f1)
  cd "$BASE/pr"; rm -f tool_log_file.txt zray_application_stats.csv
  ./pr >/dev/null 2>&1
  loads=$(grep '^TOTAL LOADS :' tool_log_file.txt | awk '{s+=$NF} END{print s}')
  t0=$(grep '^TOTAL LOADS :' tool_log_file.txt | head -1 | awk '{print $NF}')
  echo "run $r (fresh build): sum16=$loads  thread0=$t0  zlog_md5=$zlog"
  cd "$BASE"
done
echo VALIDATE DONE
