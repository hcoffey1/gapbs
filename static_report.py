#!/usr/bin/env python3
"""
Static-only analysis of the ZRAY opt-study: parse the per-kernel compile-time static logs for
all three configurations, check the coverage/loop invariants, quantify the optimization effect,
render the per-kernel bar charts, and emit a self-contained HTML report.

This is independent of the runtime load/store data (which has a separate accumulation bug); the
static metrics are compile-time and fully trustworthy.

    source ~/.zshrc; conda activate dataVis
    python static_report.py
"""
import base64
import os

import pandas as pd

import analyze_opt_study as a  # reuse parse_static_log, plot_static_bars, CONFIG_LABEL, metric groups

RESULTS = a.RESULTS
os.makedirs(a.FIG_DIR, exist_ok=True)

# Corrected classification (see ADR 0002):
#  - LOOP_INVARIANT: pure properties of the input code — identical across ALL three configs.
#  - COVERAGE: identical between baseline and post-dom (post-dom preserves coverage); the
#    hoist config intentionally RESCALES these by loop scale factors, so it is shown but not
#    treated as a failure.
#  - EFFECT: expected to change — counter reduction and hoisting.
LOOP_INVARIANT = ["app_total_loops", "app_static_loops", "app_dyn_loops",
                  "app_invar_loops", "app_canonical_loops"]
COVERAGE = ["loads_covered", "stores_covered", "bb_covered"]
EFFECT = a.EFFECT_METRICS                            # total_counters, static/runtime bbs, counters_hoisted


def main():
    st = a.collect_static()
    st.to_csv(os.path.join(RESULTS, "static.csv"), index=False)

    # Per-kernel bar charts (reuse the validated plotting).
    st_paths = a.plot_static_bars(st)

    VC = {True: ("#1a7f37", "identical ✓"), False: ("#cf222e", "DIFFERS ✗")}
    parts = ["<title>ZRAY static instrumentation analysis — gapbs</title>",
             "<p class='eyebrow'>ZRAY · counter-placement study</p>",
             "<h1>Static instrumentation analysis across the gapbs suite</h1>",
             "<p class='lede'>Compile-time metrics for the ZRAY pass under three counter-placement "
             "configurations — <b>baseline</b> (one counter per basic block), <b>post-dom</b> "
             "(post-domination-set grouping), and <b>post-dom&nbsp;+&nbsp;hoist</b> (loop hoisting on "
             "top) — over all eight gapbs kernels. Runtime load/store data is excluded pending a "
             "separate accumulation fix; every number here is compile-time and trustworthy.</p>"]

    # --- (1) True invariants: loop structure, must be identical across ALL three configs.
    parts.append("<h2>1. True invariants — loop structure (all three configs)</h2>")
    parts.append("<p class='sub'>Pure properties of the input code, independent of counter "
                 "placement. Must be identical across every config; a difference would mean the "
                 "pass saw different code.</p>")
    parts.append("<table><thead><tr><th>kernel</th>"
                 + "".join(f"<th>{m.replace('app_','')}</th>" for m in LOOP_INVARIANT)
                 + "<th>verdict</th></tr></thead><tbody>")
    loops_ok = True
    for k in a.KERNELS:
        kd = st[st.kernel == k]
        if kd.empty:
            continue
        cells = [f"<td><b>{k}</b></td>"]
        row_ok = True
        for m in LOOP_INVARIANT:
            vals = kd.set_index("config").reindex(a.CONFIGS)[m]
            same = vals.nunique(dropna=False) == 1
            row_ok &= same
            disp = int(vals.iloc[0]) if pd.notna(vals.iloc[0]) else "?"
            style = "" if same else " style='color:#cf222e;font-weight:700'"
            cells.append(f"<td{style}>{disp}</td>")
        loops_ok &= row_ok
        color, label = VC[row_ok]
        cells.append(f"<td style='color:{color};font-weight:600'>{label}</td>")
        parts.append("<tr>" + "".join(cells) + "</tr>")
    parts.append("</tbody></table>")

    # --- (2) Coverage: baseline vs post-dom must match; hoist intentionally rescales.
    parts.append("<h2>2. Coverage — post-dom preserves it, hoisting rescales it</h2>")
    parts.append("<p class='sub'>Static loads/stores/basic-blocks covered. Post-dom grouping "
                 "must leave these <b>identical to baseline</b> (it only collapses counters). "
                 "The hoist config intentionally re-weights loop-body loads/stores by each "
                 "loop's scale factor, so its values change by design — shown for reference, not "
                 "a failure.</p>")
    parts.append("<table><thead><tr><th>kernel</th><th>metric</th>"
                 "<th>baseline</th><th>post-dom</th><th>base==post-dom?</th>"
                 "<th>post-dom+hoist</th></tr></thead><tbody>")
    cov_ok = True
    for k in a.KERNELS:
        kd = st[st.kernel == k].set_index("config")
        if kd.empty:
            continue
        for m in COVERAGE:
            b = int(kd.loc["baseline", m]) if "baseline" in kd.index else 0
            p = int(kd.loc["postdom", m]) if "postdom" in kd.index else 0
            h = int(kd.loc["postdom_hoist", m]) if "postdom_hoist" in kd.index else 0
            same = (b == p)
            cov_ok &= same
            color, label = VC[same]
            parts.append("<tr><td><b>" + (k if m == COVERAGE[0] else "") + "</b></td>"
                         + f"<td>{m}</td><td>{b}</td><td>{p}</td>"
                         + f"<td style='color:{color};font-weight:600'>{label}</td><td>{h}</td></tr>")
    parts.append("</tbody></table>")

    all_ok = loops_ok and cov_ok
    banner = ("Loop-structure invariants hold across all configs, and post-dom preserves coverage "
              "exactly — the optimizations changed counter placement only (hoisting additionally "
              "rescales coverage by design)." if all_ok else
              "A loop invariant or a baseline↔post-dom coverage value differs — see red cells.")
    parts.append(f"<p class='banner {'ok' if all_ok else 'bad'}'>{banner}</p>")

    # --- Optimization effect: counter reduction & hoisting.
    parts.append("<h2>Optimization effect — counters &amp; hoisting</h2>")
    parts.append("<p class='sub'>Expected to change. Post-dom groups basic blocks under shared "
                 "counters; loop hoisting lifts loop-body counters out with a scale factor.</p>")
    parts.append("<table><thead><tr><th>kernel</th><th>metric</th>"
                 + "".join(f"<th>{a.CONFIG_LABEL[c].replace(chr(10),' ')}</th>" for c in a.CONFIGS)
                 + "<th>hoist vs baseline</th></tr></thead><tbody>")
    for k in a.KERNELS:
        kd = st[st.kernel == k].set_index("config")
        if kd.empty:
            continue
        for m in EFFECT:
            vals = [kd.loc[c, m] if c in kd.index and pd.notna(kd.loc[c, m]) else 0 for c in a.CONFIGS]
            base = vals[0] or 0
            hoist = vals[-1] or 0
            if m == "total_counters" and base:
                delta = f"{100.0*(base-hoist)/base:+.1f}% counters"
            elif m == "counters_hoisted":
                delta = f"{int(hoist)} hoisted"
            else:
                delta = f"{int(hoist)}"
            parts.append("<tr><td><b>" + (k if m == EFFECT[0] else "") + "</b></td>"
                         + f"<td>{m}</td>"
                         + "".join(f"<td>{int(v)}</td>" for v in vals)
                         + f"<td>{delta}</td></tr>")
    parts.append("</tbody></table>")

    # --- Per-kernel figures.
    parts.append("<h2>Per-kernel static metrics</h2>")
    for k in a.KERNELS:
        if k in st_paths:
            with open(st_paths[k], "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            parts.append(f"<h3>{k}</h3><span class='fig'>"
                         f"<img src='data:image/png;base64,{b64}' alt='static metrics for {k}'></span>")

    css = """<style>
      /* Palette grounded in the subject (LLVM/ZRAY instrumentation): cool slate neutrals with a
         blue bias, an indigo accent for structure, and semantic good/bad kept separate. Charts
         echo load=blue / store=red. Tokens drive both themes. */
      :root{
        --bg:#f5f7fa; --surface:#ffffff; --paper:#ffffff;
        --ink:#1c2530; --muted:#5b6b7d; --line:#d3dbe4;
        --accent:#3b5bdb; --accent-soft:#3b5bdb18;
        --good:#1a7f37; --good-soft:#1a7f3718; --bad:#c0362c; --bad-soft:#c0362c18;
        --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Code",Menlo,Consolas,monospace;
        --sans:ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
        color-scheme:light dark;
      }
      @media (prefers-color-scheme:dark){
        :root{
          --bg:#0d1117; --surface:#161b22; --paper:#f7f9fc;
          --ink:#e6edf3; --muted:#93a1b1; --line:#2a3340;
          --accent:#7c93f5; --accent-soft:#7c93f52a;
          --good:#3fb950; --good-soft:#3fb95022; --bad:#f0705f; --bad-soft:#f0705f22;
        }
      }
      :root[data-theme="light"]{
        --bg:#f5f7fa; --surface:#ffffff; --paper:#ffffff;
        --ink:#1c2530; --muted:#5b6b7d; --line:#d3dbe4;
        --accent:#3b5bdb; --accent-soft:#3b5bdb18;
        --good:#1a7f37; --good-soft:#1a7f3718; --bad:#c0362c; --bad-soft:#c0362c18;
      }
      :root[data-theme="dark"]{
        --bg:#0d1117; --surface:#161b22; --paper:#f7f9fc;
        --ink:#e6edf3; --muted:#93a1b1; --line:#2a3340;
        --accent:#7c93f5; --accent-soft:#7c93f52a;
        --good:#3fb950; --good-soft:#3fb95022; --bad:#f0705f; --bad-soft:#f0705f22;
      }
      *{box-sizing:border-box}
      body{background:var(--bg);color:var(--ink);font-family:var(--sans);
           max-width:1080px;margin:0 auto;padding:3rem 1.25rem 5rem;line-height:1.55;
           -webkit-font-smoothing:antialiased}
      .eyebrow{font-family:var(--mono);font-size:.72rem;letter-spacing:.18em;text-transform:uppercase;
               color:var(--accent);margin:0 0 .5rem}
      h1{font-size:clamp(1.7rem,3.2vw,2.3rem);line-height:1.15;margin:0 0 .6rem;text-wrap:balance;
         letter-spacing:-.01em}
      h2{font-size:1.28rem;margin:3rem 0 .3rem;padding-top:1.2rem;border-top:1px solid var(--line);
         text-wrap:balance;letter-spacing:-.005em}
      h3{font-family:var(--mono);font-size:1rem;margin:1.8rem 0 .4rem;color:var(--accent)}
      p{max-width:70ch}
      .sub{color:var(--muted);font-size:.92rem}
      .lede{font-size:1.02rem;color:var(--muted);max-width:72ch}
      .tw{overflow-x:auto;margin:1.1rem 0;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
      table{border-collapse:collapse;width:100%;font-size:.85rem;font-variant-numeric:tabular-nums}
      th,td{border-bottom:1px solid var(--line);padding:.5rem .7rem;text-align:right}
      th{background:var(--accent-soft);color:var(--ink);font-weight:600;font-family:var(--mono);
         font-size:.74rem;letter-spacing:.04em;text-transform:uppercase;position:sticky;top:0}
      td:first-child,th:first-child,td:nth-child(2){text-align:left}
      tbody tr:last-child td{border-bottom:none}
      td{font-family:var(--mono);color:var(--ink)}
      .fig{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:.75rem;
           margin:.6rem 0 1.4rem;box-shadow:0 1px 2px #0000000d}
      .fig img{max-width:100%;height:auto;display:block;border-radius:4px}
      .banner{padding:.8rem 1rem;border-radius:10px;font-weight:600;font-size:.92rem;margin:1.2rem 0;
              border:1px solid transparent}
      .banner.ok{background:var(--good-soft);color:var(--good);border-color:var(--good)}
      .banner.bad{background:var(--bad-soft);color:var(--bad);border-color:var(--bad)}
    </style>"""
    # Wrap tables in scroll containers (figures are already carded at emission).
    html = css + "\n".join(parts)
    html = html.replace("<table>", "<div class='tw'><table>").replace("</table>", "</table></div>")
    out = os.path.join(RESULTS, "static_report.html")
    with open(out, "w") as fh:
        fh.write(html)

    # Console summary
    print("Static invariants hold across all configs:", all_ok)
    print("\nTotal counters inserted (baseline / post-dom / hoist):")
    for k in a.KERNELS:
        kd = st[st.kernel == k].set_index("config")
        if kd.empty:
            continue
        tc = [int(kd.loc[c, "total_counters"]) if c in kd.index else 0 for c in a.CONFIGS]
        red = 100.0*(tc[0]-tc[2])/tc[0] if tc[0] else 0
        print(f"  {k:8} {tc[0]:>7} / {tc[1]:>7} / {tc[2]:>7}   ({red:+.1f}% vs baseline)")
    print("\nReport:", out)
    print("CSV:", os.path.join(RESULTS, "static.csv"))


if __name__ == "__main__":
    main()
