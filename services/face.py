"""
Face Service — InsightFace (buffalo_l) backend
Drop-in replacement: get_face_embedding(), compute_similarity(), match_faces()
"""

import os
import json
import numpy as np
import cv2

# ── Load InsightFace once at import time ──────────────────────────────
try:
    from insightface.app import FaceAnalysis

    _app = FaceAnalysis(
        name="buffalo_l",
        providers=["CPUExecutionProvider"],   # safe everywhere; swap to CUDAExecutionProvider if GPU available
    )
    _app.prepare(ctx_id=0, det_size=(640, 640))
    INSIGHTFACE_AVAILABLE = True
    print("[FaceService] InsightFace (buffalo_l / ArcFace) loaded successfully.")
except ImportError:
    INSIGHTFACE_AVAILABLE = False
    print("WARNING: 'insightface' not found. Using simulated face embeddings.")
except Exception as e:
    INSIGHTFACE_AVAILABLE = False
    print(f"WARNING: InsightFace failed to initialize: {e}. Using simulated face embeddings.")


# ── Public API (same signatures as the DeepFace version) ─────────────

def get_face_embedding(file_bytes: bytes) -> list[float]:
    """Return a 512-dim ArcFace embedding from raw image bytes.
    Returns [] if no face is detected.
    """
    if not INSIGHTFACE_AVAILABLE:
        # deterministic mock so the rest of the app still works
        mock = np.random.RandomState(len(file_bytes) % 10000).uniform(-1.0, 1.0, 512)
        return mock.tolist()

    try:
        # Decode image bytes → BGR numpy array (OpenCV format)
        np_arr = np.frombuffer(file_bytes, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

        if img is None:
            print("[FaceService] Could not decode image bytes.")
            return []

        faces = _app.get(img)

        if not faces:
            return []

        # Pick the largest face (by bounding-box area) if multiple detected
        best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        embedding = best.embedding  # numpy array, 512-dim

        # Normalize to unit vector (ArcFace embeddings are already near-unit, but be safe)
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        return embedding.tolist()

    except Exception as e:
        print(f"[FaceService] Face extraction failed: {e}")
        return []


def compute_similarity(emb1: list, emb2: list) -> float:
    """Cosine similarity between two embedding vectors."""
    e1, e2 = np.array(emb1), np.array(emb2)
    if e1.size == 0 or e2.size == 0:
        return 0.0
    dot = np.dot(e1, e2)
    n1 = np.linalg.norm(e1)
    n2 = np.linalg.norm(e2)
    return float(dot / (n1 * n2 + 1e-10))


def match_faces(stored_embedding_json: str, incoming_file_bytes: bytes, threshold: float = 0.45) -> dict:
    """Compare a live photo against a stored embedding.

    NOTE: ArcFace cosine similarity scale is different from Facenet512.
        Same-person pairs typically score 0.3–0.7+.
        A threshold of 0.45 is a good starting point (tune as needed).
    """
    if not stored_embedding_json:
        return {"match": False, "score": 0.0, "error": "No stored embedding found."}

    try:
        stored_emb = json.loads(stored_embedding_json)
        incoming_emb = get_face_embedding(incoming_file_bytes)

        if len(incoming_emb) == 0:
            return {"match": False, "score": 0.0, "error": "No face found in incoming photo."}

        cosine_sim = compute_similarity(stored_emb, incoming_emb)
        is_match = bool(cosine_sim > threshold)

        return {"match": is_match, "score": float(cosine_sim), "error": None}
    except Exception as e:
        print(f"[FaceService] Match error: {e}")
        return {"match": False, "score": 0.0, "error": str(e)}