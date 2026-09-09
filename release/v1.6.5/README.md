# reCamera Pro v1.6.5

本版改两处：**OCR（ppocr-reader）识别质量**，以及**发布打包的两个缺陷**。
固件（`rkipc` / `entry.cgi` / `.so`）、appmgr、前端一行未动 —— 三者与 v1.6.4 逐字节相同。

---

## 一、本版改了什么

### 1. ppocr-reader 0.1.0 → 0.1.1：过去它一个文本框都检不出

`db_ocr.decode` 的 `min_size` 默认 **8.0**，作用在**未 unclip 的原始 DB 轮廓**上。
DB 用 shrink 标签训练，输出 blob 只有真实文字高度的约 40%：1280×720 letterbox 到
480（scale 0.375）时，48px 的字落成约 `48 × 0.375 × 0.4 ≈ 7px` 的 blob，卡在 8.0 之下。
而且比的是**短边**，实际门槛是原图字高 53px 以上。

真机实测（RV1126B，6 档字号测试图，2026-09-04）：

| 原图字号 | DB blob 短边（480 空间） | min_size=8.0 |
|---|---|---|
| 48px | 6.00 | 丢弃 |
| 36px | 6.00 | 丢弃 |
| 28px / 22px / 16px / 12px | 5.00 | 丢弃 |

**8 个轮廓，0 个通过。识别器从头到尾没拿到过一个裁剪。**
改回上游 PaddleOCR 的默认值 3 之后，同一张图 **8/8 检出，平均字符相似度 94.8%**，
12px 小字也能读。该参数原先写死在函数签名里、应用从不传，用户在应用中心怎么调都绕不过去；
现已提到 `config_schema`（`apply:"live"`）。

### 2. ppocr-reader：过宽的文本行现在能读了

rec 模型固定 48×320，宽高比超 `320/48 = 6.67:1` 就被横向压缩，CTC 又只有 T=40 步
（约 20 字符）。上游 PaddleOCR 按 batch 动态放宽 `imgW`，定尺寸的 rknn 做不到。
实测一张宽高比递增的对照图：

| 宽高比 | 改前 | 改后 |
|---|---|---|
| 2.60 / 6.98 / 10.15 | 100% | 100% |
| 16.40 | 70.2% | 100% |
| 20.19（英文长句） | **7.1%** | 100% |
| 15.06（40 位数字） | **4.9%** | 100% |
| 22.90 | 97.1% | 100% |
| **平均** | **68.5%** | **99.7%** |

做法是切成重叠窗口分别识别，再**按位置合并**——CTC 的时间步本身就是位置。
装不下的行才切；能装下的走原来的单次推理路径，短行开销不变。
实现在应用自带的 `striptext.py`（**不在 kit**：应用包不含 kit 运行时，
放 kit 里就无法通过应用中心送达）。

### 3. 打包缺陷：应用的同级模块会被漏掉

`release/deploy/build-packages.py` 的 `APP_INCLUDE_TOP` 只收
`manifest.json / app.py / README.md / models`，**不收 `app.py` 旁边的其他模块**。
`ppocr-reader/striptext.py`、`crayfish-fight/logic.py` 都会被漏掉，装上去 `import` 即崩。
应用中心的打包器（`market/packaging/build.py`）本来就收「任何同级 helper .py」，
两个打包器规则不一致。已对齐。

### 4. 打包缺陷：发布清单的哨兵失效

`test_there_are_still_nine_apps` 硬编码「9 个」，第 10 个应用提交后即失真；
而且它会去 import `apps/` 下的**每一个**目录，被未发布的在建应用（缺三方依赖）
拖挂整个测试套件。现改为以 `build-packages.py:APPS` 为准，并新增 `UNSHIPPED_APPS`
显式登记在建应用——新增应用必须做一次有意识的发布决定，否则测试报警。

### 5. crayfish-fight 首次随发布提供

版本号补正为 0.1.2（代码本就与独立项目的 v0.1.2 一致，只是 manifest 漏 bump）。
应用中心包已带齐四个 `.rknn` 权重。

---

## 二、升级顺序

```bash
cd release/v1.6.5
./deploy-app.sh --host <设备IP>
```

**固件层不必动**：`recamera-ext-api-v1.6.5.tar` 里的 `rkipc` / `entry.cgi` / `.so`
与 v1.6.0 起逐字节相同，装过遮罩固件的设备无需重装。

**OCR 修复不依赖 kit 升级**：ppocr-reader 0.1.1 自带 `striptext.py`，
`min_size` 由应用显式传入，旧 kit 也接受该关键字。用户在应用中心更新应用即可。

---

## 三、与 v1.6.4 的产物差异

| 产物 | 与 v1.6.4 | 说明 |
|---|---|---|
| `apps-v1.6.5.tar.gz` | **不同** | ppocr 修复 + 同级模块入包 + 新增 crayfish-fight（10 个应用） |
| `recamera-ext-kit-v1.6.5.tar.gz` | **不同** | `__version__` 1.6.4 → 1.6.5；`db_ocr` 的 `min_size` 默认值 |
| `appmgr-v1.6.5.tar.gz` | **相同**（md5 `8bcb2c54…`） | 未改，确定性重建逐字节一致 |
| `frontend-v1.6.5.tar.gz` | **相同**（md5 `6a487491…`） | 未改 |
| `recamera-ext-api-v1.6.5.tar` | 不同 | 仅包内 MANIFEST/README 的版本与日期变；固件产物三件逐字节同 v1.6.0 |
| `deploy-app.sh` / `deploy-firmware.sh` | 不同 | 仅 `VER=1.6.5` |
| `S94appmgr` / `ext_appmgr.conf` / 两个运行时 | **相同** | 未改 |

---

## 四、应用中心（catalog）

本版随发布更新 catalog，**10 个应用**。相对线上目录只有两项变化：

| 应用 | 变化 |
|---|---|
| `ppocr-reader` | 0.1.0 → **0.1.1** |
| `crayfish-fight` | 新增 **0.1.2** |

其余 8 个应用的包 sha256 与线上逐字节一致，无需重传。

---

## 五、产物 md5

本表是本版所有产物校验值的**唯一权威来源**。

| 包 | size (bytes) | md5 |
|----|-------------:|-----|
| `recamera-ext-api-v1.6.5.tar` | 18708480 | `cf8d09b16e6161cc83b02d592cc44822` |
| `recamera-ext-kit-v1.6.5.tar.gz` | 2248889 | `49297f3cdc27890b982a46144591621f` |
| `appmgr-v1.6.5.tar.gz` | 68964 | `8bcb2c54207b086c6797ddf8316b260c` |
| `apps-v1.6.5.tar.gz` | 1032454 | `2690e3d993558f99e3df823c00687e2e` |
| `frontend-v1.6.5.tar.gz` | 36757262 | `6a48749108747c6bed59a06fef55965e` |
| `voice-runtime-1.0.0.tar.gz` | 18856604 | `ace48a688d41a3fc6b852a0f14ddad8d` |
| `gst-hwcodec-1.0.0.tar.gz` | 425137 | `8e6d286fac58a5b366e8fdd1709b212f` |
| `deploy-app.sh` | 21613 | `26e8a1e68f231334e6b659f78994205d` |
| `deploy-firmware.sh` | 5469 | `c3d881e552bc2689cee342aea8b756d3` |
| `S94appmgr` | 8116 | `e49fcf81c715e827daeed10475f0a5b4` |
| `ext_appmgr.conf` | 4849 | `c5e0131966b85bfce8e614afd0a55577` |

`recamera-ext-api-v1.6.5.tar` 内附固件产物（与 v1.6.0 起相同）：

| 固件产物 | size (bytes) | md5 |
|----------|-------------:|-----|
| `rkipc` | 15585904 | `f683352a9d062a05a3df1f8df22d7d53` |
| `entry.cgi` | 1057168 | `75a693c87c317a49c37c4dddb6b9ac7a` |
| `librecamera_ext.so.1.0.0` | 89496 | `5cebfb9e4d9c001c45b58c75daafe934` |

应用中心包：

| 包 | size (bytes) | md5 |
|----|-------------:|-----|
| `ppocr-reader-0.1.1-arm64.tar.gz` | 5199297 | `f2c53a27667dad2f75b51376c1293cec` |
| `crayfish-fight-0.1.2-arm64.tar.gz` | 7281468 | `083a5bddce89ce2340a7116d6508c68a` |

---

## 六、未随本版发布

`apps/` 下有两个在建应用，已在 `build-packages.py:UNSHIPPED_APPS` 显式登记，
不进任何发布包：

- **`intrusion-detection`** —— `import paho.mqtt.client`，而 kit 刻意手写 MQTT
  客户端就是为了不依赖 paho（见 `kit/adapters/mqtt_sink.py` 开头），且 manifest
  未把它声明为 runtime。在没装 paho 的设备上必崩。
- **`face-recognition`** —— 未审。
