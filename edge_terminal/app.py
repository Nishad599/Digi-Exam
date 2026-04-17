"""
edge_terminal/app.py
Standalone FastAPI application for the offline Edge Gate Terminal.
Loads a .digipack file, verifies students locally, and can sync back to the main server.
"""
import json
import hmac
import hashlib
import os
from dotenv import load_dotenv
load_dotenv()
import httpx
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import db
import verifier

app = FastAPI(title="Digi-Exam Edge Gate Terminal")
templates = Jinja2Templates(directory="templates")

# In-memory package & session state
_package: Optional[dict] = None
_candidates: dict = {}
_exam_name: str = "No Package Loaded"
_exam_id: int = 0
_conductor_name: Optional[str] = None

EDGE_HMAC_SECRET = os.environ.get("EDGE_HMAC_SECRET")
if not EDGE_HMAC_SECRET:
    raise RuntimeError("EDGE_HMAC_SECRET environment variable is missing!")

_device_pin: Optional[str] = None  # Set dynamically from loaded .digipack

# Initialize local database
db.init_db()


from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# ─── Page Routes ───────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def gate_page(request: Request):
    if not _conductor_name:
        return RedirectResponse(url="/login", status_code=303)
        
    return templates.TemplateResponse(request=request, name="gate.html", context={
        "exam_name": _exam_name,
        "package_loaded": _package is not None,
        "conductor_name": _conductor_name,
        "exam_locked": db.is_exam_completed() if _package else False,
    })

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={
        "package_loaded": _package is not None,
    })

@app.post("/login")
async def do_login(
    request: Request,
    conductor_username: str = Form(...),
    pin: str = Form(...)
):
    global _conductor_name
    if not _package:
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": "Please load a .digipack file first before logging in.",
            "package_loaded": False,
        })
    if pin != _device_pin:
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": "Invalid Device PIN",
            "package_loaded": True,
        })
        
    _conductor_name = conductor_username
    return RedirectResponse(url="/", status_code=303)

@app.get("/logout")
async def logout():
    global _conductor_name
    _conductor_name = None
    return RedirectResponse(url="/login", status_code=303)

@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    if not _conductor_name:
        return RedirectResponse(url="/login", status_code=303)
        
    summary = db.get_summary()
    logs = db.get_all_logs()
    return templates.TemplateResponse(request=request, name="sync.html", context={
        "exam_name": _exam_name,
        "exam_id": _exam_id,
        "summary": summary,
        "logs": logs,
        "conductor_name": _conductor_name,
        "exam_locked": db.is_exam_completed() if _package else False,
    })

@app.get("/roster", response_class=HTMLResponse)
async def roster_page(request: Request):
    if not _conductor_name:
        return RedirectResponse(url="/login", status_code=303)
    
    candidates = list(_candidates.values()) if _candidates else []
    return templates.TemplateResponse(request=request, name="roster.html", context={
        "exam_name": _exam_name,
        "package_loaded": _package is not None,
        "conductor_name": _conductor_name,
        "candidates": candidates,
        "total": len(candidates),
    })


# ─── API Routes ────────────────────────────────────────────────

@app.post("/api/load_package")
async def load_package(package_file: UploadFile = File(...)):
    """Load a .digipack file into memory, verify HMAC, and index candidates."""
    global _package, _candidates, _exam_name, _exam_id, _device_pin

    content = await package_file.read()
    try:
        signed = json.loads(content)
    except Exception:
        raise HTTPException(400, "Invalid file: could not parse JSON")

    sig = signed.get("signature", "")
    payload = signed.get("payload", {})
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    expected_sig = hmac.new(EDGE_HMAC_SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        raise HTTPException(403, "Package signature invalid. This file may have been tampered with.")

    _package = payload
    _exam_name = payload.get("exam_name", "Unknown Exam")
    _exam_id = payload.get("exam_id", 0)
    _device_pin = payload.get("device_pin", "1234")
    _candidates = {c["reg_no"]: c for c in payload.get("candidates", [])}

    db.set_db_path(_exam_id)

    return {
        "status": "success",
        "exam_name": _exam_name,
        "exam_id": _exam_id,
        "total_candidates": len(_candidates),
    }

@app.post("/api/unload_package")
async def unload_package():
    """Unload the active package gracefully from memory."""
    global _package, _candidates, _exam_name, _exam_id, _device_pin
    _package = None
    _candidates = {}
    _exam_name = "No Package Loaded"
    _exam_id = 0
    _device_pin = None
    return {"status": "success", "message": "Package successfully unloaded."}


@app.post("/api/verify")
async def verify_student(
    registration_no: str = Form(...),
    photo: UploadFile = File(...)
):
    """Verify a student at the gate (fully offline)."""
    if not _candidates:
        return JSONResponse({"status": "error", "message": "No exam package loaded. Please load a .digipack file first."}, status_code=400)

    if db.is_exam_completed():
        return JSONResponse({"status": "error", "message": "EXAM LOCKED: This exam has been marked as completed. Verification is closed."}, status_code=403)

    candidate = _candidates.get(registration_no.strip())
    if not candidate:
        db.insert_log(
            reg_no=registration_no, name="UNKNOWN", session_id=0,
            session_label="", center_name="", status="FAIL", confidence=0.0
        )
        return {"status": "failure", "message": f"Candidate '{registration_no}' not found in this exam package.", "score": 0.0}

    # STRICT DEDUPLICATION: Prevent double check-ins
    if db.check_already_verified(registration_no.strip()):
        return {
            "status": "failure",
            "message": f"ALREADY VERIFIED: Candidate {candidate['name']} is already inside.",
            "score": 100.0,
            "candidate": {
                "name": candidate["name"],
                "center": candidate.get("center_name", "")
            }
        }

    stored_embedding = candidate.get("face_embedding")
    if not stored_embedding:
        db.insert_log(
            reg_no=registration_no, name=candidate["name"], session_id=candidate.get("session_id", 0),
            session_label=candidate.get("session_label", ""), center_name=candidate.get("center_name", ""),
            status="FAIL", confidence=0.0
        )
        return {"status": "failure", "message": f"No biometric data on file for {candidate['name']}. Cannot verify.", "score": 0.0}

    photo_bytes = await photo.read()
    import base64
    image_b64 = base64.b64encode(photo_bytes).decode("utf-8")

    result = verifier.verify_candidate(stored_embedding, image_b64, threshold=0.45)

    status_str = "PASS" if result["match"] else "FAIL"
    ts = db.insert_log(
        reg_no=registration_no,
        name=candidate["name"],
        session_id=candidate.get("session_id", 0),
        session_label=candidate.get("session_label", ""),
        center_name=candidate.get("center_name", ""),
        status=status_str,
        confidence=result["score"]
    )

    if result["error"]:
        return {"status": "failure", "message": result["error"], "score": 0.0}

    if result["match"]:
        return {
            "status": "success",
            "message": f"Welcome, {candidate['name']}! Verification successful.",
            "score": result["score"],
            "candidate": {
                "name": candidate["name"],
                "center": candidate.get("center_name"),
                "session": candidate.get("session_label"),
            }
        }
    else:
        return {
            "status": "failure",
            "message": f"FACE MISMATCH for {candidate['name']} (score: {result['score']:.3f}, need >= 0.45)",
            "score": result["score"]
        }


@app.get("/api/logs")
async def get_logs():
    return db.get_all_logs()


@app.post("/api/upload_logs")
async def upload_logs_to_server(request: Request):
    """Batch upload all local unsynced logs to the main server."""
    body = await request.json()
    server_url = body.get("server_url", "").rstrip("/")
    if not server_url:
        raise HTTPException(400, "server_url is required")

    logs = db.get_all_logs()
    unsynced = [l for l in logs if not l["synced"]]

    if not unsynced:
        return {"status": "success", "message": "No unsynced logs to upload.", "synced": 0}

    # Build signed payload
    log_payload = [
        {"reg_no": l["reg_no"], "status": l["status"], "timestamp": l["timestamp"], "confidence": l["confidence"]}
        for l in unsynced
    ]
    logs_bytes = json.dumps(log_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    sig = hmac.new(EDGE_HMAC_SECRET.encode(), logs_bytes, hashlib.sha256).hexdigest()

    upload_body = {"signature": sig, "logs": log_payload}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{server_url}/api/admin/exam/{_exam_id}/sync_logs",
                json=upload_body
            )
        if resp.status_code == 200:
            db.mark_logs_synced()
            result = resp.json()
            return {"status": "success", "synced": result.get("synced", 0), "skipped": result.get("skipped", 0)}
        else:
            return {"status": "error", "message": f"Server returned {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"status": "error", "message": f"Connection failed: {str(e)}"}

@app.post("/api/lock_exam")
async def lock_exam():
    """Permanently lock the currently loaded offline exam so no further verifications can take place."""
    if not _package:
        raise HTTPException(status_code=400, detail="No exam package loaded.")
    db.mark_exam_completed()
    return {"status": "success", "message": "Exam permanently locked for auditing."}


@app.get("/api/time_offset")
async def get_time_offset():
    """Returns the current time offset and the adjusted terminal time."""
    offset = db.get_time_offset()
    adjusted = db.get_adjusted_now().strftime("%Y-%m-%d %I:%M:%S %p")
    return {"offset_minutes": offset, "adjusted_time": adjusted}


@app.post("/api/time_offset")
async def set_time_offset(request: Request):
    """Set a time offset (in minutes) to correct the edge terminal clock."""
    body = await request.json()
    minutes = int(body.get("offset_minutes", 0))
    db.set_time_offset(minutes)
    adjusted = db.get_adjusted_now().strftime("%Y-%m-%d %I:%M:%S %p")
    return {"status": "success", "offset_minutes": minutes, "adjusted_time": adjusted}

