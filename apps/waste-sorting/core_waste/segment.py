"""背景差分切割：固定机位下用 OpenCV 找出"有东西的地方"，不训检测器。

场景是固定机位俯拍分拣台 / 桶口，背景不变。这种场景下"哪里有东西"是一个
**几何问题**，不是识别问题：先拍一张空台背景帧，运行时逐帧与它相减，形态学
清一下噪点，取连通域，每个连通域就是一件。材质仍然交给已验收的 m1c 分类器
逐 crop 分类。

为什么不训单类检测器：训一个只会说"这是一件垃圾"的模型，学的其实就是
"前景 / 背景"，而固定机位已经把这个信息白送了。代价那边则实打实——要标注、
要 GPU、要四个平台各转一次、每帧多一次推理。背景差分零参数、零推理、
在任何平台上都是同一份 numpy + cv2。

它实现 `core_waste.detect.Detector` 协议，所以 `MultiItemPipeline`、事件契约、
跨帧去重、多件拆分全部原样复用——换掉的只是"框从哪来"。

两种取背景的方式：

- **`static`（默认，推荐）**：显式给一张空台背景帧。确定性最好，重跑同一段
  视频得到同一批框，评测和回归 diff 才有意义。
- **`mog2`**：`cv2.createBackgroundSubtractorMOG2` 在线学背景。不需要事先拍
  空台，但前几十帧不可用，而且物体长时间不动会被学进背景（分拣台上放着不
  管的东西会"消失"）。只在拿不到空台帧时用。

局限写在这里，不要在别处重复发现：**两件挨在一起会连成一个连通域**，计数就
少一件。`split_touching`（距离变换 + 分水岭）是为这种情况准备的，它**确实
救得回一部分**（粘连版面召回 0.573→0.653、计数 MAE 1.27→0.95），代价是摆开
的版面精确率掉 12.5 pp、计数 MAE 翻倍——一件长条物体自己就有多个局部极值，
会被切碎。合成集里摆开的版面占 74%，总账为负，所以**默认关**；以粘连为常态
的现场（投放口一次倒一堆）应当开到 seed 0.25 / part 0.15。数字见
`SegmenterConfig.split_touching` 的注释与
`evaluation/runs/2026-09-07-multi-item/results.md` §2。

粘连之外还有一层天花板：重叠严重时分水岭也切不开。spec 里记的备选方案
（用设备自带的通用检测器出类无关的物体框）就是为它准备的。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .detect import Detection

#: 前景块小于整帧这个比例就丢掉（噪点、阴影边缘）
DEFAULT_MIN_AREA_RATIO = 0.0008
#: 前景块大于这个比例视为整帧变化（有人挡住了、灯变了），整帧放弃
DEFAULT_MAX_AREA_RATIO = 0.55
#: 灰度差大于这个阈值算前景
DEFAULT_DIFF_THRESHOLD = 18
#: 中心距离小于这个比例（相对帧对角线）的两个块合并成一件
DEFAULT_MERGE_DISTANCE_RATIO = 0.0
#: 形态学核边长（像素），先开后闭
DEFAULT_OPEN_KERNEL = 3
DEFAULT_CLOSE_KERNEL = 7


@dataclass
class SegmenterConfig:
    """切割参数。默认值是在合成集（640² 固定背景）上调出来的。"""

    mode: str = "static"                       # static | mog2
    diff_threshold: int = DEFAULT_DIFF_THRESHOLD
    min_area_ratio: float = DEFAULT_MIN_AREA_RATIO
    max_area_ratio: float = DEFAULT_MAX_AREA_RATIO
    merge_distance_ratio: float = DEFAULT_MERGE_DISTANCE_RATIO
    open_kernel: int = DEFAULT_OPEN_KERNEL
    close_kernel: int = DEFAULT_CLOSE_KERNEL
    min_box_px: int = 12
    #: 用距离变换 + 分水岭把粘连的前景块切开。**默认关**，但这是一个权衡，
    #: 不是"开了一无是处"（修掉标签计数 off-by-one 之后重测的结论，
    #: 250 张合成 test，类无关口径）：
    #:
    #:   off              separated P0.865 R0.944 MAE0.40 | touching R0.573 MAE1.27
    #:   seed0.25 part0.15 separated P0.740 R0.887 MAE0.80 | touching R0.653 MAE0.95
    #:
    #: 贴在一起的版面确实被救回来一些（R +8 pp、MAE −25%），代价是摆开的
    #: 版面精确率掉 12.5 pp、计数 MAE 翻倍。合成集里摆开的占 74%，所以总账
    #: 是负的（all MAE 0.63→0.84），默认关。**现场如果以粘连为常态**
    #: （投放口一次倒一堆），开到 seed 0.25 / part 0.15 是划算的——先在本地
    #: 数据上量一次再定。
    split_touching: bool = False
    #: 分水岭的种子阈值：距离变换归一化后大于它的像素当种子。调高切得更碎，
    #: 调低更容易漏切。
    split_seed_ratio: float = 0.30
    #: 切出来的每一块至少要占原连通域这么多面积，否则整块保留。挡住"一件
    #: 长条物体被自己的局部极值切碎"
    split_min_part_ratio: float = 0.25
    max_items: int = 16
    #: mog2 用；static 模式忽略
    mog2_history: int = 200
    mog2_var_threshold: float = 32.0
    mog2_learning_rate: float = 0.002
    #: 背景刷新间隔（秒）。static 模式下背景帧一旦拍定就不再更新，现场的
    #: 自动曝光漂移、日光变化、台面被挪动都会让差分整片误报。>0 时
    #: `maybe_refresh()` 会在"连续多帧判定为空台"且距上次刷新超过这个间隔时
    #: 自动重拍背景；0 表示只接受手动 `set_background()`。
    background_refresh_interval_s: float = 0.0
    #: 自动刷新前要求连续多少帧看起来是空台（前景占比低于
    #: `background_idle_ratio`）。太小会把静止的物体学进背景。
    background_idle_frames: int = 30
    background_idle_ratio: float = 0.005

    @classmethod
    def from_dict(cls, data: dict | None) -> "SegmenterConfig":
        """从 config 的 `segmenter` 段建。未知键直接报错，不静默忽略。"""
        data = dict(data or {})
        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown segmenter keys: {unknown}")
        return cls(**data)


def _split_touching(mask: np.ndarray, seed_ratio: float,
                    min_part_ratio: float = 0.25):
    """逐连通域做距离变换 + 分水岭，只在"确实像两件"时才接受切分结果。

    **不能对整张 mask 一把梭**。全局阈值下，一件长条形物体（瓶子、纸盒）自己
    就有好几个局部最大值，会被切成三四块——实测这样做 separated 版面的精确率
    从 0.94 掉到 0.55、计数 MAE 从 0.2 涨到 1.6，比不切还差。

    所以：对每个连通域单独求距离变换，用**该连通域自己的**峰值定种子阈值；
    切出来的每一块都要占到原连通域 `min_part_ratio` 以上的面积，且至少切出
    两块，才采纳；否则保留整块。这条规则的意思是"只接受把一块切成几块**势均
    力敌**的结果"，一件物体上的边角局部极值切不出势均力敌的两块，自然被否掉。

    重叠严重（一件压在另一件上一大半）时仍然切不开——那是这条路线的天花板，
    模块头部的注释里记了备选方案。
    """
    import cv2

    count, base = cv2.connectedComponents(mask, 8)
    labels = np.zeros_like(base, np.int32)
    next_label = 0
    for value in range(1, count):
        component = (base == value).astype(np.uint8) * 255
        area = int(np.count_nonzero(component))
        parts = _watershed_component(component, seed_ratio)
        accepted = [part for part in parts
                    if int(np.count_nonzero(part)) >= min_part_ratio * area]
        if len(accepted) < 2:
            accepted = [component > 0]
        for part in accepted:
            next_label += 1
            labels[part] = next_label
    # 返回值按 cv2.connectedComponents 的约定：count = 标签总数（含背景 0），
    # 调用方用 range(1, count) 遍历前景。之前这里返回的是"前景标签个数"，
    # 少了背景那一格，range(1, count) 就漏掉最后一个连通域——物体越少漏得
    # 越狠（只切出 2 块时漏掉 1 块）。
    return labels, next_label + 1


def _watershed_component(component: np.ndarray, seed_ratio: float):
    """对单个连通域做分水岭，返回每一块的布尔掩码。"""
    import cv2

    distance = cv2.distanceTransform(component, cv2.DIST_L2, 5)
    peak = float(distance.max())
    if peak <= 0:
        return []
    seeds = np.where(distance >= seed_ratio * peak, 255, 0).astype(np.uint8)
    seed_count, markers = cv2.connectedComponents(seeds, 8)
    if seed_count <= 2:                      # 只有一个核，没什么可切
        return []
    markers = markers + 1
    markers[(component > 0) & (seeds == 0)] = 0
    markers[component == 0] = 1
    cv2.watershed(cv2.cvtColor(component, cv2.COLOR_GRAY2BGR), markers)
    return [(markers == value) for value in range(2, seed_count + 1)]


def _stats_from_labels(labels: np.ndarray, count: int) -> np.ndarray:
    """把 label 图转成和 connectedComponentsWithStats 同形状的 stats 数组。

    `count` 与 cv2 一致：标签总数（含背景 0），前景是 1..count-1。
    """
    import cv2

    stats = np.zeros((count, 5), np.int32)
    for value in range(1, count):
        ys, xs = np.nonzero(labels == value)
        if len(ys) == 0:
            continue
        stats[value, cv2.CC_STAT_LEFT] = int(xs.min())
        stats[value, cv2.CC_STAT_TOP] = int(ys.min())
        stats[value, cv2.CC_STAT_WIDTH] = int(xs.max() - xs.min() + 1)
        stats[value, cv2.CC_STAT_HEIGHT] = int(ys.max() - ys.min() + 1)
        stats[value, cv2.CC_STAT_AREA] = int(len(ys))
    return stats


class SegmenterError(RuntimeError):
    """背景没设、或者输入帧尺寸和背景对不上。"""


def _merge_boxes(boxes: list[tuple[float, float, float, float]],
                 max_distance: float) -> list[tuple[float, float, float, float]]:
    """中心距离小于 `max_distance` 或已经重叠的框并成一个。

    一件物体常常被切成几块（瓶身反光把瓶子切成两段、纸盒的浅色面差分不出来），
    不合并的话一件会报成两三件，计数直接翻倍。
    """
    import math

    def contained(a, b) -> bool:
        """小框的面积有 80% 以上落在大框里 —— 同一件物体被切成了里外两块。"""
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        smaller = min((a[2] - a[0]) * (a[3] - a[1]),
                      (b[2] - b[0]) * (b[3] - b[1]))
        return smaller > 0 and inter / smaller >= 0.8

    # 注意：这里**不能**按"外接框相交"就合并。物体是任意朝向的，旋转之后的
    # 外接框天然互相压一角，一合并就级联，整帧最后并成一件（实测 6 件并成
    # 1 件）。只认"中心足够近"和"一个基本被另一个包住"。
    remaining = list(boxes)
    merged = True
    while merged and len(remaining) > 1:
        merged = False
        for i in range(len(remaining)):
            for j in range(i + 1, len(remaining)):
                a, b = remaining[i], remaining[j]
                distance = math.hypot((a[0] + a[2]) / 2 - (b[0] + b[2]) / 2,
                                      (a[1] + a[3]) / 2 - (b[1] + b[3]) / 2)
                if contained(a, b) or distance <= max_distance:
                    remaining[i] = (min(a[0], b[0]), min(a[1], b[1]),
                                    max(a[2], b[2]), max(a[3], b[3]))
                    remaining.pop(j)
                    merged = True
                    break
            if merged:
                break
    return remaining


class BackgroundSegmenter:
    """固定机位的前景切割器。实现 `core_waste.detect.Detector` 协议。"""

    accelerator = "cpu"
    #: 没有模型，事件里的检测器指纹用参数指纹代替（见 `fingerprint`）
    onnx_sha256 = ""
    classes = ("waste",)

    def __init__(self, config: SegmenterConfig | None = None,
                 background: np.ndarray | None = None) -> None:
        self.config = config or SegmenterConfig()
        if self.config.mode not in ("static", "mog2"):
            raise SegmenterError(f"unknown mode {self.config.mode!r}")
        self._background_gray: np.ndarray | None = None
        self._mog2 = None
        self._idle_frames = 0
        self._last_refresh_s = 0.0
        self.input_size = (0, 0)          # 原生分辨率，不缩放
        if background is not None:
            self.set_background(background)

    # ---- 背景 ----------------------------------------------------------
    def set_background(self, frame: np.ndarray) -> None:
        """设空台背景帧（HWC uint8 BGR）。static 模式必须先调这个。"""
        import cv2

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        self._background_gray = cv2.GaussianBlur(gray, (5, 5), 0)
        self.input_size = (frame.shape[1], frame.shape[0])

    @property
    def fingerprint(self) -> str:
        """参数指纹，写进事件的 `model.segmenter`，用来对齐评测与现场。"""
        import hashlib
        import json

        payload = json.dumps(self.config.__dict__, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def warmup(self, iterations: int = 3) -> None:
        """背景差分没有 lazy 初始化，留空以满足协议。"""

    def maybe_refresh(self, frame: np.ndarray, now_s: float) -> bool:
        """看这一帧能不能当新的背景。返回是否真的刷新了。

        触发条件是**两条同时满足**：连续 `background_idle_frames` 帧的前景
        占比都低于 `background_idle_ratio`（台面确实空了），且距上次刷新
        超过 `background_refresh_interval_s`。两条缺一不可——只看"空"会把
        放着不动的物体学进背景（MOG2 的老毛病），只看时间会在台上有东西时
        把物体拍进背景。

        调用方每帧调一次；`background_refresh_interval_s = 0`（默认）时这个
        方法什么都不做，背景只能手动 `set_background()`。
        """
        if self.config.background_refresh_interval_s <= 0:
            return False
        try:
            ratio = float(self.foreground_mask(frame).mean()) / 255.0
        except SegmenterError:
            return False
        if ratio > self.config.background_idle_ratio:
            self._idle_frames = 0
            return False
        self._idle_frames += 1
        if self._idle_frames < self.config.background_idle_frames:
            return False
        if now_s - self._last_refresh_s < \
                self.config.background_refresh_interval_s:
            return False
        self.set_background(frame)
        self._idle_frames = 0
        self._last_refresh_s = now_s
        return True

    # ---- 前景掩码 ------------------------------------------------------
    def foreground_mask(self, image: np.ndarray) -> np.ndarray:
        import cv2

        if image.ndim != 3 or image.shape[2] != 3:
            raise SegmenterError(f"expected HWC BGR image, got {image.shape}")
        gray = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY),
                                (5, 5), 0)
        if self.config.mode == "mog2":
            if self._mog2 is None:
                self._mog2 = cv2.createBackgroundSubtractorMOG2(
                    history=self.config.mog2_history,
                    varThreshold=self.config.mog2_var_threshold,
                    detectShadows=True)
            mask = self._mog2.apply(image, learningRate=
                                    self.config.mog2_learning_rate)
            # MOG2 把阴影标成 127，阴影不是物体
            mask = np.where(mask >= 200, 255, 0).astype(np.uint8)
        else:
            if self._background_gray is None:
                raise SegmenterError(
                    "static mode needs a background frame; call "
                    "set_background() with an empty-table capture first")
            if self._background_gray.shape != gray.shape:
                raise SegmenterError(
                    f"frame {gray.shape} does not match background "
                    f"{self._background_gray.shape}")
            diff = cv2.absdiff(gray, self._background_gray)
            mask = np.where(diff >= self.config.diff_threshold,
                            255, 0).astype(np.uint8)

        if self.config.open_kernel > 1:
            k = np.ones((self.config.open_kernel,) * 2, np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
        if self.config.close_kernel > 1:
            k = np.ones((self.config.close_kernel,) * 2, np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
        return mask

    # ---- 框 ------------------------------------------------------------
    def detect(self, image: np.ndarray) -> list[Detection]:
        import cv2
        import math

        mask = self.foreground_mask(image)
        height, width = mask.shape
        frame_area = float(height * width)
        if mask.mean() / 255.0 > self.config.max_area_ratio:
            # 整帧都在变（有人俯身、灯闪了）：这一帧的框不可信，交给回退
            return []

        if self.config.split_touching:
            labels, count = _split_touching(mask,
                                            self.config.split_seed_ratio,
                                            self.config.split_min_part_ratio)
            stats = _stats_from_labels(labels, count)
        else:
            count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        raw: list[tuple[float, float, float, float]] = []
        areas: dict[tuple, int] = {}
        for index in range(1, count):
            area = int(stats[index, cv2.CC_STAT_AREA])
            if area / frame_area < self.config.min_area_ratio:
                continue
            x = float(stats[index, cv2.CC_STAT_LEFT])
            y = float(stats[index, cv2.CC_STAT_TOP])
            w = float(stats[index, cv2.CC_STAT_WIDTH])
            h = float(stats[index, cv2.CC_STAT_HEIGHT])
            box = (x, y, x + w, y + h)
            raw.append(box)
            areas[box] = area
        if not raw:
            return []

        diagonal = math.hypot(width, height)
        boxes = _merge_boxes(raw, self.config.merge_distance_ratio * diagonal)

        results: list[Detection] = []
        for box in boxes:
            x1, y1, x2, y2 = box
            if (x2 - x1) < self.config.min_box_px or \
                    (y2 - y1) < self.config.min_box_px:
                continue
            # 没有分类分数可报，用"框内前景占比"当置信度：贴合的框接近 1，
            # 把背景一起圈进来的框会明显偏低，下游可以按它设阈值。
            patch = mask[int(y1):int(y2), int(x1):int(x2)]
            fill = float(patch.mean()) / 255.0 if patch.size else 0.0
            results.append(Detection((x1, y1, x2, y2), round(fill, 4), 0,
                                     "waste"))
        results.sort(key=lambda d: -d.area)
        return results[:self.config.max_items]

    def close(self) -> None:
        self._mog2 = None
