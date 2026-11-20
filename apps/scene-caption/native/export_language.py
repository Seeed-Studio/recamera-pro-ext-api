"""Build the InternVL3-1B language decoder with RKLLM Toolkit 1.2.3."""
import argparse
from rkllm.api import RKLLM

parser = argparse.ArgumentParser()
parser.add_argument("snapshot")
parser.add_argument("output")
args = parser.parse_args()
model = RKLLM()
if model.load_huggingface(model=args.snapshot, device="cpu") != 0:
    raise RuntimeError("Could not load InternVL3-1B language weights")
if model.build(do_quantization=True, optimization_level=1,
               quantized_dtype="w4a16", quantized_algorithm="normal",
               target_platform="rv1126b", num_npu_core=1) != 0:
    raise RuntimeError("RKLLM conversion failed")
if model.export_rkllm(args.output) != 0:
    raise RuntimeError("RKLLM export failed")
