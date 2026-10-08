import os
import unittest


class RgaGuardTest(unittest.TestCase):
    """设备摄像头 NV12 帧经 kit 的 RGA 路径转换后偏蓝（U/V 互换），
    App 必须在 import kit 前设置 RECAMERA_RGA=0 强制 OpenCV 转换路径。"""

    def test_app_rga_guard_is_documented_escape_hatch(self):
        """源头修复后 RGA 默认启用；防御开关以注释形式保留，且必须位于
        import kit 之前的位置（取消注释即可生效）。"""
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")).read()
        guard = src.find('RECAMERA_RGA')
        kit_pos = src.find("from kit.app import")
        self.assertNotEqual(guard, -1, "app.py 缺少 RECAMERA_RGA 防御说明")
        self.assertNotEqual(kit_pos, -1)
        self.assertLess(guard, kit_pos, "防御开关必须位于 kit import 之前")

    def test_rga_constructor_refuses_when_disabled(self):
        """kit._rga 的契约：RECAMERA_RGA=0 时构造即抛异常 → 上层 latch 到 OpenCV。"""
        os.environ["RECAMERA_RGA"] = "0"
        try:
            from kit.adapters._rga import RgaNV12ToRGB
        except ImportError:
            self.skipTest("kit not importable on this host")
        with self.assertRaises(RuntimeError):
            RgaNV12ToRGB()


if __name__ == "__main__":
    unittest.main()
