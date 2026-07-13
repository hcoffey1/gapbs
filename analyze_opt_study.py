#!/usr/bin/env python3
"""
Parse the raw logs collected by run-opt-study.sh, compute the correctness verdicts, render
the violin (runtime) and bar-chart (static) figures, and emit a self-contained HTML report.

Run inside the dataVis conda env:
    source ~/.zshrc; conda activate dataVis
    python analyze_opt_study.py [results_dir]

Outputs into <results_dir>:
    runtime.csv, static.csv, stability.csv, figures/*.png, report.html
"""
import base64
import io
import os
import re
import sys

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RESULTS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "opt-study-results")
FIG_DIR = os.path.join(RESULTS, "figures")
os.makedirs(FIG_DIR, exist_ok=True)

KERNELS = ["bc", "bfs", "cc", "cc_sv", "pr", "pr_spmv", "sssp", "tc"]
CONFIGS = ["baseline", "postdom", "postdom_hoist", "mst"]
CONFIG_LABEL = {"baseline": "baseline", "postdom": "post-dom", "postdom_hoist": "post-dom\n+hoist", "mst": "MST",
                "uninstrumented": "uninstr"}
# Performance (timing) plots include the uninstrumented reference; mix plots do not.
PERF_CONFIGS = ["uninstrumented", "baseline", "postdom", "postdom_hoist", "mst"]
# Arms whose mix must match the baseline (the correctness gate). post-dom and
# hoist are exact ZRAY optimizations; MST is a wholly separate placement whose
# reconstructed mix must still agree. See docs/adr/0003.
GATED_CONFIGS = ["postdom", "postdom_hoist", "mst"]

# Head-to-head trial-time comparison: ZRAY's best-optimized arm vs the MST arm.
CMP_CONFIGS = ["postdom_hoist", "mst"]
CMP_COLOR = {"postdom_hoist": "#4C78A8", "mst": "#F58518"}

# Static metrics grouped by expected role (ADR 0002).
INVARIANT_METRICS = ["loads_covered", "stores_covered", "bb_covered"]
EFFECT_METRICS = ["total_counters", "static_bbs_covered", "runtime_bbs_covered", "counters_hoisted"]

# ---------------------------------------------------------------- runtime parsing

RUNTIME_ANCHORS = {
    "total_loads": "TOTAL LOADS :",
    "total_stores": "TOTAL STORES:",
    "total_heap_loads": "TOTAL HEAP LOADS :",
    "total_heap_stores": "TOTAL HEAP STORES:",
}


def parse_runtime_log(path):
    """Sum each TOTAL metric across the per-thread dumps appended to the file.

    ZRAY arms log "TOTAL LOADS : N" lines (one set per thread). The MST arm's run
    log is instead post_process's output, which carries a single
    "MST totals: loads=N stores=M ..." line; parse that when present.
    """
    sums = {k: 0 for k in RUNTIME_ANCHORS}
    wallclock = None
    avg_trial = None
    with open(path, errors="replace") as fh:
        for line in fh:
            if line.startswith("WALLCLOCK:"):
                try:
                    wallclock = float(line.split()[-1])
                except ValueError:
                    pass
                continue
            if line.startswith("AVG_TRIAL:"):
                try:
                    avg_trial = float(line.split()[-1])
                except ValueError:
                    pass
                continue
            # The instrumented runtime floods stdout with its counter dump, so the
            # shell's AVG_TRIAL capture is NA for those arms. Recover the kernel's
            # own "Average Time:" line directly (present in every run log; Python
            # reads it binary-safe). Takes precedence over an NA AVG_TRIAL.
            if "Average Time:" in line:
                try:
                    avg_trial = float(line.split()[-1])
                except ValueError:
                    pass
                continue
            if line.startswith("MST totals:"):
                ml = re.search(r"loads=(\d+)", line)
                ms = re.search(r"stores=(\d+)", line)
                if ml:
                    sums["total_loads"] += int(ml.group(1))
                if ms:
                    sums["total_stores"] += int(ms.group(1))
                continue
            for key, anchor in RUNTIME_ANCHORS.items():
                if line.startswith(anchor):
                    try:
                        sums[key] += int(float(line.rsplit(None, 1)[-1]))
                    except ValueError:
                        pass
    sums["wallclock"] = wallclock
    sums["avg_trial"] = avg_trial
    return sums


def collect_runtime():
    rows = []
    base = os.path.join(RESULTS, "runtime")
    for cfg in PERF_CONFIGS:
        for k in KERNELS:
            kdir = os.path.join(base, cfg, k)
            if not os.path.isdir(kdir):
                continue
            for fn in sorted(os.listdir(kdir)):
                m = re.match(r"run(\d+)\.log", fn)
                if not m:
                    continue
                s = parse_runtime_log(os.path.join(kdir, fn))
                rows.append(dict(kernel=k, config=cfg, run=int(m.group(1)), **s))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- static parsing

def _num(text, pattern):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def parse_static_log(path):
    with open(path, errors="replace") as fh:
        text = fh.read()
    # The pass prints running totals after every function; the authoritative summary is the
    # final block emitted after "At end , list size is".
    idx = text.rfind("At end , list size is")
    tail = text[idx:] if idx != -1 else text

    out = {}
    m = re.search(r"At end , list size is (\d+) ROI count is : (\d+)", tail)
    if m:
        out["pragma_regions"] = int(m.group(1))
        out["roi_count"] = int(m.group(2))
    out["timing_events"] = _num(tail, r"Timing Events Inserted:\s*(\d+)")
    out["total_counters"] = _num(tail, r"Total Counters Inserted:\s*(\d+)")
    out["most_counters"] = _num(tail, r"Most Counters in Region:\s*(\d+)")
    out["bb_covered"] = _num(tail, r"BB Covered:\s*(\d+)")
    out["bb_ignored"] = _num(tail, r"BB Ignored:\s*(\d+)")
    out["loads_covered"] = _num(tail, r"Loads Covered:\s*(\d+)")
    out["stores_covered"] = _num(tail, r"Stores Covered:\s*(\d+)")
    out["counters_hoisted"] = _num(tail, r"Counters Hoisted\s+(\d+)")
    out["static_loops_hoisted"] = _num(tail, r"Static Loops Hoisted\s+(\d+)")
    out["runtime_loops_hoisted"] = _num(tail, r"Runtime Loops Hoisted\s+(\d+)")
    out["static_bbs_covered"] = _num(tail, r"Static Basic Blocks Covered :\s*(\d+)")
    out["runtime_bbs_covered"] = _num(tail, r"Runtime Basic Blocks Covered :\s*(\d+)")

    # Two loop-analysis blocks in order: application, then ROI-containing functions.
    loop_fields = ["total_loops", "static_loops", "dyn_loops", "invar_loops", "canonical_loops"]
    mode = None
    app, roi = {}, {}
    for line in tail.splitlines():
        if "Application Loop Analysis" in line:
            mode = "app"
        elif "Functions Containing ROI" in line:
            mode = "roi"
        if mode is None:
            continue
        target = app if mode == "app" else roi
        for label, field in [("Total Loops:", "total_loops"),
                             ("Static Bound Loops:", "static_loops"),
                             ("Dynamic Bound Loops:", "dyn_loops"),
                             ("Invariant Trip Counts:", "invar_loops"),
                             ("Canonical Loops:", "canonical_loops")]:
            mm = re.match(rf"\s*{re.escape(label)}\s*(\d+)", line)
            if mm and field not in target:
                target[field] = int(mm.group(1))
    for f in loop_fields:
        out["app_" + f] = app.get(f)
        out["roi_" + f] = roi.get(f)
    return out


def collect_static():
    rows = []
    for cfg in CONFIGS:
        for k in KERNELS:
            path = os.path.join(RESULTS, "static", cfg, k + ".log")
            if not os.path.isfile(path):
                continue
            rows.append(dict(kernel=k, config=cfg, **parse_static_log(path)))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- stability & verdicts

def compute_stability(rt):
    rows = []
    for k in KERNELS:
        b = rt[(rt.kernel == k) & (rt.config == "baseline")]
        if b.empty:
            continue
        lmin, lmax = int(b.total_loads.min()), int(b.total_loads.max())
        smin, smax = int(b.total_stores.min()), int(b.total_stores.max())
        stable = (lmin == lmax) and (smin == smax)
        def pct(lo, hi):
            return 0.0 if hi == 0 else 100.0 * (hi - lo) / hi
        rows.append(dict(kernel=k, stable=stable,
                         loads_min=lmin, loads_max=lmax, loads_spread_pct=round(pct(lmin, lmax), 4),
                         stores_min=smin, stores_max=smax, stores_spread_pct=round(pct(smin, smax), 4)))
    return pd.DataFrame(rows)


def verdicts(rt, stab):
    """Optimized config is correct if every run's totals fall within the baseline envelope."""
    rows = []
    for k in KERNELS:
        srow = stab[stab.kernel == k]
        if srow.empty:
            continue
        s = srow.iloc[0]
        for cfg in GATED_CONFIGS:
            c = rt[(rt.kernel == k) & (rt.config == cfg)]
            if c.empty:
                continue
            in_loads = (c.total_loads >= s.loads_min).all() and (c.total_loads <= s.loads_max).all()
            in_stores = (c.total_stores >= s.stores_min).all() and (c.total_stores <= s.stores_max).all()
            ok = bool(in_loads and in_stores)
            if ok and s.stable:
                verdict = "EXACT"
            elif ok:
                verdict = "WITHIN"
            else:
                verdict = "MISMATCH"
            # Worst deviation beyond the envelope, relative.
            def dev(vals, lo, hi):
                worst = 0.0
                for v in vals:
                    if v < lo and lo:
                        worst = max(worst, 100.0 * (lo - v) / lo)
                    elif v > hi and hi:
                        worst = max(worst, 100.0 * (v - hi) / hi)
                return round(worst, 4)
            rows.append(dict(kernel=k, config=cfg, verdict=verdict,
                             loads_dev_pct=dev(c.total_loads, s.loads_min, s.loads_max),
                             stores_dev_pct=dev(c.total_stores, s.stores_min, s.stores_max)))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- plotting

LOAD_COLOR = "#4C78A8"
STORE_COLOR = "#E45756"


def _violin(ax, data, positions, color):
    arrs = [np.asarray(d, dtype=float) for d in data]
    drawable = [(p, a) for p, a in zip(positions, arrs) if len(a) > 1 and a.std() > 0]
    if drawable:
        vp = ax.violinplot([a for _, a in drawable], positions=[p for p, _ in drawable],
                           widths=0.28, showmeans=False, showextrema=False)
        for body in vp["bodies"]:
            body.set_facecolor(color)
            body.set_edgecolor(color)
            body.set_alpha(0.35)
    # Always overlay raw points (jittered) so deterministic (flat) kernels are still legible.
    for p, a in zip(positions, arrs):
        if len(a) == 0:
            continue
        jitter = (np.random.RandomState(0).rand(len(a)) - 0.5) * 0.10
        ax.scatter(p + jitter, a, s=26, color=color, edgecolor="white", linewidth=0.5, zorder=3)


def plot_runtime_violins(rt):
    paths = {}
    xs = np.arange(len(CONFIGS))
    for k in KERNELS:
        kd = rt[rt.kernel == k]
        if kd.empty:
            continue
        loads = [kd[kd.config == c].total_loads.values for c in CONFIGS]
        stores = [kd[kd.config == c].total_stores.values for c in CONFIGS]
        fig, axL = plt.subplots(figsize=(6.2, 4.4))
        axR = axL.twinx()
        _violin(axL, loads, xs - 0.16, LOAD_COLOR)
        _violin(axR, stores, xs + 0.16, STORE_COLOR)
        axL.set_xticks(xs)
        axL.set_xticklabels([CONFIG_LABEL[c] for c in CONFIGS])
        axL.set_ylabel("Total loads", color=LOAD_COLOR)
        axR.set_ylabel("Total stores", color=STORE_COLOR)
        axL.tick_params(axis="y", labelcolor=LOAD_COLOR)
        axR.tick_params(axis="y", labelcolor=STORE_COLOR)
        axL.set_title(f"{k}: runtime loads/stores by configuration (n=5)")
        axL.grid(axis="y", alpha=0.15)
        # Keep magnitudes readable
        for ax in (axL, axR):
            ax.ticklabel_format(axis="y", style="sci", scilimits=(-3, 3))
        fig.tight_layout()
        p = os.path.join(FIG_DIR, f"runtime_{k}.png")
        fig.savefig(p, dpi=110)
        plt.close(fig)
        paths[k] = p
    return paths


PERF_COLOR = "#59A14F"


def _perf_violin_fig(rt, k, metric, ylabel, title):
    """One per-kernel violin: metric distribution (across runs) per PERF config,
    with the uninstrumented reference as the leftmost violin."""
    xs = np.arange(len(PERF_CONFIGS))
    kd = rt[rt.kernel == k]
    data = []
    for c in PERF_CONFIGS:
        vals = kd[kd.config == c][metric].dropna().values
        data.append(vals)
    if not any(len(d) for d in data):
        return None
    fig, ax = plt.subplots(figsize=(6.6, 4.4))
    _violin(ax, data, xs, PERF_COLOR)
    # Reference line at the uninstrumented median for quick slowdown reading.
    ref = data[0]
    if len(ref):
        ax.axhline(float(np.median(ref)), color="#888", lw=1, ls="--", alpha=0.7,
                   label="uninstr median")
        ax.legend(fontsize=8, loc="upper left")
    ax.set_xticks(xs)
    ax.set_xticklabels([CONFIG_LABEL[c] for c in PERF_CONFIGS])
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis="y", alpha=0.15)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    p = os.path.join(FIG_DIR, f"perf_{metric}_{k}.png")
    fig.savefig(p, dpi=110)
    plt.close(fig)
    return p


def plot_perf_violins(rt):
    """Two performance violins per workload: full-app wall-clock and mean trial
    time, each by configuration against the uninstrumented reference."""
    wall_paths, trial_paths = {}, {}
    for k in KERNELS:
        pw = _perf_violin_fig(rt, k, "wallclock", "Wall-clock time (s)",
                              f"{k}: full-application wall-clock by configuration")
        if pw:
            wall_paths[k] = pw
        pt = _perf_violin_fig(rt, k, "avg_trial", "Mean trial time (s)",
                              f"{k}: average trial time by configuration")
        if pt:
            trial_paths[k] = pt
    return wall_paths, trial_paths


def _trial_stats(rt):
    """Per kernel: uninstrumented median trial time, plus mean/std trial time for
    each compared config. Only kernels with an uninstrumented reference and at least
    one compared config present are kept."""
    kept, stats = [], {}
    for k in KERNELS:
        u = rt[(rt.kernel == k) & (rt.config == "uninstrumented")].avg_trial.dropna()
        if u.empty or float(u.median()) <= 0:
            continue
        row = {"uninstr": float(u.median())}
        have = False
        for c in CMP_CONFIGS:
            v = rt[(rt.kernel == k) & (rt.config == c)].avg_trial.dropna()
            row[c] = (float(v.mean()), float(v.std(ddof=0))) if not v.empty else (None, None)
            have = have or not v.empty
        if have:
            kept.append(k)
            stats[k] = row
    return kept, stats


def plot_trial_comparison(rt):
    """Two cross-workload charts comparing post-dom+hoist vs MST trial time:
    (1) mean trial normalized to each workload's uninstrumented median (=1, dashed);
    (2) head-to-head % difference so the per-workload winner is obvious."""
    kept, stats = _trial_stats(rt)
    if not kept:
        return {}
    paths = {}
    x = np.arange(len(kept))
    figw = max(7.0, 1.15 * len(kept))

    # --- Chart 1: normalized-to-uninstrumented grouped bars ---
    width = 0.38
    fig, ax = plt.subplots(figsize=(figw, 4.6))
    for i, c in enumerate(CMP_CONFIGS):
        means = [stats[k][c][0] / stats[k]["uninstr"] if stats[k][c][0] is not None else np.nan
                 for k in kept]
        errs = [stats[k][c][1] / stats[k]["uninstr"] if stats[k][c][1] is not None else 0
                for k in kept]
        bars = ax.bar(x + (i - 0.5) * width, means, width, yerr=errs, capsize=3,
                      color=CMP_COLOR[c], edgecolor="black", linewidth=0.5,
                      label=CONFIG_LABEL[c].replace("\n", " "))
        for b, m in zip(bars, means):
            if not np.isnan(m):
                ax.annotate(f"{m:.2f}", (b.get_x() + b.get_width() / 2, m),
                            ha="center", va="bottom", fontsize=7)
    ax.axhline(1.0, color="#555", ls="--", lw=1.2, label="uninstrumented (median)")
    ax.set_xticks(x)
    ax.set_xticklabels(kept)
    ax.set_ylabel("Mean trial time ÷ uninstrumented median")
    ax.set_title("Trial-time slowdown vs uninstrumented — post-dom+hoist vs MST")
    ax.grid(axis="y", alpha=0.15)
    ax.legend(fontsize=8)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    p1 = os.path.join(FIG_DIR, "trial_cmp_normalized.png")
    fig.savefig(p1, dpi=110)
    plt.close(fig)
    paths["norm"] = p1

    # --- Chart 2: head-to-head MST vs post-dom+hoist (% difference, diverging) ---
    from matplotlib.patches import Patch
    fig, ax = plt.subplots(figsize=(figw, 4.4))
    diffs, colors = [], []
    for k in kept:
        mh, mm = stats[k]["postdom_hoist"][0], stats[k]["mst"][0]
        d = (mm / mh - 1.0) * 100.0 if (mh and mm) else np.nan
        diffs.append(d)
        # Negative => MST faster (better); positive => post-dom+hoist faster.
        colors.append(CMP_COLOR["mst"] if (d == d and d < 0) else CMP_COLOR["postdom_hoist"])
    bars = ax.bar(x, diffs, 0.6, color=colors, edgecolor="black", linewidth=0.5)
    ax.axhline(0.0, color="#555", ls="--", lw=1.2)
    for b, d in zip(bars, diffs):
        if d == d:
            ax.annotate(f"{d:+.1f}%", (b.get_x() + b.get_width() / 2, d),
                        ha="center", va="bottom" if d >= 0 else "top", fontsize=7)
    ax.set_xticks(x)
    ax.set_xticklabels(kept)
    ax.set_ylabel("MST trial time vs post-dom+hoist (%)")
    ax.set_title("Head-to-head: MST vs post-dom+hoist  (below 0 = MST faster)")
    ax.grid(axis="y", alpha=0.15)
    ax.legend(handles=[Patch(facecolor=CMP_COLOR["mst"], edgecolor="black", label="MST faster"),
                       Patch(facecolor=CMP_COLOR["postdom_hoist"], edgecolor="black",
                             label="post-dom+hoist faster")], fontsize=8)
    fig.tight_layout()
    p2 = os.path.join(FIG_DIR, "trial_cmp_ratio.png")
    fig.savefig(p2, dpi=110)
    plt.close(fig)
    paths["ratio"] = p2
    return paths


def plot_counter_comparison(st):
    """Head-to-head static-counter footprint: % difference in the number of counters
    inserted by MST vs post-dom+hoist, per workload. Below 0 = MST inserts fewer."""
    piv = st.pivot_table(index="kernel", columns="config", values="total_counters", aggfunc="first")
    kept = [k for k in KERNELS if k in piv.index
            and pd.notna(piv.loc[k].get("postdom_hoist")) and pd.notna(piv.loc[k].get("mst"))]
    if not kept:
        return None
    x = np.arange(len(kept))
    from matplotlib.patches import Patch
    fig, ax = plt.subplots(figsize=(max(7.0, 1.15 * len(kept)), 4.4))
    diffs, colors = [], []
    for k in kept:
        h, m = piv.loc[k, "postdom_hoist"], piv.loc[k, "mst"]
        d = (m / h - 1.0) * 100.0
        diffs.append(d)
        # Negative => MST inserts fewer counters (leaner); positive => hoist fewer.
        colors.append(CMP_COLOR["mst"] if d < 0 else CMP_COLOR["postdom_hoist"])
    bars = ax.bar(x, diffs, 0.6, color=colors, edgecolor="black", linewidth=0.5)
    ax.axhline(0.0, color="#555", ls="--", lw=1.2)
    for b, d in zip(bars, diffs):
        ax.annotate(f"{d:+.1f}%", (b.get_x() + b.get_width() / 2, d),
                    ha="center", va="bottom" if d >= 0 else "top", fontsize=7)
    ax.set_xticks(x)
    ax.set_xticklabels(kept)
    ax.set_ylabel("MST counters vs post-dom+hoist (%)")
    ax.set_title("Static counters inserted: MST vs post-dom+hoist  (below 0 = MST fewer)")
    ax.grid(axis="y", alpha=0.15)
    ax.legend(handles=[Patch(facecolor=CMP_COLOR["mst"], edgecolor="black", label="MST fewer"),
                       Patch(facecolor=CMP_COLOR["postdom_hoist"], edgecolor="black",
                             label="post-dom+hoist fewer")], fontsize=8)
    fig.tight_layout()
    p = os.path.join(FIG_DIR, "counter_cmp_ratio.png")
    fig.savefig(p, dpi=110)
    plt.close(fig)
    return p


def plot_static_bars(st):
    paths = {}
    for k in KERNELS:
        kd = st[st.kernel == k].set_index("config")
        if kd.empty:
            continue
        metrics = INVARIANT_METRICS + EFFECT_METRICS
        fig, axes = plt.subplots(2, 4, figsize=(13, 6))
        axes = axes.ravel()
        for ax, metric in zip(axes, metrics):
            vals = [kd.loc[c, metric] if c in kd.index and pd.notna(kd.loc[c, metric]) else 0
                    for c in CONFIGS]
            is_inv = metric in INVARIANT_METRICS
            bars = ax.bar(range(len(CONFIGS)), vals,
                          color=["#59A14F" if is_inv else "#B279A2"] * len(CONFIGS),
                          edgecolor="black", linewidth=0.5)
            ax.set_xticks(range(len(CONFIGS)))
            ax.set_xticklabels([CONFIG_LABEL[c] for c in CONFIGS], fontsize=8)
            role = "invariant" if is_inv else "effect"
            ax.set_title(f"{metric}\n({role})", fontsize=9)
            ax.grid(axis="y", alpha=0.15)
            for b, v in zip(bars, vals):
                ax.annotate(f"{int(v)}", (b.get_x() + b.get_width() / 2, v),
                            ha="center", va="bottom", fontsize=7)
        # Hide any unused subplot (7 metrics in an 8-slot grid).
        for ax in axes[len(metrics):]:
            ax.set_visible(False)
        fig.suptitle(f"{k}: static instrumentation metrics by configuration", fontsize=12)
        fig.tight_layout(rect=(0, 0, 1, 0.96))
        p = os.path.join(FIG_DIR, f"static_{k}.png")
        fig.savefig(p, dpi=110)
        plt.close(fig)
        paths[k] = p
    return paths


# ---------------------------------------------------------------- HTML report

def b64(path):
    with open(path, "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode()


def build_html(stab, verd, rt_paths, st_paths, static_df, rt, wall_paths=None, trial_paths=None,
               trial_cmp_paths=None, counter_cmp_path=None):
    wall_paths = wall_paths or {}
    trial_paths = trial_paths or {}
    trial_cmp_paths = trial_cmp_paths or {}
    parts = ["<h1>ZRAY optimization-correctness study — gapbs</h1>"]

    # Verdict summary: for each gated config, the % difference of its mean loads and
    # mean stores against the baseline ZRAY mean (per kernel). Two subcolumns per config.
    parts.append("<h2>Runtime correctness verdict</h2>")

    def _cfg_mean(k, cfg, metric):
        d = rt[(rt.kernel == k) & (rt.config == cfg)][metric].dropna()
        return float(d.mean()) if len(d) else None

    def _pct(val, base):
        if val is None or base is None or base == 0:
            return None
        return 100.0 * (val - base) / base

    def _cell(pct):
        # Color by magnitude of the difference: green ~identical, amber small, red >1%.
        if pct is None:
            return '<td style="color:#666">—</td>'
        col = "#1a7f37" if abs(pct) < 0.1 else ("#9a6700" if abs(pct) <= 1.0 else "#cf222e")
        return f'<td style="color:{col};font-weight:600">{pct:+.3g}%</td>'

    top = "".join(f'<th colspan="2">{CONFIG_LABEL.get(c, c).replace(chr(10), " ")}</th>'
                  for c in GATED_CONFIGS)
    sub = "".join("<th>loads</th><th>stores</th>" for _ in GATED_CONFIGS)
    parts.append('<table><thead>'
                 '<tr><th rowspan="2">kernel</th>' + top + '</tr>'
                 '<tr>' + sub + '</tr></thead><tbody>')
    for k in KERNELS:
        srow = stab[stab.kernel == k]
        if srow.empty:
            continue
        bl = _cfg_mean(k, "baseline", "total_loads")
        bs = _cfg_mean(k, "baseline", "total_stores")
        cells = [f"<td><b>{k}</b></td>"]
        for cfg in GATED_CONFIGS:
            cells.append(_cell(_pct(_cfg_mean(k, cfg, "total_loads"), bl)))
            cells.append(_cell(_pct(_cfg_mean(k, cfg, "total_stores"), bs)))
        parts.append("<tr>" + "".join(cells) + "</tr>")
    parts.append("</tbody></table>")
    parts.append('<p class="legend">Each cell is the % difference of that configuration\'s mean '
                 'vs the <b>baseline ZRAY</b> mean for that metric, averaged over runs. '
                 '<span style="color:#1a7f37">green</span> = &lt;0.1% &nbsp;·&nbsp; '
                 '<span style="color:#9a6700">amber</span> = ≤1% &nbsp;·&nbsp; '
                 '<span style="color:#cf222e">red</span> = &gt;1%. '
                 'A large diff can reflect baseline run-to-run noise rather than a real divergence '
                 '(e.g. <code>pr</code>, whose Gauss-Seidel convergence varies per run).</p>')

    # Trial-time head-to-head: post-dom+hoist vs MST
    if trial_cmp_paths:
        parts.append("<h2>Trial-time comparison: post-dom+hoist vs MST</h2>")
        parts.append('<p class="note">Mean per-trial time (across runs) for ZRAY\'s best-optimized '
                     'arm (post-dom+hoist) and the MST arm, normalized to each workload\'s '
                     '<b>uninstrumented median</b> (dashed line = 1). Lower is faster; error bars '
                     'show run-to-run spread.</p>')
        if "norm" in trial_cmp_paths:
            parts.append(f'<img src="{b64(trial_cmp_paths["norm"])}" alt="normalized trial-time comparison">')
        if "ratio" in trial_cmp_paths:
            parts.append('<p class="note">Head-to-head, per workload: a bar <b>below 0</b> '
                         '(orange) means MST has the faster trial time; <b>above 0</b> (blue) means '
                         'post-dom+hoist is faster. <br>Note: <code>pr</code>\'s trial time is '
                         'non-deterministic (Gauss-Seidel convergence varies run-to-run), so treat '
                         'its bar as indicative only — <code>pr_spmv</code> is the deterministic '
                         'PageRank comparison.</p>')
            parts.append(f'<img src="{b64(trial_cmp_paths["ratio"])}" alt="MST vs post-dom+hoist head-to-head">')

    # Static counter footprint head-to-head: post-dom+hoist vs MST
    if counter_cmp_path:
        parts.append("<h2>Static counter footprint: post-dom+hoist vs MST</h2>")
        parts.append('<p class="note">% difference in the number of static counters inserted at '
                     'compile time by MST vs post-dom+hoist, per workload. A bar <b>below 0</b> '
                     '(orange) means MST inserts fewer counters; <b>above 0</b> (blue) means '
                     'post-dom+hoist inserts fewer. Unlike trial time, this is a deterministic '
                     'compile-time count (one value per configuration).</p>')
        parts.append(f'<img src="{b64(counter_cmp_path)}" alt="MST vs post-dom+hoist static counters">')

    # Per-kernel sections
    for k in KERNELS:
        parts.append(f"<h2>{k}</h2>")
        if k in wall_paths:
            parts.append(f'<img src="{b64(wall_paths[k])}" alt="wall-clock violins {k}">')
        if k in trial_paths:
            parts.append(f'<img src="{b64(trial_paths[k])}" alt="trial-time violins {k}">')
        if k in rt_paths:
            parts.append(f'<img src="{b64(rt_paths[k])}" alt="runtime violins {k}">')
        if k in st_paths:
            parts.append(f'<img src="{b64(st_paths[k])}" alt="static bars {k}">')
        inv = static_df[static_df.kernel == k]
        if not inv.empty:
            same = all(inv[m].nunique() == 1 for m in INVARIANT_METRICS if m in inv)
            note = ("Static invariants identical across configs ✓" if same
                    else "⚠ a static invariant differs across configs — investigate")
            parts.append(f'<p class="note">{note}</p>')

    css = """
    <style>
      :root { color-scheme: light dark; }
      body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 1100px;
             margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
      h1 { border-bottom: 2px solid #8884; padding-bottom: .3rem; }
      h2 { margin-top: 2.2rem; }
      table { border-collapse: collapse; margin: 1rem 0; }
      th, td { border: 1px solid #8886; padding: .35rem .7rem; text-align: center; }
      th { background: #8882; }
      img { max-width: 100%; height: auto; display: block; margin: .8rem 0;
            border: 1px solid #8883; border-radius: 6px; }
      .legend, .note { font-size: .9rem; color: #666; }
      .note { margin: .2rem 0 1rem; }
      @media (prefers-color-scheme: dark) { body { background: #0d1117; color: #c9d1d9; } }
    </style>"""
    html = css + "\n".join(parts)
    out = os.path.join(RESULTS, "report.html")
    with open(out, "w") as fh:
        fh.write(html)
    return out


def main():
    rt = collect_runtime()
    st = collect_static()
    rt.to_csv(os.path.join(RESULTS, "runtime.csv"), index=False)
    st.to_csv(os.path.join(RESULTS, "static.csv"), index=False)
    if rt.empty:
        print("No runtime logs found under", RESULTS)
        return
    stab = compute_stability(rt)
    verd = verdicts(rt, stab)
    stab.to_csv(os.path.join(RESULTS, "stability.csv"), index=False)
    verd.to_csv(os.path.join(RESULTS, "verdicts.csv"), index=False)
    rt_paths = plot_runtime_violins(rt)
    st_paths = plot_static_bars(st)
    wall_paths, trial_paths = plot_perf_violins(rt)
    trial_cmp_paths = plot_trial_comparison(rt)
    counter_cmp_path = plot_counter_comparison(st)
    out = build_html(stab, verd, rt_paths, st_paths, st, rt, wall_paths, trial_paths, trial_cmp_paths,
                     counter_cmp_path)

    print("\n=== stability ===")
    print(stab.to_string(index=False))
    print("\n=== verdicts ===")
    print(verd.to_string(index=False))
    print("\nReport:", out)


if __name__ == "__main__":
    main()
