"""Modbus TCP 寄存器编码。契约见 contracts/MODBUS.md。

纯函数，不依赖 pymodbus，因此可以单测。运行时把 encode_* 的结果按
「先写 HR 再写 Coil」的顺序落到 datastore。

Demo B 在 Demo A 的 HR 0–7 之后追加四个寄存器：
HR 8 = 缺件数、HR 9 = 多余件数、HR 10 = 主尺寸测量值 ×100 (mm)、
HR 11 = 公差判定码（`dimension.STATUS_CODES`）。HR 0–7 的语义不变。
"""
from __future__ import annotations

from dataclasses import dataclass

from .dimension import STATUS_CODES, STATUS_UNCALIBRATED, DimensionResult
from .rules import Verdict

SCALE = 10000
MM_SCALE = 100          # HR 10：毫米 ×100，因此上限 655.35 mm
UINT16_MAX = 0xFFFF

COIL_NG = 0
COIL_OK = 1
HR_PRIMARY_CLASS = 0
HR_DEFECT_COUNT = 1
HR_BBOX_CX = 2
HR_BBOX_CY = 3
HR_BBOX_W = 4
HR_BBOX_H = 5
HR_HEARTBEAT_HI = 6
HR_HEARTBEAT_LO = 7
HR_MISSING_COUNT = 8
HR_EXTRA_COUNT = 9
HR_DIMENSION_VALUE = 10
HR_DIMENSION_STATUS = 11
HR_COUNT = 12


def encode_normalized(value: float) -> int:
    """归一化坐标 -> uint16。先夹到 [0,1]，再 ×10000 四舍五入。"""
    clamped = min(max(float(value), 0.0), 1.0)
    return int(round(clamped * SCALE))


def encode_uint16(value: int) -> int:
    """任意整数 -> uint16，越界饱和而不是回绕。"""
    return min(max(int(value), 0), UINT16_MAX)


def encode_heartbeat(unix_seconds: int) -> tuple[int, int]:
    """Unix 秒 -> (高字, 低字)。"""
    seconds = max(int(unix_seconds), 0) & 0xFFFFFFFF
    return (seconds >> 16) & 0xFFFF, seconds & 0xFFFF


def encode_millimetres(value: float) -> int:
    """毫米 -> uint16。×100 四舍五入，负值取 0，越界饱和（上限 655.35 mm）。"""
    return encode_uint16(int(round(max(float(value), 0.0) * MM_SCALE)))


@dataclass(frozen=True)
class ModbusFrame:
    """一次判定要写入的全部内容。写入顺序：registers 先，coils 后。"""

    registers: tuple[int, ...]   # HR 0..11
    coils: tuple[bool, bool]     # (Coil0=NG, Coil1=OK)

    def __post_init__(self) -> None:
        if len(self.registers) != HR_COUNT:
            raise ValueError(f"expected {HR_COUNT} holding registers")
        if self.coils[0] == self.coils[1]:
            raise ValueError("Coil0 (NG) and Coil1 (OK) must be mutually exclusive")


def encode_verdict(verdict: Verdict, unix_seconds: int,
                   dimension: DimensionResult | None = None) -> ModbusFrame:
    """把一次判定编码成寄存器 + 线圈。OK 时 HR0–5 全 0。

    HR 10/11 取 `DimensionResult.primary`：有不合格项时取第一个不合格项，
    否则取第一条测量。没配尺寸模块时写 (0, STATUS_UNCALIBRATED)。
    """
    primary = verdict.primary if verdict.is_ng else None
    count = len(verdict.defects) if verdict.is_ng else 0
    if primary is None:
        bbox = (0, 0, 0, 0)
        class_id = 0
    else:
        bbox = (encode_normalized(primary.cx), encode_normalized(primary.cy),
                encode_normalized(primary.w), encode_normalized(primary.h))
        class_id = encode_uint16(primary.class_id)
    hi, lo = encode_heartbeat(unix_seconds)
    primary_measure = dimension.primary if dimension is not None else None
    if primary_measure is None:
        dim_value, dim_status = 0, STATUS_CODES[STATUS_UNCALIBRATED]
    else:
        dim_value = (0 if primary_measure.long_mm is None
                     else encode_millimetres(primary_measure.long_mm))
        dim_status = primary_measure.code
    registers = (class_id, encode_uint16(count), *bbox, hi, lo,
                 encode_uint16(verdict.missing_count),
                 encode_uint16(verdict.extra_count),
                 dim_value, dim_status)
    return ModbusFrame(registers=registers,
                       coils=(verdict.is_ng, not verdict.is_ng))
