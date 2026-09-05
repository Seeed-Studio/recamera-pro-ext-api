"""Gallery persistence, the model-tag gate, and the cosine scan."""
import json
import os

import numpy as np
import pytest

import gallery as gallery_mod
from gallery import Gallery, GalleryMismatch

TAG = "rv1126b:scrfd500m+mbf512@fp16"
DIM = 512


def basis(i, dim=DIM, noise=0.0, seed=0):
    v = np.zeros(dim, dtype=np.float32)
    v[i] = 1.0
    if noise:
        v = v + np.random.default_rng(seed).normal(0, noise, dim).astype(np.float32)
    return gallery_mod.l2_normalize(v)


@pytest.fixture()
def g(tmp_path):
    return Gallery(str(tmp_path / "g.json"), TAG, dim=DIM, threshold=0.40)


class TestPaths:
    def test_tag_sanitisation_strips_path_hostile_characters(self):
        assert gallery_mod.sanitize_tag(TAG) == "rv1126b_scrfd500m_mbf512_fp16"

    def test_default_path_honours_the_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FACE_GALLERY_DIR", str(tmp_path))
        assert gallery_mod.default_path(TAG) == str(
            tmp_path / "rv1126b_scrfd500m_mbf512_fp16.json")

    def test_default_path_without_the_env_var(self, monkeypatch):
        monkeypatch.delenv("FACE_GALLERY_DIR", raising=False)
        assert gallery_mod.default_path(TAG).startswith(gallery_mod.DEFAULT_DIR)


class TestPersistence:
    def test_missing_file_is_an_empty_gallery_not_an_error(self, g):
        g.load()
        assert len(g) == 0
        assert g.match(basis(0)) == (None, 0.0, [])

    def test_enroll_save_load_roundtrip(self, g, tmp_path):
        g.load()
        g.enroll("alice", [basis(0)])
        g.enroll("bob", [basis(1)])

        again = Gallery(g.path, TAG, dim=DIM, threshold=0.40).load()
        assert [u["name"] for u in again.list()] == ["alice", "bob"]
        name, score, _ = again.match(basis(0))
        assert (name, round(score, 3)) == ("alice", 1.0)

    def test_file_format_v2(self, g):
        g.load()
        g.enroll("alice", [basis(0), basis(0)])
        blob = json.loads(open(g.path, encoding="utf-8").read())
        assert blob["_meta"]["model_tag"] == TAG
        assert blob["_meta"]["dim"] == DIM
        assert blob["_meta"]["threshold"] == pytest.approx(0.40)
        assert blob["_meta"]["updated"]
        assert set(blob["users"]) == {"alice"}
        rec = blob["users"]["alice"]
        assert len(rec["emb"]) == DIM and rec["n"] == 2 and rec["ts"]

    def test_save_is_atomic_and_leaves_no_temp_files(self, g, tmp_path):
        g.load()
        g.enroll("alice", [basis(0)])
        leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".gallery-")]
        assert leftovers == []

    def test_directory_is_created_on_first_save(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "g.json"
        gal = Gallery(str(path), TAG, dim=DIM).load()
        gal.enroll("alice", [basis(0)])
        assert path.exists()


class TestModelTagGate:
    """A gallery from another model matches nobody, silently -- so refuse it."""

    def test_wrong_model_tag_raises(self, g):
        g.load()
        g.enroll("alice", [basis(0)])
        other = Gallery(g.path, "rk3588:scrfd2p5g+r50@int8", dim=DIM)
        with pytest.raises(GalleryMismatch):
            other.load()
        assert len(other) == 0        # and it did NOT populate

    def test_wrong_dim_raises(self, g):
        g.load()
        g.enroll("alice", [basis(0)])
        with pytest.raises(GalleryMismatch):
            Gallery(g.path, TAG, dim=256).load()

    def test_a_wrong_length_embedding_in_the_file_raises(self, g):
        g.load()
        g.enroll("alice", [basis(0)])
        blob = json.loads(open(g.path, encoding="utf-8").read())
        blob["users"]["alice"]["emb"] = [0.1] * 128
        open(g.path, "w", encoding="utf-8").write(json.dumps(blob))
        with pytest.raises(GalleryMismatch):
            Gallery(g.path, TAG, dim=DIM).load()


class TestEnroll:
    def test_multiple_embeddings_are_averaged_and_renormalized(self, g):
        g.load()
        a, b = basis(0), basis(1)
        g.enroll("alice", [a, b])
        tpl = g._users["alice"]["emb"]
        assert float(np.linalg.norm(tpl)) == pytest.approx(1.0, abs=1e-5)
        assert float(tpl @ a) == pytest.approx(float(tpl @ b), abs=1e-5)
        assert g._users["alice"]["n"] == 2

    def test_re_enrolling_replaces_rather_than_appends(self, g):
        g.load()
        g.enroll("alice", [basis(0)])
        g.enroll("alice", [basis(5)])
        assert len(g) == 1
        assert g.match(basis(5))[0] == "alice"
        assert g.match(basis(0))[0] is None

    def test_empty_name_and_empty_embeddings_are_rejected(self, g):
        g.load()
        with pytest.raises(ValueError):
            g.enroll("  ", [basis(0)])
        with pytest.raises(ValueError):
            g.enroll("alice", [])

    def test_wrong_dimensionality_is_rejected(self, g):
        g.load()
        with pytest.raises(ValueError):
            g.enroll("alice", [np.ones(128, dtype=np.float32)])


class TestMatch:
    def test_below_threshold_returns_no_name_but_still_the_score(self, g):
        g.load()
        g.enroll("alice", [basis(0)])
        name, score, cands = g.match(basis(1))
        assert name is None
        assert score == pytest.approx(0.0, abs=1e-6)
        assert cands[0]["name"] == "alice"

    def test_nearest_of_several_wins(self, g):
        g.load()
        for i, n in enumerate(["alice", "bob", "carol"]):
            g.enroll(n, [basis(i)])
        query = gallery_mod.l2_normalize(basis(1) * 0.9 + basis(2) * 0.3)
        name, score, cands = g.match(query)
        assert name == "bob"
        assert [c["name"] for c in cands] == ["bob", "carol", "alice"]
        assert score == pytest.approx(cands[0]["score"])

    def test_threshold_is_the_only_thing_between_match_and_unknown(self, tmp_path):
        loose = Gallery(str(tmp_path / "g.json"), TAG, dim=DIM,
                        threshold=0.10).load()
        loose.enroll("alice", [basis(0)])
        weak = gallery_mod.l2_normalize(basis(0) * 0.2 + basis(1) * 0.98)
        assert loose.match(weak)[0] == "alice"
        strict = Gallery(loose.path, TAG, dim=DIM, threshold=0.9).load()
        assert strict.match(weak)[0] is None

    def test_unnormalized_query_is_normalized_before_the_scan(self, g):
        g.load()
        g.enroll("alice", [basis(0)])
        name, score, _ = g.match(basis(0) * 37.0)
        assert name == "alice"
        assert score == pytest.approx(1.0, abs=1e-5)


class TestRemove:
    def test_remove_persists(self, g):
        g.load()
        g.enroll("alice", [basis(0)])
        g.enroll("bob", [basis(1)])
        assert g.remove("alice") is True
        assert [u["name"] for u in Gallery(g.path, TAG, dim=DIM).load().list()] \
            == ["bob"]

    def test_removing_a_stranger_is_false_not_an_exception(self, g):
        g.load()
        assert g.remove("nobody") is False
