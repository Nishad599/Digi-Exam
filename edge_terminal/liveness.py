"""
edge_terminal/liveness.py
Passive Liveness Detection (Anti-Spoofing) via ONNX Runtime using MiniFASNet
"""
import os
import urllib.request
import cv2
import numpy as np
import onnxruntime

# MiniFASNetV2 — 3-class output [Spoof, Real, Spoof], input 80×80
MODEL_URL = "https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV2.onnx"
MODEL_PATH = os.path.join(os.path.dirname(__file__), ".models", "MiniFASNetV2.onnx")

INPUT_SIZE = (80, 80)   # ← change from (80, 80)
SCALE = 2.7               # ← keep as is
_session = None

def _get_session():
    global _session
    if _session is None:
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        if not os.path.exists(MODEL_PATH):
            print("[Liveness] Downloading MiniFASNetV2 Anti-Spoofing model...")
            urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
            print("[Liveness] Download complete.")
        _session = onnxruntime.InferenceSession(
            MODEL_PATH,
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
        )
    return _session

def get_crop_bbox(bbox, img_w, img_h, scale=2.7):
    """Expanded crop bounding box — scale must match the model's training scale."""
    x1, y1, x2, y2 = bbox
    w, h = x2 - x1, y2 - y1
    cx, cy = x1 + w // 2, y1 + h // 2
    side = int(max(w, h) * scale) / 2
    new_x1 = max(0, int(cx - side))
    new_y1 = max(0, int(cy - side))
    new_x2 = min(img_w, int(cx + side))
    new_y2 = min(img_h, int(cy + side))
    return new_x1, new_y1, new_x2, new_y2

def check_liveness(img, bbox, threshold=0.85) -> dict:
    """
    Check if a face is Real or Spoof.
    Returns: {"is_real": bool, "score": float}
    """
    try:
        session = _get_session()
        h_img, w_img, _ = img.shape

        x1, y1, x2, y2 = get_crop_bbox(bbox, w_img, h_img, scale=SCALE)
        face_crop = img[y1:y2, x1:x2]

        if face_crop.size == 0:
            return {"is_real": False, "score": 0.0}

        # Preprocessing: BGR→RGB, resize to model input, NCHW, float32
        face_img = cv2.resize(face_crop, INPUT_SIZE)
        face_img = cv2.cvtColor(face_img, cv2.COLOR_BGR2RGB)
        input_data = np.transpose(face_img, (2, 0, 1)).astype(np.float32)
        input_data = np.expand_dims(input_data, axis=0)  # [1, 3, 80, 80]

        # Inference
        input_name = session.get_inputs()[0].name
        outs = session.run(None, {input_name: input_data})

        # Output: [1, 3] — layout is [Spoof, Real, Spoof]
        logits = outs[0][0]
        exp_logits = np.exp(logits - np.max(logits))
        probs = exp_logits / np.sum(exp_logits)

        real_score = float(probs[1])  # index 1 = "Real"

        return {
            "is_real": real_score >= threshold,
            "score": real_score
        }
    except Exception as e:
        print(f"[Liveness] Error: {e}")
        return {"is_real": False, "score": 0.0}  # Fail closed (secure default)