"""Typed, scheduled model factory for YOLO-World's heterogeneous inputs.

Kit's compatibility RknnModel treats input as one uint8 image. A strict session
is required to preserve both the text features and CLIP token IDs on the wire.
AppMgr still owns authorization, backend choice and every native model handle.
"""
import os

from kit.errors import CapabilityError, InferenceError
from kit.runtime.engine import ModelSpec, TensorSpec
from kit.runtime.remote import RemoteRknnSession, configured_inference_socket


def model_spec(path):
    filename = os.path.basename(path)
    if filename == "world_v21s.rknn":
        return ModelSpec(path=path, name="detector", inputs=(
            TensorSpec("images", (1, 640, 640, 3), "uint8", "NHWC"),
            TensorSpec("texts", (1, 80, 512), "float32", "NLC"),
        ))
    if filename == "clip_v21.rknn":
        # The native graph uses integer indices; RKNNLite performs the native
        # conversion from the float32 input accepted by the Kit client.
        return ModelSpec(path=path, name="text_encoder", inputs=(
            TensorSpec("input_ids", (1, 20), "float32", "NC"),
        ))
    raise ValueError(f"unexpected open-vocabulary model: {filename}")


def load_scheduled_model(path):
    socket_path = configured_inference_socket()
    if not socket_path:
        raise CapabilityError(
            "开放词汇检测需要 AppMgr 注入的推理调度端点，请通过 App Center 启动",
            operation="open_vocabulary.model.load",
            code="scheduled_service_required",
        )
    try:
        return RemoteRknnSession(model_spec(path), socket_path=socket_path,
                                 strict_inputs=True, shared_io=False)
    except InferenceError as exc:
        if "CtypesUnsupportedModel" not in str(exc):
            raise
        raise CapabilityError(
            "设备推理服务无法为开放词汇模型选择 RKNNLite。请检查推理服务版本及其 "
            "ESK_RKNN_BACKEND 配置（应为 auto 或 rknnlite）；应用进程的环境变量"
            "不能改变服务进程的后端。原始错误：" + str(exc),
            operation="open_vocabulary.model.load",
            code="open_vocabulary_backend_unsupported",
        ) from exc
