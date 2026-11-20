"""Compile the 24 InternViT blocks in six FP16 stages for the 2 GiB camera."""
import argparse
from pathlib import Path
import onnx
from rknn.api import RKNN

parser = argparse.ArgumentParser()
parser.add_argument("onnx", type=Path)
parser.add_argument("out", type=Path)
args = parser.parse_args()
args.out.mkdir(exist_ok=True, parents=True)
model = onnx.load(str(args.onnx))
# Small stages keep camera, decoder and vision inference within the firmware's
# memory headroom. The cut tensor after each fourth residual block is [1,1025,1024].
bounds = [model.graph.input[0].name]
for block in (3, 7, 11, 15, 19):
    ends = [node.output[0] for node in model.graph.node
            if node.name.endswith(f"layers.{block}/Add_1")]
    if len(ends) != 1:
        raise ValueError(f"Missing residual output of InternViT block {block}")
    bounds.append(ends[0])
    model.graph.value_info.append(onnx.helper.make_tensor_value_info(
        ends[0], onnx.TensorProto.FLOAT, [1, 1025, 1024]))
bounds.append(model.graph.output[0].name)
extractor = onnx.utils.Extractor(model)
for stage, (start, end) in enumerate(zip(bounds, bounds[1:])):
    segment = extractor.extract_model([start], [end])
    if len(segment.graph.input) != 1:
        raise ValueError("Each stage must have a single independent input")
    path = args.out / f"vision-{stage}.onnx"
    onnx.save(segment, str(path))
    compiler = RKNN(verbose=False)
    try:
        norm = dict(mean_values=[[123.675,116.28,103.53]],
                    std_values=[[58.395,57.12,57.375]]) if stage == 0 else {}
        assert compiler.config(target_platform="rv1126b", **norm) == 0
        assert compiler.load_onnx(str(path)) == 0
        assert compiler.build(do_quantization=False) == 0
        assert compiler.export_rknn(str(path.with_suffix(".rknn"))) == 0
    finally:
        compiler.release()
