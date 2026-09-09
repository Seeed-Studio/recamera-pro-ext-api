"""fp32 onnxruntime reference for the reCamera Pro face-recognition pipeline.

Runs the SAME scrfd.py / align.py code the device runs, but with the fp32 ONNX
models, so a cosine between this embedding and the device's isolates the
RV1126B fp16 NPU error. Usage:
    uv run --with onnxruntime --with opencv-python-headless --with numpy \
        python fp32_ref.py <image> [--make-probe <src112> <out>]
"""
import sys, types, json, base64
from pathlib import Path
import numpy as np, cv2
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import scrfd, align                      # noqa: E402
import onnxruntime as ort                # noqa: E402
CONV = HERE.parent / "models" / "convert"

def make_probe(src112, out, canvas=640, face_h=300):
    im = cv2.imread(str(src112)); f = cv2.resize(im, (face_h, face_h), interpolation=cv2.INTER_CUBIC)
    c = np.full((canvas, canvas, 3), 128, np.uint8); o = (canvas - face_h) // 2
    c[o:o+face_h, o:o+face_h] = f; cv2.imwrite(str(out), c); return out

def letterbox(rgb, size=640):
    h, w = rgb.shape[:2]; s = min(size / w, size / h); nw, nh = int(round(w*s)), int(round(h*s))
    r = cv2.resize(rgb, (nw, nh)); c = np.full((size, size, 3), 128, np.uint8)
    pw, ph = (size - nw) // 2, (size - nh) // 2; c[ph:ph+nh, pw:pw+nw] = r
    return c, types.SimpleNamespace(scale=s, pad_w=pw, pad_h=ph, orig_w=w, orig_h=h)

def main(img):
    bgr = cv2.imread(str(img)); rgb = bgr[..., ::-1].copy()
    lb, info = letterbox(rgb)
    det = ort.InferenceSession(str(CONV / "det_500m_640.onnx"), providers=["CPUExecutionProvider"])
    x = ((lb.astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None]
    outs = det.run(None, {det.get_inputs()[0].name: x})
    faces = scrfd.decode(outs, info, conf_thres=0.5, iou_thres=0.4, model_size=640)
    if not faces: print("NO FACE"); sys.exit(2)
    f = faces[0]; chip = align.align_face(rgb, f["kps"])
    emb_sess = ort.InferenceSession(str(CONV / "arcface_mbf_static.onnx"), providers=["CPUExecutionProvider"])
    e = ((chip.astype(np.float32) - 127.5) / 127.5).transpose(2, 0, 1)[None]
    v = emb_sess.run(None, {emb_sess.get_inputs()[0].name: e})[0].reshape(-1)
    v = v / np.linalg.norm(v)
    print("faces:", len(faces), "best box:", [round(t, 1) for t in f["box"]], "score:", round(f["score"], 3))
    print("emb[:8]:", np.round(v[:8], 4).tolist())
    np.save(str(Path(img).with_suffix(".fp32.npy")), v)
    with open(Path(img).with_suffix(".b64.json"), "w") as fh:
        json.dump({"op": "enroll", "name": "probe003301", "source": "image",
                   "image_b64": base64.b64encode(open(img, "rb").read()).decode()}, fh)

if __name__ == "__main__":
    if sys.argv[1] == "--make-probe": make_probe(sys.argv[2], sys.argv[3]); print("probe", sys.argv[3])
    else: main(sys.argv[1])
