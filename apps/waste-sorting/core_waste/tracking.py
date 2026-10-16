"""跨帧去重：同一件垃圾在连续帧里只计一次件数。

连续模式下同一件物体每帧都会被检出，不去重的话「今天投了多少件」会按帧数
翻十几倍。这里用两条弱信号做关联，不引入 ReID 模型：

1. **IoU**：前后帧框重叠。物体在传送带/投放口上位移不大时足够。
2. **简单外观**：裁剪块的 8×8×(H,S) 直方图余弦相似度。IoU 掉到阈值以下
   （物体动得快、或者被短暂挡住）时用它兜底，也用来挡住「一件离场、另一
   件立刻进到同一个位置」被误当成同一件的情况。

只有 `confirmed`（连续命中 `min_hits` 帧）的轨迹才计一次件数，之后同一条
轨迹再出现多少帧都不再计；`max_age` 帧没命中就删掉，同一位置后来的物体
拿到新的 track_id。这条规则是 spec §3「离场重置」。

纯 numpy，设备端与单测共用一份。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .detect import iou


def appearance_signature(patch: np.ndarray, bins: int = 8) -> np.ndarray:
    """裁剪块 -> 归一化的 H×S 联合直方图，L2 归一化后可直接点积。

    只用色相和饱和度、不用明度：合成与真实数据里最常见的干扰是光照，
    亮度一变直方图就跟着漂，色相相对稳。
    """
    import cv2

    if patch.size == 0:
        return np.zeros(bins * bins, dtype=np.float32)
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [bins, bins],
                        [0, 180, 0, 256]).astype(np.float32).ravel()
    norm = float(np.linalg.norm(hist))
    return hist / norm if norm > 1e-6 else hist


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 0.0
    return float(np.clip(np.dot(a, b), 0.0, 1.0))


@dataclass
class Track:
    track_id: str
    bbox: tuple[float, float, float, float]
    signature: np.ndarray
    class_name: str
    hits: int = 1
    misses: int = 0
    counted: bool = False
    first_frame: int = 0
    last_frame: int = 0
    history: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.counted


class ItemTracker:
    """IoU + 外观的贪心关联跟踪。一帧一次 `update`。

    `update` 返回和输入检测**等长**的赋值列表，每项
    `{"track_id", "is_new_item", "hits"}`：`is_new_item=True` 的那一条才计件。
    """

    def __init__(self, iou_threshold: float = 0.3,
                 appearance_threshold: float = 0.75,
                 min_hits: int = 2, max_age: int = 8,
                 session_id: str = "s0",
                 min_appearance_on_overlap: float = 0.5,
                 max_displacement_ratio: float = 1.5) -> None:
        self.iou_threshold = float(iou_threshold)
        self.appearance_threshold = float(appearance_threshold)
        #: 高 IoU 时仍要求的最低外观相似度——挡住"原位换了一件"
        self.min_appearance_on_overlap = float(min_appearance_on_overlap)
        #: 无重叠续接时允许的中心位移，单位是两框对角线均值的倍数
        self.max_displacement_ratio = float(max_displacement_ratio)
        self.min_hits = int(min_hits)
        self.max_age = int(max_age)
        self.session_id = session_id
        self.tracks: list[Track] = []
        self._next_id = 0
        self.counted_items = 0

    def _new_track_id(self) -> str:
        self._next_id += 1
        return f"{self.session_id}-t{self._next_id:06d}"

    def _affinity(self, track: Track, bbox, signature: np.ndarray) -> float:
        """IoU 与外观**联合**判据。返回 0 表示不允许关联。

        两条单独用都会错，而且错的方向相反：

        - **只看 IoU**：投放口是固定的，前一件刚拿走、后一件放到同一个位置，
          两个框的 IoU 接近 1，会被当成同一件，第二件漏计。所以高 IoU 也要
          过外观这一关（`min_appearance_on_overlap`）——同一件物体连续帧之间
          的 HS 直方图不会突变，换了一件通常会。
        - **只看外观**：同色同材质的两件（两个矿泉水瓶）签名几乎一样，画面
          两端各一个也会被连起来。所以无重叠时要额外要求**位移在合理范围内**
          （`max_displacement_ratio` × 两框尺度），挡住"隔半个画面续接"。

        无重叠但签名一致且位移合理的情况要留（`max_age` 内被短暂遮挡、或者
        检测器漏了一两帧），这正是遮挡场景不重复计数的关键。
        """
        overlap = iou(track.bbox, bbox)
        appearance = cosine(track.signature, signature)
        if overlap >= self.iou_threshold:
            if appearance < self.min_appearance_on_overlap:
                return 0.0        # 同一个位置换了一件东西
            return 0.6 * overlap + 0.4 * appearance
        if appearance >= self.appearance_threshold and \
                self._displacement_ok(track.bbox, bbox):
            return 0.4 * appearance
        return 0.0

    def _displacement_ok(self, a, b) -> bool:
        """两框中心距离是否在"同一件物体挪动过"的合理范围内。

        尺度取两框对角线的均值，位移上限是它的 `max_displacement_ratio` 倍
        乘以已经丢失的帧数上限——遮挡越久允许挪得越远。
        """
        import math

        acx, acy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
        bcx, bcy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        scale = (math.hypot(a[2] - a[0], a[3] - a[1])
                 + math.hypot(b[2] - b[0], b[3] - b[1])) / 2
        return math.hypot(acx - bcx, acy - bcy) <= \
            self.max_displacement_ratio * max(scale, 1.0)

    def update(self, detections, signatures, frame_id: int = 0) -> list[dict]:
        detections = list(detections)
        signatures = list(signatures)
        if len(detections) != len(signatures):
            raise ValueError("detections and signatures must be the same length")

        pairs = []
        for d_index, detection in enumerate(detections):
            for t_index, track in enumerate(self.tracks):
                score = self._affinity(track, detection.bbox,
                                       signatures[d_index])
                if score > 0:
                    pairs.append((score, d_index, t_index))
        pairs.sort(key=lambda p: -p[0])

        assigned_d: dict[int, int] = {}
        used_t: set[int] = set()
        for _, d_index, t_index in pairs:
            if d_index in assigned_d or t_index in used_t:
                continue
            assigned_d[d_index] = t_index
            used_t.add(t_index)

        results: list[dict] = []
        for d_index, detection in enumerate(detections):
            if d_index in assigned_d:
                track = self.tracks[assigned_d[d_index]]
                track.bbox = detection.bbox
                # 外观做指数平滑，单帧的反光/模糊不会一下子改掉签名
                track.signature = _blend(track.signature, signatures[d_index])
                track.hits += 1
                track.misses = 0
                track.last_frame = frame_id
            else:
                track = Track(self._new_track_id(), detection.bbox,
                              signatures[d_index], detection.class_name,
                              first_frame=frame_id, last_frame=frame_id)
                self.tracks.append(track)
            is_new = False
            if not track.counted and track.hits >= self.min_hits:
                track.counted = True
                self.counted_items += 1
                is_new = True
            results.append({"track_id": track.track_id,
                            "is_new_item": is_new,
                            "hits": track.hits})

        self._age(used_t, frame_id)
        return results

    def _age(self, used: set[int], frame_id: int) -> None:
        """没被匹配上的轨迹 miss +1，超过 max_age 删除。"""
        for index, track in enumerate(self.tracks):
            if index not in used and track.last_frame != frame_id:
                track.misses += 1
        self.tracks = [t for t in self.tracks if t.misses <= self.max_age]

    def mark_empty_frame(self, frame_id: int = 0) -> None:
        """这一帧一个框都没有：仍然要推进老化。

        之前流水线在空帧上直接回退、根本不碰跟踪器，于是"出现 2 帧 → 空 10
        帧 → 原位再出现"会续上同一条轨迹，第二件永远不计数。空帧和"检出了
        但没匹配上"对轨迹的意义是一样的，都要 miss +1。
        """
        self._age(set(), frame_id)

    def reset(self) -> None:
        self.tracks.clear()
        self.counted_items = 0


def _blend(old: np.ndarray, new: np.ndarray, alpha: float = 0.7) -> np.ndarray:
    if old.shape != new.shape:
        return new
    mixed = alpha * old + (1 - alpha) * new
    norm = float(np.linalg.norm(mixed))
    return mixed / norm if norm > 1e-6 else mixed
