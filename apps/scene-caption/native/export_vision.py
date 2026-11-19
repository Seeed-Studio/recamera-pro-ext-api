"""Export InternVL3's vision encoder and image-to-language projection."""
import argparse
import torch
from transformers import AutoModel

parser = argparse.ArgumentParser()
parser.add_argument("snapshot")
parser.add_argument("output")
args = parser.parse_args()
model = AutoModel.from_pretrained(
    args.snapshot, trust_remote_code=True, local_files_only=True,
    torch_dtype=torch.float32, use_flash_attn=False,
).eval()
model.forward = model.extract_feature
sample = torch.rand(1, 3, 448, 448)
with torch.no_grad():
    assert model(sample).shape == (1, 256, 896)
    torch.onnx.export(model, sample, args.output, input_names=["pixel_values"],
                      output_names=["visual_features"], opset_version=15)
