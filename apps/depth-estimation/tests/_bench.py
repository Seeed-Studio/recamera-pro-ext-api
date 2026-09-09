"""Device-side micro-benchmark of the per-frame numpy reductions.

Run on the reCamera (nothing else running):
    /userdata/rknnenv/bin/python /userdata/local/bench/_bench.py
"""
import sys, time
sys.path.insert(0, "/userdata/local/apps/depth-estimation")
import numpy as np
import depth_map as dm

rng = np.random.RandomState(0)
view = (rng.rand(144, 256).astype(np.float32) * 800.0 + 20.0)

def bench(name, fn, n=50):
    fn()
    t0 = time.monotonic()
    for _ in range(n):
        fn()
    ms = (time.monotonic() - t0) / n * 1000.0
    print("%-24s %7.2f ms" % (name, ms))
    return ms

st = dm.frame_stats(view)
prox = dm.proximity(view, st["p5"], st["p95"])
bench("frame_stats", lambda: dm.frame_stats(view))
bench("proximity", lambda: dm.proximity(view, st["p5"], st["p95"]))
bench("grid_cells 4x3", lambda: dm.grid_cells(prox, 3, 4, 95.0))
bench("depth_map_payload", lambda: dm.depth_map_payload(prox), n=20)
def whole():
    s = dm.frame_stats(view)
    p = dm.proximity(view, s["p5"], s["p95"])
    dm.grid_cells(p, 3, 4, 95.0)
bench("stats+prox+grid", whole)
