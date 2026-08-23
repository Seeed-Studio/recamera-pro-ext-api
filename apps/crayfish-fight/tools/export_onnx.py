#!/usr/bin/env python3
"""
crayfish-fight -- ONNX export for the three device models (run on the Mac).

Three models, two export shapes:

  * detector (yolo11n / yolo11s, single class "crayfish", 640x640)
      RAW HEAD. ultralytics' default detect export folds DFL + decode + concat
      into one [1, 4+nc, 8400] tensor whose box coords (pixel scale) and class
      scores (0-1) share one INT8 quantization range -- the class values
      collapse. So the graph is cut at the 6 head leaf-Convs
      (`/cvN.i.2/Conv_output_0`, N in {2,3}) and DFL/NMS run device-side in
      `kit.runtime.postprocess.detect`. The name pattern matched here is the
      SAME one `models/convert/convert.py` uses (`_LEAF_RE`), so a raw-head
      ONNX produced here and `convert.py --yolo-head detect` agree on the
      branch set.
  * classifiers (sex 128x128 / behavior 224x224)
      WHOLE GRAPH. A yolo11-cls head is Conv -> Pool -> Linear -> Softmax --
      nothing there is unsupported or quantization-hostile, so no surgery.

Everything is exported with a static batch of 1 and a static input size (the
NPU compiler needs static shapes).

Usage (Mac):
    uv run --with ultralytics --with onnx python \
        apps/crayfish-fight/tools/export_onnx.py \
        --runs-dir /Users/harvest/project/crayfish-fight/runs \
        --out-dir  /Users/harvest/project/crayfish-fight/export

    # or a single model
    ... export_onnx.py --weights runs/sex_cls/best.pt --kind classify \
        --imgsz 128 --out export/sex_cls.onnx

Then check each artefact:
    python models/convert/inspect_onnx.py export/det_v1_n.onnx
and convert on x86 with apps/crayfish-fight/tools/convert_rknn.sh.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

# Leaf-conv output tensor of a YOLO(v8/v11) head tower: .../cvN.i.2/Conv_output_0
# N = 2 (box distribution, 4*reg_max ch) | 3 (class scores, nc ch), i = stride
# tower index 0..2. Copied verbatim from models/convert/convert.py::_LEAF_RE so
# the two stay in lockstep.
_LEAF_RE = re.compile(r"/cv([234])\.[012]\.2/Conv_output_0$")

# (name, relative weights path, kind, imgsz) -- the four artefacts P5 needs.
DEFAULT_MODELS = [
    ("det_v1_n", "det_v1_n/best.pt", "detect", 640),
    ("det_v1_s", "det_v1_s/best.pt", "detect", 640),
    ("sex_cls", "sex_cls/best.pt", "classify", 128),
    ("behavior_cls", "behavior_cls/best.pt", "classify", 224),
]


def find_head_branches(model):
    """Group a YOLO head's leaf-Conv outputs by tower. Returns (box, cls)."""
    groups = {2: [], 3: [], 4: []}
    for n in model.graph.node:
        if n.op_type != "Conv":
            continue
        m = _LEAF_RE.search(n.output[0])
        if m:
            groups[int(m.group(1))].append(n.output[0])
    return sorted(groups[2]), sorted(groups[3])


def export_detect(weights: str, imgsz: int, out_onnx: Path, opset: int) -> dict:
    """Export a detector and cut it at the raw head. Returns a summary dict."""
    import onnx
    from ultralytics import YOLO

    m = YOLO(weights)
    names = dict(m.names)
    full = m.export(format="onnx", opset=opset, imgsz=imgsz, simplify=True,
                    dynamic=False, batch=1)
    model = onnx.load(full)
    box, cls = find_head_branches(model)
    if len(box) != 3 or len(cls) != 3:
        raise SystemExit(f"[export] {weights}: expected 3 box + 3 cls head "
                         f"branches, got {len(box)}/{len(cls)}")
    inputs = [i.name for i in model.graph.input]
    outputs = box + cls
    out_onnx.parent.mkdir(parents=True, exist_ok=True)
    onnx.utils.extract_model(str(full), str(out_onnx), inputs, outputs)

    ex = onnx.load(str(out_onnx))
    shapes = {o.name: [d.dim_value for d in o.type.tensor_type.shape.dim]
              for o in ex.graph.output}
    return {"weights": weights, "kind": "detect", "imgsz": imgsz,
            "names": names, "inputs": inputs, "outputs": shapes,
            "onnx": str(out_onnx)}


def export_classify(weights: str, imgsz: int, out_onnx: Path,
                    opset: int) -> dict:
    """Export a yolo11-cls classifier whole (no surgery)."""
    import onnx
    from ultralytics import YOLO

    m = YOLO(weights)
    names = dict(m.names)
    full = m.export(format="onnx", opset=opset, imgsz=imgsz, simplify=True,
                    dynamic=False, batch=1)
    out_onnx.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(full, out_onnx)

    ex = onnx.load(str(out_onnx))
    shapes = {o.name: [d.dim_value for d in o.type.tensor_type.shape.dim]
              for o in ex.graph.output}
    inputs = [i.name for i in ex.graph.input]
    return {"weights": weights, "kind": "classify", "imgsz": imgsz,
            "names": names, "inputs": inputs, "outputs": shapes,
            "onnx": str(out_onnx)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default="/Users/harvest/project/crayfish-fight/runs",
                    help="root holding det_v1_n/ det_v1_s/ sex_cls/ behavior_cls/")
    ap.add_argument("--out-dir", default="/Users/harvest/project/crayfish-fight/export")
    ap.add_argument("--opset", type=int, default=12)
    # single-model mode
    ap.add_argument("--weights", help="export just this .pt")
    ap.add_argument("--kind", choices=["detect", "classify"])
    ap.add_argument("--imgsz", type=int)
    ap.add_argument("--out", help="output .onnx path (single-model mode)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs = []
    if args.weights:
        if not (args.kind and args.imgsz and args.out):
            raise SystemExit("--weights needs --kind, --imgsz and --out")
        jobs.append((Path(args.out).stem, args.weights, args.kind,
                     args.imgsz, Path(args.out)))
    else:
        for name, rel, kind, imgsz in DEFAULT_MODELS:
            jobs.append((name, str(Path(args.runs_dir) / rel), kind, imgsz,
                         out_dir / f"{name}.onnx"))

    summary = []
    for name, weights, kind, imgsz, out_path in jobs:
        print(f"\n=== [{name}] {kind} {imgsz}px  {weights}", flush=True)
        fn = export_detect if kind == "detect" else export_classify
        info = fn(weights, imgsz, out_path, args.opset)
        info["id"] = name
        summary.append(info)
        print(f"[export] {name}: classes={info['names']}")
        print(f"[export] {name}: inputs={info['inputs']}")
        for o, shp in info["outputs"].items():
            print(f"[export] {name}:   out {o}: {shp}")
        print(f"[export] {name}: wrote {out_path}")

    manifest = out_dir / "export_summary.json"
    manifest.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\n[export] summary -> {manifest}")
    print("[export] class order is the ground truth for manifest.json / "
          "app.py constants -- do not reorder by hand.")


if __name__ == "__main__":
    main()
