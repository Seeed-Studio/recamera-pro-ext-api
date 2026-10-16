# Modbus TCP 契约 v2（Demo B：装配缺件 + 尺寸）

服务端：pymodbus，TCP，**unit id = 1**。
每次判定的写入顺序固定：**先原子更新保持寄存器，再写线圈**。
消费方读到线圈翻转时，寄存器内容已经是同一次判定的数据。

## 寄存器表

| 类型/地址 | 含义 |
|---|---|
| Coil 0 | NG（与 Coil 1 互斥） |
| Coil 1 | OK（与 Coil 0 互斥） |
| HR 0 | 主缺陷类别 ID（本帧最高分框的类别；无缺陷时 0） |
| HR 1 | 缺陷数 |
| HR 2 | bbox cx，归一化 ×10000 |
| HR 3 | bbox cy，归一化 ×10000 |
| HR 4 | bbox w，归一化 ×10000 |
| HR 5 | bbox h，归一化 ×10000 |
| HR 6 | 心跳 Unix 秒，uint32 高字 |
| HR 7 | 心跳 Unix 秒，uint32 低字 |
| HR 8 | 缺件数（`assembly.missing` 的长度） |
| HR 9 | 多余件数（`assembly.extra` 的长度） |
| HR 10 | 主尺寸测量值（毫米 ×100，长边） |
| HR 11 | 公差判定码：0 ok / 1 undersize / 2 oversize / 3 not_found / 4 uncalibrated |

HR 0–7 与 Demo A（v1）逐位相同，v2 只在尾部追加 HR 8–11。只读 HR 0–7 的
既有 PLC 程序不受影响。

## 编码规则

- HR 全部为 16 位无符号，范围 [0, 65535]。
- 归一化坐标编码：`round(v * 10000)`，先把 v 夹到 [0,1]，因此 HR 2–5 ∈ [0,10000]。
- HR 2–5 取主缺陷框（最高分框）。无缺陷（OK）时 HR 0–5 全部写 0。
- 心跳：`ts = int(time.time())`，`HR6 = (ts >> 16) & 0xFFFF`，`HR7 = ts & 0xFFFF`。
  心跳按 `modbus.heartbeat_interval_s` 独立刷新，不依赖是否有新判定。
- Coil：NG 时 `(Coil0, Coil1) = (1, 0)`；OK 时 `(0, 1)`。二者永远不同时为 1。
  v2 的 NG 可以由**缺陷、缺件、多余件、尺寸超差**中的任一条触发
  （`rules.ng_on_*` 可逐条关闭），线圈本身不区分原因；原因在 MQTT 的
  `verdict_reasons` 里，Modbus 侧靠 HR 1 / 8 / 9 / 11 分辨。
- HR 8/9 越界同样饱和到 65535。
- HR 10 的毫米值 `round(mm * 100)`，负值写 0，上限 655.35 mm。取的是
  `DimensionResult.primary`——**有不合格项时取第一个不合格项，否则取第一条测量**，
  与 HR 11 永远来自同一条测量。
- 没配尺寸模块、或标定失败时：HR 10 = 0，HR 11 = 4（uncalibrated）。
  测量项找不到目标轮廓时：HR 10 = 0，HR 11 = 3（not_found）。
  **HR 10 = 0 不代表"量到 0 mm"**，必须先读 HR 11 才能解释 HR 10。

## 判定码与 MQTT 的对应

| HR 11 | `dimension.measurements[].status` | 含义 |
|---|---|---|
| 0 | `ok` | 在公差带内 |
| 1 | `undersize` | 偏差为负且超出公差 |
| 2 | `oversize` | 偏差为正且超出公差 |
| 3 | `not_found` | ROI 里没找到够大的轮廓 |
| 4 | `uncalibrated` | 标定物没找到 / 没配标定段 |

参考实现与单测：`core-py/core_inspection/modbus_map.py`、`tests/test_modbus_map.py`。
