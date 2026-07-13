#!/bin/bash
# ZRAY counter-placement optimization correctness study.
#
# Sweeps the three configurations (baseline / post-dom / post-dom+hoist) over the gapbs
# suite, running each kernel RUNS times per configuration at a fixed thread count, and
# collects the raw static (compile-time) and runtime logs into a results tree. Parsing,
# verdicts and plots are done by analyze_opt_study.py (kept separate so a re-parse never
# requires re-running the expensive builds/runs).
#
# ISOLATION: before EVERY run (not once per config) the whole toolchain is rebuilt from source
# — first the ZRAY library (pass tool.so + runtime tool_dyn.ll), then a clean build of the
# kernel against it. So every run starts from freshly regenerated artifacts: library binaries,
# kernel binary, test_log.zlog, StaticMixInfo.log, and a wiped kernel dir. This removes all
# shared build state between runs so any run-to-run drift cannot come from reused artifacts.
# Cost: 3*8*RUNS full library+kernel rebuilds (~8s library + kernel build each).
#
# Prereq: `source setupEnv.sh` from the customPass dir first (sets CUSTOM_*/ZRAY_BIN_PATH).
#
# Env knobs:
#   OMP_NUM_THREADS  fixed thread count for every run   (default 16)
#   RUNS             runs per (kernel, config)          (default 5)
set -u

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS="$BASE_DIR/opt-study-results-O3"

THREADS="${OMP_NUM_THREADS:-16}"
RUNS="${RUNS:-5}"
export OMP_NUM_THREADS="$THREADS"

# Kernel list and config filter are env-overridable for scoped test runs, e.g.
#   KERNELS_LIST="pr" ONLY_CONFIG="baseline" RUNS=3 ./run-opt-study.sh
read -r -a KERNELS <<< "${KERNELS_LIST:-bc bfs cc cc_sv pr pr_spmv sssp tc}"

# Per-kernel run arguments. Unweighted kernels use the hardcoded default graph
# (../../graphs/raw/twitterU.sg); sssp needs edge weights so it is pointed at twitter.wsg.
declare -A KARGS
KARGS[sssp]="-f ../../graphs/raw/twitter.wsg"

# name : POSTDOMSET : LOOPHOIST : PLACEMENT
# The three ZRAY arms sweep post-dom/hoist under placement=zray; the mst arm uses
# LLVM PGO's MST edge placement (post-dom/hoist ignored). See docs/adr/0003.
# "uninstrumented" (placement=none) is the execution-time reference: no tool_pass,
# no runtime, so it carries zero instrumentation overhead. It has no mix (loads/
# stores) — only timing. Listed first so the reference exists for slowdown even if
# the sweep is cut short.
CONFIGS=("uninstrumented:false:false:none" "baseline:false:false:zray" "postdom:true:false:zray" "postdom_hoist:true:true:zray" "mst:false:false:mst")
if [ -n "${ONLY_CONFIG:-}" ]; then
    CONFIGS=($(printf '%s\n' "${CONFIGS[@]}" | grep "^${ONLY_CONFIG}:"))
fi

if [ -z "${ZRAY_BIN_PATH:-}" ] || [ -z "${ZRAY_LOGFILE:-}" ]; then
    echo "ERROR: ZRAY_BIN_PATH/ZRAY_LOGFILE unset. Run 'source setupEnv.sh' from customPass first." >&2
    exit 1
fi

# Runtime activation. Without TOOL_INST the ZRAY runtime never initializes and records zeros
# (tool_dyn.cc:738). TOOL_PATCH_ID=0 profiles all groups. The runtime reads the per-kernel
# compile-time static profile from $ZRAY_LOGFILE (relative name; written into each kernel dir
# by the opt pass and read back when we run from that dir). XRAY_OPTIONS matches the authors'
# known-good recipe (scripts/run_mem_inst.sh) though the active path no longer uses XRay.
export TOOL_INST=1
export TOOL_PATCH_ID=0
export XRAY_OPTIONS="patch_premain=true"

# customPass dir (holds the ZRAY library sources + Makefile). ZRAY_BIN_PATH is <customPass>/bin.
CUSTOMPASS="$(dirname "$ZRAY_BIN_PATH")"

rm -rf "$RESULTS"
mkdir -p "$RESULTS"
{
    echo "date: $(date -Iseconds)"
    echo "threads: $THREADS"
    echo "runs_per_kernel_per_config: $RUNS"
    echo "clean_build_per_run: true (library + kernel)"
    echo "kernels: ${KERNELS[*]}"
    echo "configs: ${CONFIGS[*]}"
    echo "zray_bin_path: $ZRAY_BIN_PATH"
} > "$RESULTS/manifest.txt"

sum_metric() {  # $1=logfile  $2=grep-anchor  -> sum of last field over matching lines
    grep -- "$2" "$1" 2>/dev/null | awk '{s+=$NF} END{printf "%.0f", s+0}'
}

# Full clean rebuild of the WHOLE toolchain before a run: first the ZRAY library (pass tool.so
# + runtime tool_dyn.ll) from source, then a clean build of the kernel against that fresh
# library. make clean in src wipes *.ll/*.o/*.zlog/bin targets; make clean in gapbs wipes every
# kernel dir + intermediates. Guarantees fresh library binaries, fresh kernel binary, fresh
# test_log.zlog / StaticMixInfo.log and a fresh kernel dir — no reused build artifacts anywhere.
#   $1=kernel  $2=cfgname  $3=postdomset  $4=loophoist  $5=placement  -> 0 on success
build_kernel() {
    local log="$RESULTS/build_${2}_${1}.log"
    # 1) rebuild the ZRAY library (pass + runtime)
    make -C "$CUSTOMPASS/src" clean >/dev/null 2>&1
    make -C "$CUSTOMPASS" tool > "$log" 2>&1 || return 1
    # 2) clean build the kernel against the freshly built library
    make -C "$BASE_DIR" clean >/dev/null 2>&1
    if [ "$5" = "none" ]; then
        # Uninstrumented reference: no tool_pass, no runtime.
        make -C "$BASE_DIR" "${1}_uninstr" >> "$log" 2>&1
    else
        make -C "$BASE_DIR" POSTDOMSET="$3" LOOPHOIST="$4" PLACEMENT="$5" "$1" >> "$log" 2>&1
    fi
}

for cfg in "${CONFIGS[@]}"; do
    IFS=: read -r cname pds lh placement <<< "$cfg"
    echo "================================================================"
    echo "CONFIG: $cname   (postdomset=$pds loophoist=$lh)"
    echo "================================================================"
    mkdir -p "$RESULTS/static/$cname"

    for k in "${KERNELS[@]}"; do
        # GRAPH_OVERRIDE (e.g. "-f tiny.el -n 5") replaces the per-kernel graph
        # args for all kernels — handy for a fast end-to-end smoke test.
        kargs="${GRAPH_OVERRIDE:-${KARGS[$k]:-}}"
        mkdir -p "$RESULTS/runtime/$cname/$k"
        for r in $(seq 1 "$RUNS"); do
            # Full clean build before EVERY run — fresh metadata, zero shared state.
            if ! build_kernel "$k" "$cname" "$pds" "$lh" "$placement"; then
                echo "[$cname] BUILD FAILED for $k (run $r) — see $RESULTS/build_${cname}_${k}.log" >&2
                exit 1
            fi
            # The static log is regenerated identically each build; keep one copy per config/kernel.
            # (Uninstrumented has no tool_pass, hence no static log.)
            [ "$placement" != "none" ] && [ "$r" -eq 1 ] && cp "$BASE_DIR/src/${k}_static.log" "$RESULTS/static/$cname/${k}.log"

            cd "$BASE_DIR/$k"
            rm -f tool_log_file.txt zray_application_stats.csv post_process.out kernel_out.txt

            # Pick the binary: uninstrumented reference vs instrumented arm.
            if [ "$placement" = "none" ]; then bin="./${k}_uninstr"; else bin="./$k"; fi

            # Time the full run (wall clock) and capture the kernel's own per-trial
            # times from stdout. Per-run wall-clock cap (RUN_TIMEOUT) keeps a slow
            # instrumented kernel from stalling the unattended sweep.
            t0=$(date +%s.%N)
            timeout "${RUN_TIMEOUT:-2400}" $bin $kargs > kernel_out.txt 2>&1
            t1=$(date +%s.%N)
            wall=$(awk "BEGIN{printf \"%.4f\", $t1-$t0}")
            avg=$(grep -m1 'Average Time' kernel_out.txt | awk '{print $NF}')

            runlog="$RESULTS/runtime/$cname/$k/run${r}.log"
            if [ "$placement" = "none" ]; then
                cp kernel_out.txt "$runlog"; loads=NA; stores=NA
            elif [ "$placement" = "mst" ]; then
                # MST totals come from post_process (runtime's tool_log_file.txt is
                # empty in MST mode). Concatenate kernel_out.txt so the analyzer can
                # read both timing and mix from one run log.
                "$ZRAY_BIN_PATH/post_process" > post_process.out 2>&1
                cat post_process.out kernel_out.txt > "$runlog"
                loads=$(grep -m1 'MST totals' post_process.out | sed -E 's/.*loads=([0-9]+).*/\1/')
                stores=$(grep -m1 'MST totals' post_process.out | sed -E 's/.*stores=([0-9]+).*/\1/')
            else
                cat tool_log_file.txt kernel_out.txt > "$runlog"
                loads=$(sum_metric tool_log_file.txt '^TOTAL LOADS :')
                stores=$(sum_metric tool_log_file.txt '^TOTAL STORES:')
            fi
            # Timing metrics for the analyzer.
            { echo "WALLCLOCK: ${wall}"; echo "AVG_TRIAL: ${avg:-NA}"; } >> "$runlog"
            echo "  [$cname] $k run $r/$RUNS  loads=$loads stores=$stores wall=${wall}s avg=${avg:-NA}"
            cd "$BASE_DIR"
        done
    done
done

echo "================================================================"
echo "Done. Raw logs in $RESULTS"
echo "Next: python3 $BASE_DIR/analyze_opt_study.py"
