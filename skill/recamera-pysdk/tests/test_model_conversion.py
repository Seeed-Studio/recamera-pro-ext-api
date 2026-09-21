import copy
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import tarfile

import numpy as np
import pytest

import convert_model as converter
from package_app import build


@pytest.fixture
def recipe():
    return {"input_name": "images", "input_layout": "NCHW", "input_shape": [1, 3, 8, 8],
            "color": "RGB", "mean": [0, 0, 0], "std": [255, 255, 255],
            "resize": {"mode": "letterbox", "pad_value": 114}}


@pytest.fixture
def graph():
    return {"inputs": [{"name": "images", "shape": [1, 3, 8, 8], "dtype": "FLOAT"}],
            "outputs": [{"name": "out", "shape": [1, 3, 8, 8], "dtype": "FLOAT"}]}


@pytest.fixture
def mocked_conversion(tmp_path, monkeypatch, recipe, graph):
    source = tmp_path / "model.onnx"
    source.write_bytes(b"fixture onnx")
    config = tmp_path / "recipe.json"
    config.write_text(json.dumps(recipe))
    calls = []
    errors = {}

    class RKNN:
        def __init__(self, **kwargs):
            calls.append(("create", kwargs))

        def config(self, **kwargs):
            calls.append(("config", kwargs))
            return errors.get("config", 0)

        def load_onnx(self, **kwargs):
            calls.append(("load_onnx", kwargs))
            return errors.get("load_onnx", 0)

        def build(self, **kwargs):
            calls.append(("build", kwargs))
            return errors.get("build", 0)

        def export_rknn(self, path):
            calls.append(("export_rknn", path))
            if not errors.get("no_output"):
                Path(path).write_bytes(b"mock compiled model")
            return errors.get("export_rknn", 0)

        def release(self):
            calls.append(("release", None))

    module = ModuleType("rknn.api")
    module.RKNN = RKNN
    monkeypatch.setitem(sys.modules, "rknn", ModuleType("rknn"))
    monkeypatch.setitem(sys.modules, "rknn.api", module)
    monkeypatch.setattr(converter, "environment", lambda: {"errors": []})
    monkeypatch.setattr(converter, "inspect_onnx", lambda _: copy.deepcopy(graph))
    args = SimpleNamespace(onnx=source, recipe=config, out=tmp_path / "converted", quant="fp",
                           dataset=None, sample=[], atol=None, rtol=None, model_id="det", task="detect", verbose=False)
    return args, calls, errors


def test_conversion_and_package(mocked_conversion, manifest, make_app, tmp_path):
    args, calls, _ = mocked_conversion
    previous = Path.cwd()
    report = converter.convert(args)
    assert Path.cwd() == previous
    assert [x[0] for x in calls] == ["create", "config", "load_onnx", "build", "export_rknn", "release"]
    assert calls[1][1]["target_platform"] == "rv1126b"
    assert calls[3][1] == {"do_quantization": False, "dataset": None}
    assert report["conversion"] == "passed"
    assert report["app_integration"]["standard_kit_preprocessing"] is True
    assert report["numerical_validation"]["status"] == report["device_validation"]["status"] == "not_run"
    fragment = json.loads((args.out / "manifest.fragment.json").read_text())
    assert fragment["models"][0]["input"] == [1, 8, 8, 3]  # Kit metadata is NHWC, not ONNX NCHW!
    manifest.update(fragment)
    manifest["resources"]["claims"].append({"name": "npu.rknn", "mode": "scheduled", "required": True})
    manifest["permissions"]["sdk"].append("npu.infer")
    app = make_app(files={"models/det.rknn": "mock compiled model"})
    archive, _ = build(app, tmp_path / "dist")
    with tarfile.open(archive) as tf:
        assert tf.extractfile("models/det.rknn").read() == (args.out / "model.rknn").read_bytes()
        assert "conversion-report.json" not in tf.getnames()


@pytest.mark.parametrize("stage", ["config", "load_onnx", "build", "export_rknn", "no_output"])
def test_failed_conversion_cleans_up_and_cannot_report_success(mocked_conversion, stage):
    args, calls, errors = mocked_conversion
    errors[stage] = -1
    previous = Path.cwd()
    with pytest.raises(converter.ConversionError):
        converter.convert(args)
    assert calls[-1][0] == "release"
    assert Path.cwd() == previous
    assert not (args.out / "model.rknn").exists()
    assert not (args.out / "manifest.fragment.json").exists()
    assert json.loads((args.out / "conversion-report.json").read_text())["conversion"] == "failed"


def test_existing_output_is_never_reused(mocked_conversion):
    args, calls, _ = mocked_conversion
    args.out.mkdir()
    stale = args.out / "model.rknn"
    stale.write_bytes(b"previous model")
    with pytest.raises(FileExistsError):
        converter.convert(args)
    assert not calls
    assert stale.read_bytes() == b"previous model"


@pytest.mark.parametrize("shape", [[1, 3, "height", "width"], [2, 3, 8, 8], [1, 1, 8, 8]])
def test_reject_unsupported_graph_shapes(recipe, graph, shape):
    recipe["input_shape"] = graph["inputs"][0]["shape"] = shape
    with pytest.raises(converter.ConversionError):
        converter.validate_recipe(recipe, graph)


def test_reject_mismatched_recipe_and_multiple_inputs(recipe, graph):
    recipe["input_name"] = "wrong"
    with pytest.raises(converter.ConversionError, match="exactly match"):
        converter.validate_recipe(recipe, graph)
    recipe["input_name"] = "images"
    graph["inputs"].append(graph["inputs"][0])
    with pytest.raises(converter.ConversionError, match="one float32"):
        converter.validate_recipe(recipe, graph)


def test_nhwc_graph_needs_explicit_model_specific_adaptation(recipe, graph):
    recipe["input_layout"] = "NHWC"
    recipe["input_shape"] = graph["inputs"][0]["shape"] = [1, 8, 8, 3]
    with pytest.raises(converter.ConversionError, match="assumes NCHW"):
        converter.validate_recipe(recipe, graph)


@pytest.mark.parametrize("change", [{"std": [0, 1, 1]}, {"mean": [float("nan"), 0, 0]},
                                    {"color": "auto"}, {"resize": {"mode": "crop", "pad_value": 114}}])
def test_invalid_preprocessing_is_rejected(recipe, graph, change):
    recipe.update(change)
    with pytest.raises(converter.ConversionError):
        converter.validate_recipe(recipe, graph)


def test_i8_requires_dataset_and_sample_requires_explicit_tolerances(mocked_conversion):
    args, calls, _ = mocked_conversion
    args.quant = "i8"
    with pytest.raises(converter.ConversionError, match="requires --dataset"):
        converter.convert(args)
    args.quant = "fp"
    args.sample = [Path("sample.png")]
    with pytest.raises(converter.ConversionError, match="--atol and --rtol"):
        converter.convert(args)
    assert not calls


def test_numerical_failure_remains_distinct_from_compilation(mocked_conversion, monkeypatch):
    args, _, _ = mocked_conversion
    args.sample, args.atol, args.rtol = [args.onnx], .01, .01
    monkeypatch.setattr(converter, "verify_samples", lambda *a: {"status": "failed"})
    report = converter.convert(args)
    assert report["conversion"] == "passed"
    assert report["numerical_validation"]["status"] == "failed"
    assert report["device_validation"]["status"] == "not_run"
    assert not (args.out / "manifest.fragment.json").exists()


def test_simulator_error_records_failure(mocked_conversion, monkeypatch):
    args, calls, _ = mocked_conversion
    args.sample, args.atol, args.rtol = [args.onnx], .01, .01
    def fail(*args):
        raise RuntimeError("simulator unavailable")
    monkeypatch.setattr(converter, "verify_samples", fail)
    with pytest.raises(RuntimeError, match="simulator unavailable"):
        converter.convert(args)
    report = json.loads((args.out / "conversion-report.json").read_text())
    assert report["conversion"] == "passed"
    assert report["numerical_validation"]["status"] == "error"
    assert not (args.out / "manifest.fragment.json").exists()
    assert calls[-1][0] == "release"


def test_output_comparison_rejects_hidden_layout_and_nan_errors():
    reference = np.arange(12, dtype=np.float32).reshape(1, 3, 2, 2)
    good = converter.compare_outputs([reference], [reference.copy()], ["out"], 0, 0)
    assert good[0]["passed"]
    assert not converter.compare_outputs([reference], [reference + 1], ["out"], .01, .01)[0]["passed"]
    for actual in (reference.transpose(0, 2, 3, 1), reference.astype(np.int8), reference * np.nan):
        with pytest.raises(converter.ConversionError):
            converter.compare_outputs([reference], [actual], ["out"], .01, .01)


def test_calibration_letterbox_and_relative_paths(tmp_path, recipe):
    Image = pytest.importorskip("PIL.Image")
    source = tmp_path / "red image.png"
    Image.new("RGB", (8, 4), (255, 0, 0)).save(source)
    listing = tmp_path / "calibration.txt"
    listing.write_text("red image.png\n")
    work = tmp_path / "work"
    work.mkdir()
    dataset, info = converter.prepare_dataset(listing, recipe, work)
    pixels = np.asarray(Image.open(dataset.read_text().strip()))
    assert pixels.shape == (8, 8, 3)
    assert pixels[0, 0].tolist() == [114, 114, 114]
    assert pixels[3, 3].tolist() == [255, 0, 0]
    assert info["count"] == 1 and info["images"][0]["source_sha256"] == converter.digest(source)
    recipe["color"] = "BGR"
    # Saved PNGs remain RGB; config quant_img_RGB2BGR does the calibration swap.
    assert converter.prepare_image(source, recipe)[3, 3].tolist() == [255, 0, 0]


@pytest.mark.parametrize("color", ["RGB", "BGR"])
def test_simulator_receives_raw_pixels_and_onnx_receives_normalized_nchw(tmp_path, recipe, graph, monkeypatch, color):
    Image = pytest.importorskip("PIL.Image")
    recipe.update(color=color, mean=[1, 2, 3], std=[2, 4, 8])
    source = tmp_path / "sample.png"
    Image.new("RGB", (8, 8), (20, 40, 60)).save(source)
    received = {}
    expected = np.ones((1, 3, 8, 8), np.float32)
    class Session:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, names, inputs):
            received["onnx"] = inputs["images"]
            return [expected]

    ort = ModuleType("onnxruntime")
    ort.SessionOptions = SimpleNamespace
    ort.InferenceSession = Session
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)

    class Simulator:
        def init_runtime(self):
            return 0

        def inference(self, *, inputs, data_format):
            assert data_format == ["nhwc"]
            received["rknn"] = inputs[0]
            return [expected.copy()]

    graph["outputs"][0]["name"] = "file"
    report = converter.verify_samples(Simulator(), tmp_path / "model.onnx", graph,
                                       recipe, [source], tmp_path, 0, 0)
    pixel = [20, 40, 60] if color == "RGB" else [60, 40, 20]
    assert received["rknn"].dtype == np.uint8
    assert received["rknn"].shape == (1, 8, 8, 3)
    assert received["rknn"][0, 0, 0].tolist() == pixel
    assert received["onnx"].shape == (1, 3, 8, 8)
    assert received["onnx"][0, :, 0, 0].tolist() == ((np.array(pixel) - [1, 2, 3]) / [2, 4, 8]).tolist()
    assert report["status"] == "passed"


def test_empty_calibration_list_rejected(tmp_path, recipe):
    pytest.importorskip("PIL.Image")
    dataset = tmp_path / "empty.txt"
    dataset.write_text("\n")
    with pytest.raises(converter.ConversionError, match="empty"):
        converter.prepare_dataset(dataset, recipe, tmp_path)


def test_real_onnx_inspection_including_external_weights(tmp_path):
    onnx = pytest.importorskip("onnx")
    h = onnx.helper
    weights = onnx.numpy_helper.from_array(np.ones((1, 3, 1, 1), dtype=np.float32), name="weights")
    graph = h.make_graph([h.make_node("Add", ["images", "weights"], ["out"])], "test",
                         [h.make_tensor_value_info("images", onnx.TensorProto.FLOAT, [1, 3, 8, 8])],
                         [h.make_tensor_value_info("out", onnx.TensorProto.FLOAT, [1, 3, 8, 8])], [weights])
    model = h.make_model(graph, opset_imports=[h.make_opsetid("", 13)])
    path = tmp_path / "test.onnx"
    onnx.save_model(model, str(path), save_as_external_data=True, all_tensors_to_one_file=True,
                    location="weights.bin", size_threshold=0)
    info = converter.inspect_onnx(path)
    assert info["inputs"][0]["shape"] == [1, 3, 8, 8]
    assert info["external_data"][0]["sha256"] == converter.digest(tmp_path / "weights.bin")
    stored = onnx.load(str(path), load_external_data=False)
    stored.graph.initializer[0].external_data[0].value = "../escaped.bin"
    onnx.save(stored, str(path))
    with pytest.raises(converter.ConversionError, match="inside the ONNX directory"):
        converter.inspect_onnx(path)
