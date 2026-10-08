import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gesture_common as G


# 来自设备当前清晰 Victory 帧的 FP16 21 点输出。坐标是原图 1280x720
# 的 normalized image coordinates；未做宽高比归一化时旧实现判 None。
VICTORY_LANDMARKS = np.array([
    [0.4644, 0.8663, 0.0000], [0.4709, 0.8433, -0.0473],
    [0.4610, 0.8007, -0.0632], [0.4359, 0.7690, -0.0712],
    [0.4118, 0.7405, -0.0773], [0.4690, 0.7279, -0.0315],
    [0.4695, 0.6686, -0.0487], [0.4690, 0.6300, -0.0612],
    [0.4697, 0.5936, -0.0700], [0.4476, 0.7356, -0.0162],
    [0.4373, 0.6761, -0.0389], [0.4280, 0.6424, -0.0583],
    [0.4227, 0.6107, -0.0696], [0.4296, 0.7579, -0.0055],
    [0.4078, 0.7396, -0.0441], [0.4110, 0.7848, -0.0651],
    [0.4186, 0.8196, -0.0701], [0.4140, 0.7831, 0.0032],
    [0.3991, 0.7782, -0.0338], [0.4053, 0.8121, -0.0501],
    [0.4144, 0.8410, -0.0555],
], dtype=np.float32)


class GestureAspectRatioTest(unittest.TestCase):
    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.heads = G.GestureHeads(os.path.join(here, "gesture_heads.npz"))

    def test_wide_frame_victory_uses_aspect_normalized_landmarks(self):
        """缺少 1280:720 宽高比修正时，此真实 Victory 会错误输出 None。"""
        classify = getattr(self.heads, "classify_landmarks", None)
        self.assertIsNotNone(
            classify,
            "GestureHeads must classify image landmarks with frame aspect ratio",
        )
        label, confidence, probs = classify(
            VICTORY_LANDMARKS, image_width=1280, image_height=720
        )
        self.assertEqual("Victory", label)
        self.assertGreater(float(confidence), float(probs[G.GESTURES.index("None")]))


if __name__ == "__main__":
    unittest.main()
