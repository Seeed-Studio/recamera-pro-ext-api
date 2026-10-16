"""OpenCV 尺寸测量模块：同平面标定物求 mm/px，量 ROI 内目标，按公差判 OK/NG。

纯 CPU、纯几何，**不进 backend**，也不参与检测模型的推理路径。

三步：
1. **标定** —— 画面里放一个已知宽度的同平面标定物（ArUco 标记或规则轮廓），
   `mm_per_pixel = ref_object_width_mm / ref_width_px`。标定物必须与被测件同一平面，
   否则透视会把比例带偏，这个前提由现场保证，代码不校验。
2. **测量** —— 在配置的 ROI 内取最大外轮廓的最小外接矩形（`cv2.minAreaRect`），
   得到长边/短边像素数，乘 `mm_per_pixel` 得毫米值。
3. **判定** —— 与名义尺寸比，超出 ± 公差即 `out_of_tolerance`。

朝向无关：长边对名义值的长边，短边对短边。工件在 ROI 里横放竖放不影响判定。

`cv2` 在函数内部惰性导入，core 层的其它模块不因此依赖 OpenCV。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .types import parse_roi

# 轮廓点取的是像素中心，跨 p0..p1 的轮廓实际覆盖 p1-p0+1 个像素。
# 标定物与被测件都加同一个修正，比值误差基本抵消。
PIXEL_EXTENT_CORRECTION = 1.0
MIN_CONTRAST = 5.0          # 区域灰度标准差下限，低于此判为空白 ROI
MAX_AREA_FRACTION = 0.98    # 最大轮廓占 ROI 的面积比上限，超过判为背景

STATUS_OK = "ok"
STATUS_UNDERSIZE = "undersize"
STATUS_OVERSIZE = "oversize"
STATUS_NOT_FOUND = "not_found"
STATUS_UNCALIBRATED = "uncalibrated"

# Modbus HR 11 的公差判定码，契约见 contracts/MODBUS.md
STATUS_CODES = {
    STATUS_OK: 0,
    STATUS_UNDERSIZE: 1,
    STATUS_OVERSIZE: 2,
    STATUS_NOT_FOUND: 3,
    STATUS_UNCALIBRATED: 4,
}


class DimensionError(RuntimeError):
    """标定或测量失败。调用方应把它转成 not_found / uncalibrated，不要让它冒到主循环。"""


@dataclass(frozen=True)
class Calibration:
    """对应 config schema 的 `dimension.calibration` 段。"""

    ref_object_width_mm: float
    detect: str = "contour"                 # "contour" | "aruco"
    roi: tuple[float, float, float, float] | None = None   # 归一化，None = 全图
    aruco_dict: str = "DICT_4X4_50"
    aruco_id: int | None = None

    @classmethod
    def from_config(cls, config: dict) -> "Calibration":
        detect = str(config.get("detect", "contour"))
        if detect not in ("contour", "aruco"):
            raise ValueError(f"calibration.detect must be aruco|contour, got {detect!r}")
        roi = config.get("roi")
        return cls(
            ref_object_width_mm=float(config["ref_object_width_mm"]),
            detect=detect,
            roi=None if roi is None else parse_roi(roi),
            aruco_dict=str(config.get("aruco_dict", "DICT_4X4_50")),
            aruco_id=(None if config.get("aruco_id") is None
                      else int(config["aruco_id"])),
        )


@dataclass(frozen=True)
class MeasurementSpec:
    """一个被测特征。名义尺寸与公差都是毫米。"""

    name: str
    roi: tuple[float, float, float, float]
    nominal_long_mm: float
    nominal_short_mm: float
    tolerance_mm: float

    @classmethod
    def from_config(cls, config: dict) -> "MeasurementSpec":
        pair = sorted((float(config["nominal_width_mm"]),
                       float(config["nominal_height_mm"])), reverse=True)
        return cls(
            name=str(config["name"]),
            roi=parse_roi(config["roi"]),
            nominal_long_mm=pair[0],
            nominal_short_mm=pair[1],
            tolerance_mm=float(config["tolerance_mm"]),
        )


@dataclass(frozen=True)
class DimensionConfig:
    """对应 config schema 的 `dimension` 段。measurements 为空 = 不做尺寸判定。"""

    calibration: Calibration | None = None
    measurements: tuple[MeasurementSpec, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.measurements)

    @classmethod
    def from_config(cls, config: dict | None) -> "DimensionConfig":
        config = config or {}
        calibration = config.get("calibration")
        return cls(
            calibration=(Calibration.from_config(calibration)
                         if calibration else None),
            measurements=tuple(MeasurementSpec.from_config(m)
                               for m in config.get("measurements", ())),
        )


@dataclass(frozen=True)
class MeasurementResult:
    name: str
    status: str
    long_mm: float | None = None
    short_mm: float | None = None
    long_px: float | None = None
    short_px: float | None = None
    deviation_mm: float | None = None      # 偏差绝对值最大的那一边

    @property
    def in_tolerance(self) -> bool:
        return self.status == STATUS_OK

    @property
    def code(self) -> int:
        return STATUS_CODES[self.status]

    def to_json(self, spec: MeasurementSpec | None = None) -> dict:
        payload = {
            "name": self.name,
            "status": self.status,
            "status_code": self.code,
            "in_tolerance": self.in_tolerance,
            "measured_long_mm": (None if self.long_mm is None
                                 else round(float(self.long_mm), 3)),
            "measured_short_mm": (None if self.short_mm is None
                                  else round(float(self.short_mm), 3)),
            "deviation_mm": (None if self.deviation_mm is None
                             else round(float(self.deviation_mm), 3)),
        }
        if spec is not None:
            payload["nominal_long_mm"] = round(spec.nominal_long_mm, 3)
            payload["nominal_short_mm"] = round(spec.nominal_short_mm, 3)
            payload["tolerance_mm"] = round(spec.tolerance_mm, 3)
        return payload


@dataclass(frozen=True)
class DimensionResult:
    enabled: bool = False
    mm_per_pixel: float | None = None
    calibration_error: str = ""
    measurements: tuple[MeasurementResult, ...] = ()
    specs: tuple[MeasurementSpec, ...] = ()

    @property
    def calibrated(self) -> bool:
        return self.mm_per_pixel is not None

    @property
    def failures(self) -> tuple[MeasurementResult, ...]:
        return tuple(m for m in self.measurements if not m.in_tolerance)

    @property
    def primary(self) -> MeasurementResult | None:
        """Modbus HR 10/11 取的那一条：优先第一个不合格项，否则第一条测量。"""
        failures = self.failures
        if failures:
            return failures[0]
        return self.measurements[0] if self.measurements else None

    def to_json(self) -> dict:
        by_name = {s.name: s for s in self.specs}
        return {
            "enabled": self.enabled,
            "calibrated": self.calibrated,
            "mm_per_pixel": (None if self.mm_per_pixel is None
                             else round(float(self.mm_per_pixel), 6)),
            "calibration_error": self.calibration_error,
            "out_of_tolerance_count": len(self.failures),
            "measurements": [m.to_json(by_name.get(m.name))
                             for m in self.measurements],
        }


# -- 底层几何 -------------------------------------------------------------
def crop(image: np.ndarray, roi: tuple[float, float, float, float] | None
         ) -> tuple[np.ndarray, tuple[int, int]]:
    """按归一化 ROI 裁图，返回 (子图, 左上角像素偏移)。"""
    height, width = image.shape[:2]
    if roi is None:
        return image, (0, 0)
    x1 = int(round(roi[0] * width))
    y1 = int(round(roi[1] * height))
    x2 = int(round(roi[2] * width))
    y2 = int(round(roi[3] * height))
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(width, x2), min(height, y2)
    if x2 - x1 < 3 or y2 - y1 < 3:
        raise DimensionError(f"roi {roi} degenerates to {x2 - x1}x{y2 - y1} px")
    return image[y1:y2, x1:x2], (x1, y1)


def largest_contour_rect(image: np.ndarray, min_area_px: float = 25.0,
                         min_contrast: float = MIN_CONTRAST,
                         max_area_fraction: float = MAX_AREA_FRACTION
                         ) -> tuple[float, float]:
    """取最大外轮廓的最小外接矩形，返回 (长边 px, 短边 px)。

    两道"这里根本没有工件"的闸门，缺了它们空白 ROI 会返回整个 ROI 的尺寸：
    Otsu 在无对比度的区域上照样会切出一个阈值，把整片背景当成前景。
    - 区域灰度标准差 < `min_contrast` -> 判为空白；
    - 最大轮廓面积 >= `max_area_fraction` × 区域面积 -> 判为背景而不是工件。
    """
    import cv2

    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if float(gray.std()) < min_contrast:
        raise DimensionError(
            f"region contrast {gray.std():.2f} < {min_contrast}; nothing to measure")
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    # 前景/背景极性由边框像素的均值判定：边框亮 = 浅色背景 = 目标是暗的。
    border = np.concatenate([blurred[0, :], blurred[-1, :],
                             blurred[:, 0], blurred[:, -1]])
    mode = (cv2.THRESH_BINARY_INV if float(border.mean()) > 127.0
            else cv2.THRESH_BINARY)
    _, mask = cv2.threshold(blurred, 0, 255, mode + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    contours = [c for c in contours if cv2.contourArea(c) >= min_area_px]
    if not contours:
        raise DimensionError("no contour above min_area_px in the region")
    biggest = max(contours, key=cv2.contourArea)
    region_area = float(gray.shape[0] * gray.shape[1])
    if cv2.contourArea(biggest) >= max_area_fraction * region_area:
        raise DimensionError(
            "largest contour fills the whole region; the roi probably contains "
            "background only, or the workpiece is clipped by the roi")
    rect = cv2.minAreaRect(biggest)
    a, b = rect[1]
    a += PIXEL_EXTENT_CORRECTION
    b += PIXEL_EXTENT_CORRECTION
    return (max(a, b), min(a, b))


def aruco_marker_side_px(image: np.ndarray, dict_name: str,
                         marker_id: int | None) -> float:
    """ArUco 标记的平均边长（像素）。"""
    import cv2

    if not hasattr(cv2, "aruco"):
        raise DimensionError("cv2.aruco is unavailable (install opencv-contrib)")
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    attr = getattr(cv2.aruco, dict_name, None)
    if attr is None:
        raise DimensionError(f"unknown aruco dictionary {dict_name!r}")
    dictionary = cv2.aruco.getPredefinedDictionary(attr)
    if hasattr(cv2.aruco, "ArucoDetector"):
        detector = cv2.aruco.ArucoDetector(dictionary,
                                           cv2.aruco.DetectorParameters())
        corners, ids, _ = detector.detectMarkers(gray)
    else:  # OpenCV < 4.7
        corners, ids, _ = cv2.aruco.detectMarkers(gray, dictionary)
    if ids is None or len(corners) == 0:
        raise DimensionError("no aruco marker detected")
    chosen = None
    for corner, marker in zip(corners, ids.flatten().tolist()):
        if marker_id is None or marker == marker_id:
            chosen = corner.reshape(4, 2)
            break
    if chosen is None:
        raise DimensionError(f"aruco id {marker_id} not present")
    sides = [float(np.linalg.norm(chosen[i] - chosen[(i + 1) % 4]))
             for i in range(4)]
    return sum(sides) / 4.0


def estimate_mm_per_pixel(image: np.ndarray, calibration: Calibration) -> float:
    """同平面标定物 -> mm/px。失败抛 DimensionError。"""
    if calibration.ref_object_width_mm <= 0:
        raise DimensionError("ref_object_width_mm must be > 0")
    region, _ = crop(image, calibration.roi)
    if calibration.detect == "aruco":
        width_px = aruco_marker_side_px(region, calibration.aruco_dict,
                                        calibration.aruco_id)
    else:
        # 标定物的"宽度"取最小外接矩形的短边：这是与朝向无关的那一边。
        width_px = largest_contour_rect(region)[1]
    if width_px <= 0:
        raise DimensionError("reference object measured 0 px wide")
    return calibration.ref_object_width_mm / width_px


def measure(image: np.ndarray, spec: MeasurementSpec,
            mm_per_pixel: float) -> MeasurementResult:
    """量一个特征并判公差。"""
    try:
        region, _ = crop(image, spec.roi)
        long_px, short_px = largest_contour_rect(region)
    except DimensionError:
        return MeasurementResult(name=spec.name, status=STATUS_NOT_FOUND)
    long_mm = long_px * mm_per_pixel
    short_mm = short_px * mm_per_pixel
    d_long = long_mm - spec.nominal_long_mm
    d_short = short_mm - spec.nominal_short_mm
    worst = d_long if abs(d_long) >= abs(d_short) else d_short
    if abs(d_long) <= spec.tolerance_mm and abs(d_short) <= spec.tolerance_mm:
        status = STATUS_OK
    else:
        status = STATUS_OVERSIZE if worst > 0 else STATUS_UNDERSIZE
    return MeasurementResult(name=spec.name, status=status,
                             long_mm=long_mm, short_mm=short_mm,
                             long_px=long_px, short_px=short_px,
                             deviation_mm=worst)


def inspect(image: np.ndarray, config: DimensionConfig) -> DimensionResult:
    """标定 + 逐个测量。标定失败时所有测量项记 uncalibrated，不抛异常。"""
    if not config.enabled:
        return DimensionResult(enabled=False)
    if config.calibration is None:
        return DimensionResult(
            enabled=True, calibration_error="no calibration configured",
            specs=config.measurements,
            measurements=tuple(MeasurementResult(name=s.name,
                                                 status=STATUS_UNCALIBRATED)
                               for s in config.measurements))
    try:
        mm_per_pixel = estimate_mm_per_pixel(image, config.calibration)
    except DimensionError as exc:
        return DimensionResult(
            enabled=True, calibration_error=str(exc), specs=config.measurements,
            measurements=tuple(MeasurementResult(name=s.name,
                                                 status=STATUS_UNCALIBRATED)
                               for s in config.measurements))
    return DimensionResult(
        enabled=True, mm_per_pixel=mm_per_pixel, specs=config.measurements,
        measurements=tuple(measure(image, spec, mm_per_pixel)
                           for spec in config.measurements))
