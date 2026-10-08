"""hand-gesture — MediaPipe 手势识别（reCamera Pro / RV1126B）.

四级级联（MediaPipe Gesture Recognizer 语义，官方测试图与官方
mediapipe 包端到端对齐验证）：

    frame (RGB, cpu 模式全分辨率)
      -> 自管 letterbox 192（黑边，MediaPipe BORDER_ZERO 约定）
      -> hand_detector_fp16.rknn        (NPU: SSD 2016 锚点 + NMS)
      -> 每掌: 旋转角 + shift/2.6x 矩形 -> cv2.warpPerspective 旋转裁剪 224
      -> hand_landmarks_detector_fp16.rknn (NPU: 21 关键点投影回原图)
      -> gesture_embedder + canned_classifier (CPU numpy 精确复刻,
                                               权重在 gesture_heads.npz)
      -> emit 手掌框(pixel xyxy) + 21 归一化关键点 + 手势 + 左右手

embedder/classifier 不上 NPU 的原因：kit 托管推理链路只支持单输入 uint8
图像模型（非 uint8 输入会被强制转型毁掉 0~1 浮点向量）。两个头部是微型
全连接网络（43+4 个算子），numpy 复刻在 RV1126B CPU 上是微秒级，且与
tflite 在主机侧做逐数值等价验证（见 model/gesture/validate_pipeline.py）。
"""
from __future__ import annotations

import os

# kit 的 RGA NV12->RGB 硬件转换在本机 librga v1.10.5_[11] 上曾输出偏蓝
# （U/V 互换，RK_FORMAT 枚举不匹配），已源头修复（设备端 _rga.py 枚举
# 0xE00→0xA00）。若固件升级恢复了旧 kit，帧会再次偏蓝导致检测失效；
# 届时取消下行注释可强制回退到 OpenCV 转换路径。
# os.environ.setdefault("RECAMERA_RGA", "0")

import numpy as np
from kit.app import App, run_app

import gesture_common as G
import gesture_stabilizer as GS

def _bundled_file(name: str) -> str:
    """定位包内数据工件：AppMgr 下 cwd=App 目录优先，否则按入口模块同级。"""
    for cand in (os.path.join(os.getcwd(), name),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), name)):
        if os.path.exists(cand):
            return cand
    return name


DET_ID = "hand_detector_fp16"
LM_ID = "hand_landmarks_detector_fp16"

# 可调参数的声明默认值（manifest config_schema 才是权威；这里仅为手跑兜底）
DEFAULTS = dict(confidence=0.5, nms=0.3, max_hands=2, min_presence=0.5,
                gesture_min_conf=0.5, roi_shift_y=-0.55, roi_scale=2.5,
                vote_window=5, vote_min_votes=0)


def gesture_event(r: dict) -> dict:
    """一行结果 -> 一个扁平 gesture 事件（kit.events 机械映射风格）。"""
    return {
        "kind": "gesture",
        "gesture": r.get("gesture"),
        "gesture_conf": r.get("gesture_conf"),
        "hand": r.get("hand"),
        "score": r.get("score"),
        "box": r.get("box"),
        "keypoints": r.get("keypoints"),
        "gesture_raw": r.get("gesture_raw"),
        "vote_count": r.get("vote_count"),
    }


class HandGesture(App):
    owns_loop = True
    # cpu 模式：frame.data 为全分辨率 RGB，供旋转 warp 取像素；
    # 检测器输入在 app 内自管 letterbox（黑边对齐 MediaPipe 约定）。
    model_frame = "cpu"

    def setup(self, config):
        super().setup(config)
        for k, v in DEFAULTS.items():
            if getattr(self, k, None) is None:
                setattr(self, k, v)
        self._det = G.PalmDetector(self.confidence, self.nms, self.max_hands)
        self._heads = G.GestureHeads(_bundled_file("gesture_heads.npz"))
        self._stab = GS.GestureStabilizer(self.vote_window,
                                          self.vote_min_votes)
        self._fn = 0
        print(f"[hand-gesture] setup conf={self.confidence} nms={self.nms} "
              f"max_hands={self.max_hands} min_presence={self.min_presence} "
              f"gesture_min_conf={self.gesture_min_conf} "
              f"vote_window={self.vote_window} "
              f"vote_min_votes={self.vote_min_votes}", flush=True)

    def on_params_changed(self, changed):
        # 阈值类变更即时重建后处理器（无状态对象，代价可忽略）
        if changed & {"confidence", "nms", "max_hands"}:
            self._det = G.PalmDetector(self.confidence, self.nms,
                                       self.max_hands)
        if changed & {"vote_window", "vote_min_votes"}:
            self._stab = GS.GestureStabilizer(self.vote_window,
                                              self.vote_min_votes)

    def run(self):
        for frame in self.frames():
            rgb = frame.data                      # 全分辨率 RGB uint8
            H, W = rgb.shape[:2]

            lb, s, pl, pt = G.letterbox_rgb(rgb, G.DET_SIZE)
            outs = self.models[DET_ID].infer(lb)
            palms = self._det.postprocess(outs, W, H, s, pl, pt)

            rows = []
            for p in palms[:self.max_hands]:
                rot = G.compute_rotation(p["kpts"], W, H)
                rect = G.transform_rect(p["bbox"], rot, W, H,
                                         shift_y=self.roi_shift_y,
                                         scale_factor=self.roi_scale)
                roi = G.warp_rotated_roi(rgb, rect)
                louts = self.models[LM_ID].infer(roi)
                pts, _world, handed, flag = G.landmark_postprocess(
                    louts, rect, W, H)
                if flag < self.min_presence:
                    continue
                label, conf, probs = self._heads.classify_landmarks(
                    pts, image_width=W, image_height=H)
                if conf < self.gesture_min_conf:
                    label, conf = "None", float(conf)
                # 注意：模型按自拍镜像约定输出 handedness，非镜像相机下取反
                hand = "Left" if handed >= 0.5 else "Right"
                # 用 21 关键点的包围盒代替检测器的紧手掌框：指尖超出掌框，
                # 若用掌框，框顶标签会被指尖关键点遮挡（前端 pose 层在 detect 层之上）
                kxs = pts[:, 0] * W
                kys = pts[:, 1] * H
                margin = 8.0
                bx1 = float(np.clip(kxs.min() - margin, 0, W))
                by1 = float(np.clip(kys.min() - margin, 0, H))
                bx2 = float(np.clip(kxs.max() + margin, 0, W))
                by2 = float(np.clip(kys.max() + margin, 0, H))
                rows.append({
                    "kind": "hand",
                    "box": [round(bx1, 1), round(by1, 1),
                            round(bx2, 1), round(by2, 1)],
                    "score": round(p["score"], 4),
                    # 使用前端通用标签字段，避免自定义 gesture 字段在
                    # 某些 overlay 归一化路径中只剩 score 的兼容问题。
                    "gesture": label,
                    "label": label,
                    "class_name": label,
                    "gesture_conf": round(conf, 4),
                    "hand": hand,
                    "presence": round(flag, 4),
                    "keypoints": [[round(float(x), 4), round(float(y), 4)]
                                  for x, y, _z in pts],
                    # 前端渲染器按 spaces[字段名] 判定坐标系；缺了不画点
                    "spaces": {"box": "pixel_xyxy",
                               "keypoints": "normalized_points"},
                })
            rows = self._stab.update(rows, self._fn, W, H)
            self._fn += 1
            self.emit([gesture_event(r) for r in rows], frame.pts,
                      results=rows)


if __name__ == "__main__":
    run_app(HandGesture())
