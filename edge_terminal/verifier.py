"""
edge_terminal/verifier.py
Local biometric face matching engine for the offline Edge Gate Terminal.
Reuses the same InsightFace buffalo_l model as the main server.
"""
import numpy as np
import json
import base64
import cv2

import liveness

_app = None

def _get_app():
    global _app
    if _app is None:
        import insightface
        # Optimize by ONLY loading detection and recognition. Skips gender/age and 3d landmarks.
        _app = insightface.app.FaceAnalysis(
            name="buffalo_l", 
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            allowed_modules=["detection", "recognition"]
        )
        # Reduce detection size for faster CPU inference
        _app.prepare(ctx_id=0, det_size=(320, 320))
    return _app

def get_embedding_from_b64(image_b64: str):
    """Decode a base64 image and extract a face embedding."""
    try:
        img_data = base64.b64decode(image_b64)
        nparr = np.frombuffer(img_data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None, "Could not decode image"
        
        app = _get_app()
        faces = app.get(img)
        if not faces:
            return None, "No face detected in image"
        
        # 1. Check Liveness (Anti-Spoofing) first
        bbox = faces[0].bbox
        liveness_result = liveness.check_liveness(img, bbox)
        if not liveness_result["is_real"]:
            score = liveness_result["score"]
            return None, f"Liveness Check Failed: Spoof Detected! (Score: {score:.2f})"
        
        # 2. Extract Identity Embedding
        embedding = faces[0].embedding
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm
        return embedding.tolist(), None
    except Exception as e:
        return None, str(e)

def cosine_similarity(a, b) -> float:
    """Compute cosine similarity between two embedding vectors."""
    a = np.array(a, dtype=np.float32)
    b = np.array(b, dtype=np.float32)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))

def verify_candidate(stored_embedding_json: str, live_image_b64: str, threshold: float = 0.45):
    """
    Compare a stored embedding (JSON string) against a live image (base64).
    Returns a dict with: match (bool), score (float), error (str|None)
    """
    try:
        stored = json.loads(stored_embedding_json)
    except Exception:
        return {"match": False, "score": 0.0, "error": "Invalid stored embedding"}

    live_emb, err = get_embedding_from_b64(live_image_b64)
    if err:
        return {"match": False, "score": 0.0, "error": err}

    score = cosine_similarity(stored, live_emb)
    return {"match": score >= threshold, "score": round(score, 4), "error": None}
