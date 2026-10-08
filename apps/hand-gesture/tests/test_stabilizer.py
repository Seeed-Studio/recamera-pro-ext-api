import unittest

from gesture_stabilizer import GestureStabilizer

W, H = 1280, 720


def row(gesture, conf=0.9, box=(100.0, 100.0, 200.0, 200.0)):
    return {"kind": "hand", "box": list(box),
            "gesture": gesture, "label": gesture, "class_name": gesture,
            "gesture_conf": conf}


class StabilizerTest(unittest.TestCase):
    def test_majority_vote_smooths_single_frame_flicker(self):
        """window=5/min=3：中间两帧 None 不应打断稳定的 Open_Palm。"""
        st = GestureStabilizer(window=5, min_votes=3)
        seq = ["Open_Palm", "Open_Palm", "None", "Open_Palm", "None"]
        outs = [st.update([row(g)], i, W, H)[0]["gesture"]
                for i, g in enumerate(seq)]
        # f1:[O]1<3 None  f2:[O,O]2<3 None  f3:[O,O,N]2<3 None
        # f4:[O,O,N,O]3>=3 Open  f5:[O,O,N,O,N] O3>=3 Open（当前帧 None 被平滑）
        self.assertEqual(outs,
                         ["None", "None", "None", "Open_Palm", "Open_Palm"])

    def test_label_switch_requires_majority(self):
        """稳定 Open_Palm 后，Victory 需占多数（3/5）才切换。"""
        st = GestureStabilizer(window=5, min_votes=3)
        for i in range(5):
            st.update([row("Open_Palm")], i, W, H)
        outs = [st.update([row("Victory")], 5 + i, W, H)[0]["gesture"]
                for i in range(4)]
        # f5:[O4,V1]→O  f6:[O3,V2]→O  f7:[O2,V3]→V(多数)  f8:[O1,V4]→V
        self.assertEqual(outs,
                         ["Open_Palm", "Open_Palm", "Victory", "Victory"])

    def test_first_frames_output_none_until_majority(self):
        """启动时未积累到多数票前输出 None，避免开机误报。"""
        st = GestureStabilizer(window=5, min_votes=3)
        outs = [st.update([row("Victory")], i, W, H)[0]["gesture"]
                for i in range(3)]
        self.assertEqual(outs, ["None", "None", "Victory"])

    def test_passthrough_when_window_one(self):
        """window=1 等价关闭投票：逐帧直通。"""
        st = GestureStabilizer(window=1)
        seq = ["None", "Open_Palm", "Victory"]
        outs = [st.update([row(g)], i, W, H)[0]["gesture"]
                for i, g in enumerate(seq)]
        self.assertEqual(outs, seq)

    def test_separate_tracks_vote_independently(self):
        """空间上分开的两只手各自维护投票窗口。"""
        st = GestureStabilizer(window=3, min_votes=2)
        left, right = (100.0, 100.0, 200.0, 200.0), (600.0, 100.0, 700.0, 200.0)
        for i in range(3):
            rows = st.update([row("None", box=left),
                              row("Open_Palm", box=right)], i, W, H)
        self.assertEqual(rows[0]["gesture"], "None")
        self.assertEqual(rows[1]["gesture"], "Open_Palm")

    def test_stale_track_expires_and_restarts(self):
        """手消失超过 timeout 后投票历史作废，重新出现需重新积累。"""
        st = GestureStabilizer(window=3, min_votes=2, timeout=3)
        st.update([row("Open_Palm")], 0, W, H)
        out = st.update([row("Open_Palm")], 1, W, H)[0]["gesture"]
        self.assertEqual(out, "Open_Palm")          # [O,O] 2>=2
        for i in range(2, 8):                        # 空手 6 帧 > timeout=3
            st.update([], i, W, H)
        out = st.update([row("Open_Palm")], 8, W, H)[0]["gesture"]
        self.assertEqual(out, "None")                # 旧 track 已删，重新积累

    def test_gesture_raw_keeps_current_frame_label(self):
        """稳定输出覆盖 gesture，但 gesture_raw 保留当前帧原始标签供调试。"""
        st = GestureStabilizer(window=5, min_votes=3)
        for i in range(5):
            st.update([row("Open_Palm")], i, W, H)
        r = st.update([row("None", conf=0.6)], 5, W, H)[0]
        self.assertEqual(r["gesture"], "Open_Palm")
        self.assertEqual(r["gesture_raw"], "None")
        self.assertEqual(r["label"], "Open_Palm")


if __name__ == "__main__":
    unittest.main()
