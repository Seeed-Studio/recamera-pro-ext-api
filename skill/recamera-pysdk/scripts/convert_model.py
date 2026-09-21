#!/usr/bin/env python3
"""Host-only ONNX -> RV1126B RKNN conversion for static, single-image models.

Optional dependencies belong in a separate Toolkit2 environment, never an App
wheelhouse. See references/model-conversion.md for model-specific recipes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import tempfile


TOOLKIT_VERSION = "2.3.2"
TARGET = "rv1126b"
ZOO_REVISION = "bad6c7334531becaf90a561988519b7bec34d0ab"
ZOO_URL = f"https://github.com/airockchip/rknn_model_zoo/tree/{ZOO_REVISION}"


class ConversionError(ValueError):
    pass


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def environment():
    versions = {}
    for name in ("rknn-toolkit2", "onnx", "onnxruntime", "numpy", "Pillow", "torch", "setuptools"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    errors = []
    # This helper's tested host baseline, not a claim about all Toolkit platforms.
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        errors.append("This helper's baseline is Linux x86_64; use an isolated Linux host/container.")
    if sys.version_info[:2] != (3, 11):
        errors.append("Use Python 3.11 for the documented Toolkit2 environment.")
    if (versions["rknn-toolkit2"] or "").split("+")[0] != TOOLKIT_VERSION:
        errors.append(f"Use rknn-toolkit2 {TOOLKIT_VERSION} for the current rknn232 platform baseline.")
    for name, version in versions.items():
        if version is None:
            errors.append(f"Missing host dependency: {name}")
    if importlib.util.find_spec("pkg_resources") is None:
        errors.append("Toolkit2 2.3.2 needs pkg_resources; use the documented setuptools pin.")
    return {"python": platform.python_version(), "system": platform.system(),
            "machine": platform.machine(), "packages": versions, "errors": errors}


def inspect_onnx(path):
    try:
        import onnx
    except ImportError as exc:
        raise ConversionError("ONNX inspection needs 'onnx' in the host conversion environment.") from exc
    path = Path(path).resolve(strict=True)
    model = onnx.load(str(path), load_external_data=False)
    external = {}

    def visit(message):
        if isinstance(message, onnx.TensorProto) and message.data_location == onnx.TensorProto.EXTERNAL:
            metadata = {item.key: item.value for item in message.external_data}
            location = metadata.get("location", "")
            relative = Path(location)
            if not location or relative.is_absolute() or ".." in relative.parts:
                raise ConversionError("External ONNX weights must stay inside the ONNX directory.")
            resolved = (path.parent / relative).resolve(strict=True)
            if not resolved.is_relative_to(path.parent) or not resolved.is_file():
                raise ConversionError("External ONNX weights escape the ONNX directory.")
            external[location] = {"file": location, "sha256": digest(resolved), "size": resolved.stat().st_size}
        for field, value in message.ListFields():
            if field.message_type is not None:
                for child in value if field.label == field.LABEL_REPEATED else [value]:
                    visit(child)

    visit(model)
    # Check by filename so valid large external-data models do not hit protobuf's 2 GB limit.
    onnx.checker.check_model(str(path))

    def tensor(value):
        if not value.type.HasField("tensor_type"):
            raise ConversionError("Only tensor inputs/outputs are supported by this helper.")
        t = value.type.tensor_type
        shape = [int(d.dim_value) if d.HasField("dim_value") else (d.dim_param or None)
                 for d in t.shape.dim]
        return {"name": value.name, "shape": shape, "dtype": onnx.TensorProto.DataType.Name(t.elem_type)}

    initializers = {value.name for value in model.graph.initializer}
    return {"file": path.name, "sha256": digest(path), "size": path.stat().st_size,
            "external_data": sorted(external.values(), key=lambda x: x["file"]),
            "ir_version": model.ir_version,
            "opsets": [{"domain": x.domain, "version": x.version} for x in model.opset_import],
            "inputs": [tensor(x) for x in model.graph.input if x.name not in initializers],
            "outputs": [tensor(x) for x in model.graph.output],
            "operators": sorted({f"{x.domain or 'ai.onnx'}::{x.op_type}" for x in model.graph.node}),
            "operator_support": "unverified_until_toolkit_build"}


def validate_recipe(recipe, model):
    fields = {"input_name", "input_layout", "input_shape", "color", "mean", "std", "resize"}
    if not isinstance(recipe, dict) or set(recipe) != fields:
        raise ConversionError(f"Recipe must contain exactly: {', '.join(sorted(fields))}")
    inputs = model["inputs"]
    if len(inputs) != 1 or inputs[0]["dtype"] != "FLOAT":
        raise ConversionError("This helper requires one float32 image input; use a model-specific recipe otherwise.")
    actual = inputs[0]
    shape = actual["shape"]
    if (len(shape) != 4 or any(type(d) is not int or d <= 0 for d in shape) or shape[0] != 1):
        raise ConversionError("Export a static batch-1 image model first; dynamic dimensions are not guessed.")
    if recipe["input_name"] != actual["name"] or recipe["input_shape"] != shape:
        raise ConversionError("Recipe input name/shape must exactly match the inspected ONNX input.")
    if recipe["input_layout"] != "NCHW":
        raise ConversionError("This Toolkit2 ONNX path assumes NCHW. Re-export/adapt NHWC explicitly with a model-specific recipe.")
    if shape[1] != 3:
        raise ConversionError("This helper requires an NCHW three-channel image model.")
    if recipe["color"] not in ("RGB", "BGR"):
        raise ConversionError("color must explicitly be RGB or BGR.")
    for name in ("mean", "std"):
        values = recipe[name]
        if (not isinstance(values, list) or len(values) != 3 or
                any(type(x) not in (int, float) or not math.isfinite(x) for x in values)):
            raise ConversionError(f"{name} must have three finite numbers in model channel order.")
    if any(x <= 0 for x in recipe["std"]):
        raise ConversionError("std values must be positive.")
    resize = recipe["resize"]
    if (not isinstance(resize, dict) or set(resize) != {"mode", "pad_value"} or
            resize["mode"] not in ("letterbox", "stretch") or
            type(resize["pad_value"]) is not int or not 0 <= resize["pad_value"] <= 255):
        raise ConversionError("resize requires mode=letterbox/stretch and an integer pad_value in [0,255].")
    return recipe


def image_shape(recipe):
    s = recipe["input_shape"]
    return s[2], s[3]


def prepare_image(path, recipe):
    """Return RGB uint8 HWC; channel swapping is explicit at the inference boundary."""
    import numpy as np
    from PIL import Image
    height, width = image_shape(recipe)
    with Image.open(path) as source:
        rgb = source.convert("RGB")
        if recipe["resize"]["mode"] == "stretch":
            return np.asarray(rgb.resize((width, height), Image.Resampling.BILINEAR)).copy()
        scale = min(width / rgb.width, height / rgb.height)
        w, h = round(rgb.width * scale), round(rgb.height * scale)
        if min(w, h) < 1:
            raise ConversionError("Image aspect ratio is too extreme for the requested input size.")
        resized = rgb.resize((w, h), Image.Resampling.BILINEAR)
        fill = recipe["resize"]["pad_value"]
        canvas = Image.new("RGB", (width, height), (fill, fill, fill))
        canvas.paste(resized, (round((width - w) / 2 - .1), round((height - h) / 2 - .1)))
        return np.asarray(canvas).copy()


def prepare_dataset(list_path, recipe, work):
    """Resolve one image per line relative to the list, and calibrate resized PNGs."""
    from PIL import Image
    list_path = Path(list_path).resolve(strict=True)
    paths = [line.strip() for line in list_path.read_text().splitlines() if line.strip()]
    if not paths:
        raise ConversionError("INT8 calibration list is empty.")
    folder = work / "calibration"
    folder.mkdir()
    records = []
    prepared = []
    for index, entry in enumerate(paths):
        path = (list_path.parent / entry).resolve(strict=True)
        output = folder / f"{index:06d}.png"
        Image.fromarray(prepare_image(path, recipe)).save(output)
        records.append({"index": index, "source_sha256": digest(path), "prepared_sha256": digest(output)})
        prepared.append(str(output))
    # Toolkit splits whitespace for multi-input datasets; temporary paths must be unambiguous.
    if any(any(c.isspace() for c in path) for path in prepared):
        raise ConversionError("Use a TMPDIR without whitespace for Toolkit calibration paths.")
    dataset = work / "dataset.txt"
    dataset.write_text("\n".join(prepared) + "\n")
    return dataset, {"count": len(records), "list_sha256": digest(list_path), "images": records}


def compare_outputs(reference, actual, names, atol, rtol):
    import numpy as np
    if len(reference) != len(actual) or len(reference) != len(names):
        raise ConversionError("Output count differs; verify the model export and output ordering.")
    metrics = []
    for name, expected, observed in zip(names, reference, actual):
        expected, observed = np.asarray(expected), np.asarray(observed)
        if expected.shape != observed.shape:
            raise ConversionError(f"Output {name} shape differs: {expected.shape} vs {observed.shape}; no implicit reshape.")
        if expected.dtype.kind != "f" or observed.dtype.kind != "f":
            raise ConversionError(f"Output {name} must be floating point/dequantized before comparison.")
        if not expected.size or not np.isfinite(expected).all() or not np.isfinite(observed).all():
            raise ConversionError(f"Output {name} is empty or contains non-finite values.")
        a, b = expected.astype(np.float64).ravel(), observed.astype(np.float64).ravel()
        delta = np.abs(a - b)
        norm = float(np.linalg.norm(a) * np.linalg.norm(b))
        metrics.append({"name": name, "shape": list(expected.shape),
                        "max_abs_error": float(delta.max()), "mean_abs_error": float(delta.mean()),
                        "cosine_similarity": float(np.dot(a, b) / norm) if norm else None,
                        "passed": bool(np.allclose(b, a, atol=atol, rtol=rtol))})
    return metrics


def check_ret(stage, code):
    if code != 0:
        raise ConversionError(f"RKNN {stage} failed with code {code!r}; inspect the Toolkit log.")


def verify_samples(rknn, model_path, model, recipe, samples, output, atol, rtol):
    import numpy as np
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
    names = [item["name"] for item in model["outputs"]]
    # A loaded .rknn cannot use the simulator: call this on the just-built graph.
    check_ret("init_runtime(simulator)", rknn.init_runtime())
    results = []
    for index, sample in enumerate(samples):
        rgb = prepare_image(sample, recipe)
        pixels = rgb if recipe["color"] == "RGB" else rgb[..., ::-1]
        pixels = np.ascontiguousarray(pixels[None])
        normalized = (pixels.astype(np.float32) - np.array(recipe["mean"], np.float32)) / np.array(recipe["std"], np.float32)
        normalized = normalized.transpose(0, 3, 1, 2)
        expected = session.run(names, {recipe["input_name"]: np.ascontiguousarray(normalized)})
        actual = rknn.inference(inputs=[pixels], data_format=["nhwc"])
        if actual is None:
            raise ConversionError("RKNN simulator returned no outputs.")
        # Ordinal keys also work for ONNX tensor names such as "file", which
        # would collide with numpy.savez's own keyword arguments.
        np.savez(output / f"sample-{index:03d}-onnx.npz", **{f"output_{i}": x for i, x in enumerate(expected)})
        np.savez(output / f"sample-{index:03d}-rknn.npz", **{f"output_{i}": x for i, x in enumerate(actual)})
        metrics = compare_outputs(expected, actual, names, atol, rtol)
        results.append({"sample_sha256": digest(sample), "outputs": metrics,
                        "passed": all(x["passed"] for x in metrics)})
    return {"status": "passed" if all(x["passed"] for x in results) else "failed",
            "backend": "toolkit_simulator", "output_mapping": "ONNX graph output order; no implicit transpose",
            "atol": atol, "rtol": rtol, "samples": results}


def manifest_fragment(model_id, task, recipe, model_path, quant):
    height, width = image_shape(recipe)
    path = f"models/{model_id}.rknn"
    return {"models": [{"id": model_id, "task": task, "file": path,
                        "input": [1, height, width, 3], "quant": "int8" if quant == "i8" else "fp16"}],
            "artifacts": [{"id": model_id, "kind": "rknn", "source": "bundled", "file": path,
                           "mount": path, "sha256": digest(model_path), "size": model_path.stat().st_size,
                           "required": True, "share_scope": "content"}]}


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def convert(args):
    env = environment()
    if env["errors"]:
        raise ConversionError("; ".join(env["errors"]))
    if args.quant == "i8" and args.dataset is None:
        raise ConversionError("INT8 requires --dataset with representative calibration images.")
    if args.quant == "fp" and args.dataset is not None:
        raise ConversionError("--dataset is for --quant i8; it is not used for a non-quantized build.")
    if args.sample and (args.atol is None or args.rtol is None):
        raise ConversionError("--sample requires explicit --atol and --rtol suitable for this model.")
    for tolerance in (args.atol, args.rtol):
        if tolerance is not None and (not math.isfinite(tolerance) or tolerance < 0):
            raise ConversionError("Comparison tolerances must be finite and nonnegative.")
    if not args.sample and (args.atol is not None or args.rtol is not None):
        raise ConversionError("Comparison tolerances require --sample.")
    for token in (args.model_id, args.task):
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", token):
            raise ConversionError("Model id/task must be simple tokens, not paths.")
    model_path = args.onnx.resolve(strict=True)
    recipe_path = args.recipe.resolve(strict=True)
    model = inspect_onnx(model_path)
    recipe = validate_recipe(json.loads(recipe_path.read_text()), model)
    samples = [path.resolve(strict=True) for path in args.sample]
    dataset = args.dataset.resolve(strict=True) if args.dataset else None
    output = args.out.resolve()
    # Never silently reuse a previous model/report after a failed conversion.
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "target_platform": TARGET, "environment": env,
              "official_reference": ZOO_URL, "onnx": model, "recipe": recipe,
              "recipe_sha256": digest(recipe_path), "quantization": args.quant,
              "conversion": "pending", "numerical_validation": {"status": "not_run"},
              "device_validation": {"status": "not_run"}, "calibration": None}
    height, width = image_shape(recipe)
    report["app_integration"] = {
        "manifest_input": [1, height, width, 3],
        "standard_kit_preprocessing": (height == width and recipe["color"] == "RGB"
            and recipe["resize"] == {"mode": "letterbox", "pad_value": 114}),
        "runtime_input": f"raw uint8 {recipe['color']} pixels; mean/std are baked into RKNN",
        "postprocessing_validation": "not_run",
        "note": "Check app preprocessing, output decoding and labels before packaging; manifest metadata does not configure them.",
    }
    try:
        from rknn.api import RKNN
        with tempfile.TemporaryDirectory(prefix="recamera-rknn-") as tmp:
            work = Path(tmp)
            prepared = None
            if dataset:
                prepared, report["calibration"] = prepare_dataset(dataset, recipe, work)
            with working_directory(work):
                rknn = RKNN(verbose=args.verbose)
                try:
                    check_ret("config", rknn.config(target_platform=TARGET, mean_values=[recipe["mean"]],
                              std_values=[recipe["std"]], quant_img_RGB2BGR=recipe["color"] == "BGR"))
                    check_ret("load_onnx", rknn.load_onnx(model=str(model_path)))
                    check_ret("build", rknn.build(do_quantization=args.quant == "i8",
                              dataset=str(prepared) if prepared else None))
                    built = work / "model.rknn"
                    check_ret("export_rknn", rknn.export_rknn(str(built)))
                    if not built.is_file() or built.stat().st_size == 0:
                        raise ConversionError("Toolkit did not produce a nonempty RKNN file.")
                    artifact = output / "model.rknn"
                    shutil.copyfile(built, artifact)
                    report["artifact"] = {"file": artifact.name, "sha256": digest(artifact), "size": artifact.stat().st_size}
                    report["conversion"] = "passed"
                    if samples:
                        report["numerical_validation"] = {"status": "running"}
                        report["numerical_validation"] = verify_samples(
                            rknn, model_path, model, recipe, samples, output, args.atol, args.rtol)
                    if report["numerical_validation"]["status"] != "failed":
                        write_json(output / "manifest.fragment.json", manifest_fragment(
                            args.model_id, args.task, recipe, artifact, args.quant))
                finally:
                    rknn.release()
    except Exception as exc:
        report["error"] = str(exc)
        if report["conversion"] != "passed":
            report["conversion"] = "failed"
        if report["numerical_validation"]["status"] == "running":
            report["numerical_validation"] = {"status": "error", "error": str(exc)}
        raise
    finally:
        write_json(output / "conversion-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-env", help="Check the isolated Linux x86_64 Toolkit2 2.3.2 host baseline")
    inspect = commands.add_parser("inspect", help="Check ONNX and list the graph contract; no Toolkit import")
    inspect.add_argument("onnx", type=Path)
    build = commands.add_parser("convert", help="Convert one static 3-channel image model; optional simulator comparison")
    build.add_argument("--onnx", type=Path, required=True)
    build.add_argument("--recipe", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True, help="New output directory; must not exist")
    build.add_argument("--quant", choices=("fp", "i8"), default="fp")
    build.add_argument("--dataset", type=Path, help="INT8 only: text list, one image path per line, relative to the list")
    build.add_argument("--sample", type=Path, action="append", default=[], help="Held-out image for ONNX/simulator comparison; repeatable")
    build.add_argument("--atol", type=float)
    build.add_argument("--rtol", type=float)
    build.add_argument("--model-id", required=True)
    build.add_argument("--task", required=True, help="Kit model task, e.g. detect, pose, classify; does not select postprocessing")
    build.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "check-env":
            result = environment()
            print(json.dumps(result, indent=2))
            return 1 if result["errors"] else 0
        if args.command == "inspect":
            print(json.dumps(inspect_onnx(args.onnx), indent=2))
            return 0
        result = convert(args)
        print(args.out.resolve() / "conversion-report.json")
        return 2 if result["numerical_validation"]["status"] == "failed" else 0
    except Exception as exc:
        print(f"model conversion failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
