"""分类后端接口。M1 冻结，后续 backends/{tensorrt,hailo,rknn} 各自实现同一接口。

设计上留了两处可换的余地，因为模型分两条 track（README「基线 track / 先进 track」）：

1. **可换骨干**：`Classifier` 只承诺「吃一帧 BGR，吐一维 logits」，
   骨干是 MobileNetV3 / EfficientNet-Lite0 / 蒸馏出来的小模型都不影响 core。
   `backbone` 字段只做记录与上报，core 不按它分支。
2. **可换头（开放词汇）**：`classes` 不是常量而是实例属性，
   `OpenVocabularyClassifier` 在此基础上多一个 `set_classes()`，
   允许运行时换掉类别表（CLIP / SigLIP 那条路线的文本侧 prompt 变了，
   logits 的长度就跟着变）。core 层的 top-k、softmax、四分类映射
   全部按 `classifier.classes` 走，不写死八类。

softmax / top-k / 中国四分类映射一律留在 `core_waste.taxonomy`，backend
只负责预处理、推理调用，把**未归一化的 logits** 原样交出来。理由与 A 项目
把解码 + NMS 放在 core 一样：四个平台共用同一份后处理，是「平台后处理漂移」
最直接的对策。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

import numpy as np


@runtime_checkable
class Classifier(Protocol):
    """所有加速器后端实现的唯一接口。"""

    #: 后端标识，取值同 contracts 的 model.accelerator
    accelerator: str
    #: 骨干名，仅用于上报（MQTT model.backbone），core 不按它分支
    backbone: str
    #: 类别表，顺序即 class_id。实例属性而非常量：开放词汇头可以换
    classes: tuple[str, ...]
    #: 模型输入尺寸 (width, height)
    input_size: tuple[int, int]
    #: 产出该后端制品的 ONNX 的 SHA256，写进 MQTT model.onnx_sha256
    onnx_sha256: str

    def warmup(self, iterations: int = 3) -> None:
        """跑若干次空推理，把 lazy 初始化和显存分配挪到启动阶段。"""

    def predict(self, image: np.ndarray) -> np.ndarray:
        """输入 HWC uint8 BGR 图，返回形状 (len(classes),) 的 float32 logits。

        实现负责：resize / center-crop / 归一化、推理、把 batch 维摘掉。
        实现不负责：softmax、top-k、四分类映射、去抖、MQTT、GPIO。
        """

    def close(self) -> None:
        """释放句柄。可重复调用。"""


@runtime_checkable
class OpenVocabularyClassifier(Classifier, Protocol):
    """开放词汇分类头（CLIP / SigLIP 那条先进 track）的附加能力。

    基线 track 的 `MobileNetV3` 不实现这个协议——它的分类头是训练时固定的。
    运行时用 `isinstance(clf, OpenVocabularyClassifier)` 判断能不能换词表。
    """

    def set_classes(self, classes: Sequence[str]) -> None:
        """换掉类别表并重建文本侧嵌入。之后 `predict` 的 logits 长度随之变化。"""


class ClassifierError(RuntimeError):
    """后端初始化或推理失败。"""


def sha256_of(path: str | Path) -> str:
    """文件的 SHA256。写进 MQTT `model.onnx_sha256` 的那个值。

    每个后端各写一份的话，「两个平台报的 hash 对不上」既可能是制品不同，
    也可能是分块大小/编码不同——所有后端共用这一份实现，就只剩前一种可能。
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_image_input(session, input_size: tuple[int, int]) -> tuple[str, list]:
    """校验 ONNX 输入侧的共同契约，返回 (input_name, input_shape)。

    两条 track 的输出侧不同（闭集是 [1,C] logits，开放词汇是 [1,D] 嵌入），
    输入侧是同一份契约：单输入、名为 `images`、静态 1×3×H×W。
    """
    inputs = session.get_inputs()
    if len(inputs) != 1:
        raise ClassifierError(f"expected a single input, got {len(inputs)}")
    input_name = inputs[0].name
    input_shape = list(inputs[0].shape)
    if input_name != "images":
        raise ClassifierError(
            f"input must be named 'images' (contract), got {input_name!r}")
    if input_shape != [1, 3, input_size[1], input_size[0]]:
        raise ClassifierError(
            f"expected static shape [1,3,{input_size[1]},{input_size[0]}], "
            f"got {input_shape}")
    return input_name, input_shape
