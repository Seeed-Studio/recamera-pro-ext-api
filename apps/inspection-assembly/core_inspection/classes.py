"""数据集类别表。顺序即 class_id，导出 ONNX、训练 exp、后处理三处必须一致。

DeepPCB 的标注文件第 5 列是 1..6 的缺陷类型码，官方 README 定义为
open / short / mousebite / spur / copper / pin-hole。这里保持**数据集原生顺序**
（不按字母排序），class_id = 类型码 - 1。
"""

DEEPPCB6_CLASSES: tuple[str, ...] = (
    "open",        # 0  (DeepPCB type 1)
    "short",       # 1  (DeepPCB type 2)
    "mousebite",   # 2  (DeepPCB type 3)
    "spur",        # 3  (DeepPCB type 4)
    "copper",      # 4  (DeepPCB type 5)
    "pin-hole",    # 5  (DeepPCB type 6)
)

DEFAULT_CLASSES = DEEPPCB6_CLASSES
