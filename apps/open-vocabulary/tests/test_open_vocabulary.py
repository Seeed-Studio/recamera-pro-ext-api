import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from kit.run import load_app_module
from kit.runtime.preprocess import LetterboxInfo

APP_DIR = Path(__file__).resolve().parents[1]
OpenVocabularyApp = load_app_module(str(APP_DIR / "app.py")).OpenVocabularyApp

from clip_tokenizer import ClipTokenizer, _pieces
from object_color import box_color, split_color_query
from vocabulary import Vocabulary, Term, parse_vocabulary
from world_postprocess import postprocess


@pytest.fixture
def tokenizer():
    return ClipTokenizer(APP_DIR / "tokenizer")


class Encoder:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def infer(self, tokens):
        self.calls.append(tokens.copy())
        if len(self.calls) == self.fail_at:
            raise RuntimeError("inference service unavailable")
        return [np.full((1, 512), len(self.calls), dtype=np.float32)]


def test_clip_known_tokens_padding_and_unicode(tokenizer):
    # Known CLIP vocab IDs also guard accidental use of a different tokenizer.
    encoded = tokenizer.encode("  A photo of a CAT  ")[0].astype(int).tolist()
    assert encoded[:7] == [49406, 320, 1125, 539, 320, 2368, 49407]
    assert encoded[7:] == [49407] * 13
    assert tokenizer.encode("杯子").shape == (1, 20)
    assert list(_pieces("cat's 123…猫!")) == ["cat", "'s", "1", "2", "3", "…", "猫", "!"]
    assert tokenizer.encode("cup &amp;amp; bottle").tolist() == tokenizer.encode("cup & bottle").tolist()


def test_long_prompt_fails_without_silent_truncation(tokenizer):
    with pytest.raises(ValueError, match="18"):
        tokenizer.encode("cat " * 19)


def test_parse_aliases_duplicates_and_clear():
    assert parse_vocabulary("杯子=cup, Person；cup\n手机=cell phone") == (
        Term("杯子", "cup"), Term("Person", "person"), Term("手机", "cell phone"))
    assert parse_vocabulary(" ,， \n") == ()


@pytest.mark.parametrize("text", ["=cup", "杯子=", "a=b=c", "x" * 65,
                                     "cat\x00", ",".join(f"object {i}" for i in range(81))])
def test_invalid_vocabulary_is_rejected(text):
    with pytest.raises(ValueError):
        parse_vocabulary(text)


def test_vocabulary_cache_reorder_alias_and_rollback(tokenizer):
    encoder = Encoder()
    vocabulary = Vocabulary(tokenizer)
    vocabulary.update("cup, person", encoder)
    assert vocabulary.features.shape == (1, 80, 512)
    assert len(encoder.calls) == 3
    np.testing.assert_array_equal(encoder.calls[-1], tokenizer.encode(" "))
    vocabulary.update("人=person, 杯子=cup", encoder)
    assert len(encoder.calls) == 3
    assert vocabulary.features[0, 0, 0] == 2
    assert vocabulary.features[0, 1, 0] == 1
    assert vocabulary.features[0, 79, 0] == 3
    old_features = vocabulary.features
    old_terms, old_revision = vocabulary.terms, vocabulary.revision
    with pytest.raises(RuntimeError):
        vocabulary.update("laptop, bottle", Encoder(fail_at=2))
    assert vocabulary.features is old_features
    assert vocabulary.terms == old_terms and vocabulary.revision == old_revision
    vocabulary.update("", encoder)
    assert vocabulary.features is None and vocabulary.terms == ()


def test_incremental_updates_encode_one_prompt_per_step(tokenizer):
    vocabulary = Vocabulary(tokenizer)
    encoder = Encoder()
    vocabulary.update("cup", encoder)
    previous = vocabulary.features
    pending = vocabulary.begin("phone, backpack, bus")
    assert pending.step(encoder) is False
    assert len(encoder.calls) == 3 and vocabulary.features is previous
    assert pending.step(encoder) is False
    assert len(encoder.calls) == 4 and vocabulary.features is previous
    assert pending.step(encoder) is True
    assert len(encoder.calls) == 5
    assert [t.prompt for t in vocabulary.terms] == ["phone", "backpack", "bus"]
    assert vocabulary.revision == 2


def test_color_requests_share_a_slot_and_reuse_embedding_on_reload(tokenizer):
    encoder = Encoder()
    vocabulary = Vocabulary(tokenizer)
    vocabulary.update("红帽=red helmet, blue cup, white helmet, person", encoder)
    assert [term.prompt for term in vocabulary.model_terms] == ["helmet", "cup", "person"]
    assert len(encoder.calls) == 4  # Three nouns and the blank background.
    vocabulary.update("yellow helmet, blue cup, person", encoder)
    assert len(encoder.calls) == 4
    assert vocabulary.requests_by_slot[0] == ((0, "yellow"),)
    assert split_color_query("grey car") == ("car", "gray")
    assert split_color_query("orange") == ("orange", None)


def test_color_routing_on_full_loop_keeps_requested_labels_and_plain_terms(monkeypatch, tokenizer):
    app = OpenVocabularyApp()
    encoder = Encoder()
    app.models = SimpleNamespace(text_encoder=encoder)
    app.vocabulary = "红帽=red helmet, white helmet, blue cup, person"
    app._vocab = Vocabulary(tokenizer)
    app._vocab.update(app.vocabulary, encoder)
    app._requested, app._pending, app._error, app._next_infer = app.vocabulary, None, "", 0
    app.conf, app.iou = .25, .45
    image = np.full((640, 640, 3), [245, 245, 245], np.uint8)
    image[150:178, 150:178] = [235, 25, 22]
    image[150:178, 310:338] = [24, 40, 210]
    frame = SimpleNamespace(pts=1.5, data=image, w=640, h=640)
    outputs = heads()
    for slot, x in ((0, 20), (0, 30), (1, 40), (2, 50)):
        outputs[0][0, slot, 20, x] = .9
        outputs[1][0, :, 20, x] = 1.5
    info = LetterboxInfo(1, 0, 0, 640, 640)
    payloads = []
    monkeypatch.setattr(app, "frames", lambda: iter((frame,)))
    monkeypatch.setattr(app, "_infer", lambda frame: (outputs, info))
    monkeypatch.setattr(app, "emit", lambda events, ts, **kw: payloads.append((events, kw)))
    app.run()
    events, payload = payloads[0]
    results = {row["cls_name"]: row for row in payload["results"]}
    assert set(results) == {"红帽", "white helmet", "blue cup", "person"}
    assert results["红帽"]["cls"] == 0 and results["红帽"]["prompt"] == "red helmet"
    assert results["white helmet"]["object_color"] == "white"
    assert results["blue cup"]["object_color"] == "blue"
    assert "object_color" not in results["person"]
    assert all(event["score"] == pytest.approx(.9) for event in events)


def test_uncertain_crop_cannot_pass_a_color_filter(tokenizer):
    app = OpenVocabularyApp()
    app._vocab = Vocabulary(tokenizer)
    app._vocab.update("red car", Encoder())
    car = {"cls": 0, "box": [0, 0, 48, 48], "score": .8}
    frame = SimpleNamespace(data=np.full((48, 48, 3), 114, np.uint8))
    assert app._match_requests(frame, [car]) == []
    assert box_color(frame, [0, 0, 4, 5], app.crop_roi_hw) is None
    app._vocab.update("red car, car", Encoder())
    results = app._match_requests(frame, [car])
    assert [row["cls_name"] for row in results] == ["car"]


def heads():
    result = []
    for size in (80, 40, 20):
        result += [np.zeros((1, 80, size, size), np.float32),
                   np.ones((1, 4, size, size), np.float32)]
    return result


def test_world_heads_active_slots_nms_geometry_and_labels():
    outputs = heads()
    # Model cell centre (164,244), distance 2 cells -> (148,228,180,260).
    outputs[0][0, 0, 30, 20] = 0.9
    outputs[0][0, 79, 30, 20] = 0.99  # Padding must not steal the argmax.
    outputs[1][0, :, 30, 20] = 2
    outputs[0][0, 0, 30, 21] = 0.8  # Overlapping, same class -> suppress.
    outputs[1][0, :, 30, 21] = 2
    outputs[0][0, 1, 30, 20] = 0.85
    info = LetterboxInfo(0.5, 0, 140, 1280, 720)
    results = postprocess(outputs, info, (Term("杯子", "cup"),), iou=0.4)
    assert len(results) == 1
    assert results[0]["box"] == [296.0, 176.0, 360.0, 240.0]
    assert results[0]["cls_name"] == "杯子"
    assert "90.0%" in results[0]["display_label"]
    assert "296, 176, 360, 240" in results[0]["display_label"]


def test_world_empty_invalid_shapes_and_max_results():
    info = LetterboxInfo(1, 0, 0, 640, 640)
    outputs = heads()
    assert postprocess(outputs, info, (Term("cup", "cup"),)) == []
    assert postprocess(outputs, info, ()) == []
    with pytest.raises(ValueError):
        postprocess(outputs[:2], info, (Term("cup", "cup"),))
    outputs[0][0, 0, 0, 0] = 0.9
    outputs[0][0, 0, 79, 79] = 0.8
    results = postprocess(outputs, info, (Term("cup", "cup"),), max_detections=1)
    assert len(results) == 1 and results[0]["box"][:2] == [0, 0]


def test_live_reload_runs_with_new_features_and_clears(monkeypatch, tokenizer):
    app = OpenVocabularyApp()
    app._vocab = Vocabulary(tokenizer)
    app._requested = None
    app._pending = None
    app._error = ""
    app._next_infer = 0
    app.models = SimpleNamespace(text_encoder=Encoder())
    app.vocabulary = "cup"
    # Managed startup encodes the initial vocabulary and its background slot
    # before the first frame. Exercise the frame loop from that same state.
    app._vocab.update(app.vocabulary, app.models.text_encoder)
    app.conf, app.iou = 0.25, 0.45
    info = LetterboxInfo(1, 0, 0, 640, 640)
    seen = []
    def frames():
        yield SimpleNamespace(pts=123.5)
        app.vocabulary = "phone"
        yield SimpleNamespace(pts=124.5)
        app.vocabulary = ""
        yield SimpleNamespace(pts=125.5)
    def infer(frame):
        seen.append(app._vocab.terms[0].prompt)
        outputs = heads()
        outputs[0][0, 0, 20, 20] = 0.9
        return outputs, info
    payloads = []
    monkeypatch.setattr(app, "frames", frames)
    monkeypatch.setattr(app, "_infer", infer)
    monkeypatch.setattr(app, "emit", lambda events, ts, **kw: payloads.append((ts, kw)))
    app.run()
    assert seen == ["cup", "phone"]
    assert [p[1]["extra"]["vocabulary"] for p in payloads] == [["cup"], ["phone"], []]
    assert payloads[-1][1]["results"] == []
    assert [p[0] for p in payloads] == [123.5, 124.5, 125.5]


def test_real_manifest_declares_dynamic_input_and_pixel_overlay():
    manifest = json.loads((APP_DIR / "manifest.json").read_text())
    items = {x["key"]: x for g in manifest["config_schema"]["groups"] for x in g["items"]}
    assert items["vocabulary"]["apply"] == "live"
    assert manifest["instances"]["endpoint_mode"] == "allocated"
    assert {c["mode"] for c in manifest["resources"]["claims"]
            if c["name"] == "result.publish"} == {"brokered"}
    fields = {f["name"]: f for f in manifest["output"]["fields"]}
    assert fields["box"]["coord"] == "pixel_xyxy"
    assert manifest["render"]["boxes"]["label"] == "cls_name"


def test_detection_frame_and_event_have_known_coordinates_in_result_hub():
    from kit import events
    from market.appmgr.result_hub import normalize_app_payload, _compile_geometry_contract

    manifest = json.loads((APP_DIR / "manifest.json").read_text())
    outputs = heads()
    outputs[0][0, 0, 30, 20] = 0.9
    dets = postprocess(outputs, LetterboxInfo(0.5, 0, 140, 1280, 720),
                       (Term("杯子", "cup"),))
    payload = {"results": dets, "events": [events.detection(d) for d in dets],
               "frame": {"width": 1280, "height": 720}, "pts": 123.5}
    envelopes = normalize_app_payload(
        payload, {"app_id": manifest["id"], "instance_id": "demo-test", "generation": 1},
        trusted_geometry=_compile_geometry_contract(manifest),
        trusted_render=manifest["render"],
        trusted_stream={"id": "main", "kind": "frame.sock", "path": "/live/0"})
    frame = next(e for e in envelopes if e["type"] == "frame")
    event = next(e for e in envelopes if e["type"] == "event")
    assert frame["results"]
    assert frame["stream"] == {"id": "main", "width": 1280, "height": 720,
                               "coordinate_space": "pixel_xyxy"}
    assert event["stream"]["coordinate_space"] == "pixel_xyxy"
    assert frame["results"][0]["spaces"]["box"] == "pixel_xyxy"
    assert event["events"][0]["spaces"]["box"] == "pixel_xyxy"
    label_field = frame["render"]["boxes"]["label"]
    assert frame["results"][0][label_field] == "杯子"
    assert frame["results"][0]["score"] == pytest.approx(0.9)
    assert event["events"][0]["score"] == pytest.approx(0.9)
