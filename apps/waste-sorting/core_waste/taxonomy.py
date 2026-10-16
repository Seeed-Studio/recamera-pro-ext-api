"""垃圾分类的类别体系。顺序即 class_id，训练、导出、推理、评测四处必须一致。

两层：
- **物料八类**（`MATERIAL_CLASSES`）：模型直接输出的 logits 维度，来自
  TrashNet 与 Roboflow GC3 两个 CC BY 4.0 数据集的类别并集映射（见
  `data/DATASET.md`）。
- **中国生活垃圾四分类**（`CHINA_CATEGORIES`）：物料类别到国标投放类别的
  纯映射，不参与训练，运行时查表得到。有害垃圾（hazardous）本轮不训练，
  映射表里保留常量但没有任何物料类映射到它。

这里只放纯 Python，不 import numpy 以外的东西：设备上的运行时、Mac 上的
单测、评测脚本共用同一份表。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

# ---- 物料层（模型输出）-------------------------------------------------
MATERIAL_CLASSES: tuple[str, ...] = (
    "paper",      # 0
    "cardboard",  # 1
    "glass",      # 2
    "metal",      # 3
    "plastic",    # 4
    "textile",    # 5
    "organic",    # 6
    "residual",   # 7
)

DEFAULT_CLASSES = MATERIAL_CLASSES
NUM_CLASSES = len(MATERIAL_CLASSES)

CLASS_ID = {name: index for index, name in enumerate(MATERIAL_CLASSES)}

# ---- 中国生活垃圾四分类（GB/T 19095-2019 的通俗四分法）-----------------
RECYCLABLE = "recyclable"    # 可回收物
KITCHEN = "kitchen"          # 厨余垃圾
HAZARDOUS = "hazardous"      # 有害垃圾（本轮不训练，无物料类映射到它）
RESIDUAL = "residual"        # 其他垃圾

CHINA_CATEGORIES: tuple[str, ...] = (RECYCLABLE, KITCHEN, HAZARDOUS, RESIDUAL)

CHINA_CATEGORY_ZH: dict[str, str] = {
    RECYCLABLE: "可回收物",
    KITCHEN: "厨余垃圾",
    HAZARDOUS: "有害垃圾",
    RESIDUAL: "其他垃圾",
}

#: 物料类 -> 中国四分类。spec §2：前六类 -> 可回收，organic -> 厨余，
#: residual -> 其他；有害类暂不训练。
MATERIAL_TO_CHINA: dict[str, str] = {
    "paper": RECYCLABLE,
    "cardboard": RECYCLABLE,
    "glass": RECYCLABLE,
    "metal": RECYCLABLE,
    "plastic": RECYCLABLE,
    "textile": RECYCLABLE,
    "organic": KITCHEN,
    "residual": RESIDUAL,
}

#: 上游数据集原始标签 -> 本项目物料类。别名表是唯一的改名入口，
#: `data/split.py` 与任何新数据源都从这里查，不各自写一份。
SOURCE_LABEL_ALIASES: dict[str, str] = {
    # TrashNet（6 类）
    "paper": "paper",
    "cardboard": "cardboard",
    "glass": "glass",
    "metal": "metal",
    "plastic": "plastic",
    "trash": "residual",
    # Roboflow Garbage Classification 3 / Material Identification（7 类）
    "cloth": "textile",
    "clothes": "textile",
    "biodegradable": "organic",
    "organic": "organic",
    "textile": "textile",
    "residual": "residual",
    # TACO（60 个细类，按材质归到八类；歧义的见 AMBIGUOUS_SOURCE_LABELS）
    "aluminium_foil": "metal",
    "aluminium_blister_pack": "metal",
    "other_plastic_bottle": "plastic",
    "clear_plastic_bottle": "plastic",
    "glass_bottle": "glass",
    "plastic_bottle_cap": "plastic",
    "metal_bottle_cap": "metal",
    "broken_glass": "glass",
    "food_can": "metal",
    "aerosol": "metal",
    "drink_can": "metal",
    "toilet_tube": "cardboard",
    "other_carton": "cardboard",
    "egg_carton": "cardboard",
    "drink_carton": "cardboard",
    "corrugated_carton": "cardboard",
    "meal_carton": "cardboard",
    "pizza_box": "cardboard",
    "paper_cup": "paper",
    "disposable_plastic_cup": "plastic",
    "foam_cup": "plastic",
    "glass_cup": "glass",
    "other_plastic_cup": "plastic",
    "food_waste": "organic",
    "glass_jar": "glass",
    "plastic_lid": "plastic",
    "metal_lid": "metal",
    "other_plastic": "plastic",
    "magazine_paper": "paper",
    "tissues": "paper",
    "wrapping_paper": "paper",
    "normal_paper": "paper",
    "paper_bag": "paper",
    "plastified_paper_bag": "paper",
    "plastic_film": "plastic",
    "six_pack_rings": "plastic",
    "garbage_bag": "plastic",
    "other_plastic_wrapper": "plastic",
    "single_use_carrier_bag": "plastic",
    "polypropylene_bag": "plastic",
    "crisp_packet": "plastic",
    "spread_tub": "plastic",
    "tupperware": "plastic",
    "disposable_food_container": "plastic",
    "foam_food_container": "plastic",
    "other_plastic_container": "plastic",
    "plastic_glooves": "plastic",
    "plastic_utensils": "plastic",
    "pop_tab": "metal",
    "rope_&_strings": "textile",
    "scrap_metal": "metal",
    "shoe": "textile",
    "squeezable_tube": "plastic",
    "plastic_straw": "plastic",
    "paper_straw": "paper",
    "styrofoam_piece": "plastic",
    "cigarette": "residual",
}

#: 材质无法从标签判定的上游标签。spec §1「歧义框只训检测」：这些框进
#: 单类检测集（有位置、无材质），不进八类检测集，也不进分类集。
#: - battery：有害垃圾，本轮八类里没有 hazardous，不能塞进 residual；
#: - carded_blister_pack：纸卡 + 塑料泡罩，两种材质一个框；
#: - unlabeled_litter：TACO 自己就没定材质。
AMBIGUOUS_SOURCE_LABELS: frozenset[str] = frozenset({
    "battery",
    "carded_blister_pack",
    "unlabeled_litter",
})


class TaxonomyError(ValueError):
    """类别名或 class_id 不在分类法里。"""


def _label_key(label: str) -> str:
    return label.strip().lower().replace(" ", "_").replace("-", "_")


def normalize_label(label: str, allow_ambiguous: bool = False) -> str | None:
    """把上游数据集标签映射到物料八类。未登记的标签直接报错，不静默丢弃。

    `allow_ambiguous=True` 时，`AMBIGUOUS_SOURCE_LABELS` 里的标签返回
    `None`（调用方按"只训检测"处理）；默认仍然报错，避免分类流程误收。
    """
    key = _label_key(label)
    if key in SOURCE_LABEL_ALIASES:
        return SOURCE_LABEL_ALIASES[key]
    if key in AMBIGUOUS_SOURCE_LABELS:
        if allow_ambiguous:
            return None
        raise TaxonomyError(
            f"ambiguous source label {label!r}; detection-only, "
            f"pass allow_ambiguous=True to accept it")
    raise TaxonomyError(
        f"unknown source label {label!r}; add it to SOURCE_LABEL_ALIASES first")


def class_name(class_id: int) -> str:
    if not 0 <= class_id < NUM_CLASSES:
        raise TaxonomyError(f"class_id out of range: {class_id}")
    return MATERIAL_CLASSES[class_id]


def china_category(material: str | int) -> str:
    """物料类（名或 id）-> 中国四分类之一。"""
    name = class_name(material) if isinstance(material, int) else material
    try:
        return MATERIAL_TO_CHINA[name]
    except KeyError as exc:
        raise TaxonomyError(f"unknown material class: {name!r}") from exc


def china_category_zh(material: str | int) -> str:
    return CHINA_CATEGORY_ZH[china_category(material)]


# ---- logits 后处理 -----------------------------------------------------
def softmax(logits: Sequence[float]) -> list[float]:
    """数值稳定的 softmax。纯 Python，不依赖 numpy，设备端与单测共用一份。"""
    values = [float(v) for v in logits]
    if not values:
        raise TaxonomyError("softmax of an empty vector")
    peak = max(values)
    exps = [math.exp(v - peak) for v in values]
    total = sum(exps)
    return [e / total for e in exps]


def top_k(logits: Sequence[float], k: int = 3,
          classes: Sequence[str] = MATERIAL_CLASSES) -> list[dict]:
    """返回按概率降序的前 k 项。

    每项：`{"rank", "class_id", "class_name", "confidence", "china_category"}`。
    平局按 class_id 升序（确定性：同一份 logits 在任何平台上给同样的顺序）。
    """
    if len(logits) != len(classes):
        raise TaxonomyError(
            f"logits length {len(logits)} != classes length {len(classes)}")
    if k < 1:
        raise TaxonomyError(f"k must be >= 1, got {k}")
    probs = softmax(logits)
    order = sorted(range(len(probs)), key=lambda i: (-probs[i], i))[:k]
    out = []
    for rank, index in enumerate(order):
        name = classes[index]
        out.append({
            "rank": rank,
            "class_id": index,
            "class_name": name,
            "confidence": round(float(probs[index]), 6),
            "china_category": MATERIAL_TO_CHINA.get(name, RESIDUAL),
        })
    return out


def counts_by_china_category(names: Iterable[str]) -> dict[str, int]:
    """统计一批物料类名落到四分类里的分布，缺的档位补 0。"""
    counts = {category: 0 for category in CHINA_CATEGORIES}
    for name in names:
        counts[china_category(name)] += 1
    return counts
