"""Run a bundled CVSprout detector through AppMgr's scheduled NPU service."""
import json
from pathlib import Path

from kit.app import App, run_app
from kit.config import app_dir_of, load_manifest
from kit import events

from decoder import decode, validate_contract


class CVSproutInferenceApp(App):
    id = "cvsprout-inference"
    name = "CVSprout Inference"
    owns_loop = True
    model_frame = "hw-direct"
    model_dma_input = True

    def setup(self, config):
        super().setup(config)
        root = Path(app_dir_of(self))
        contract = json.loads((root / "model_contract.json").read_text(encoding="utf-8"))
        models = load_manifest(str(root)).get("models", [])
        if len(models) != 1:
            raise ValueError("CVSprout inference requires exactly one bundled detector")
        self.contract = validate_contract(contract, models[0])
        if list(self.class_names) != self.contract["classes"] or self._pre_size != self.contract["input_size"]:
            raise ValueError("Kit model metadata differs from the deployment contract")

    def run(self):
        for frame in self.frames():
            prepared = self.pre(frame)
            outputs = self.models.det.infer(prepared)
            detections = decode(outputs, prepared.info, self.contract, self.conf, self.iou)
            self.emit([events.detection(item) for item in detections], frame.pts,
                      results=detections,
                      extra={"model_release": self.contract.get("release_id", "")})


if __name__ == "__main__":
    run_app(CVSproutInferenceApp())
