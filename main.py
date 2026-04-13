from fastapi import FastAPI, Request, Depends, HTTPException, status, Form, File, UploadFile, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import timedelta
from io import BytesIO
import json
import uuid
import os
import csv
import qrcode
import cv2
import numpy as np
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

import os
from dotenv import load_dotenv
load_dotenv()

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

import models
from database import engine, get_db, Base
from services.auth import (
    verify_password, get_password_hash, create_access_token, 
    ACCESS_TOKEN_EXPIRE_MINUTES
)
from dependencies import get_current_user
from services.face import get_face_embedding, match_faces
from services.email_service import send_hall_ticket_email, send_otp_email

app = FastAPI(title="Digi-Exam PoC V2")

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Mount Static and Templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ========= WebSocket Live Dashboard =========

class ConnectionManager:
    """Manages WebSocket connections scoped by exam_id for real-time broadcast."""
    def __init__(self):
        self.active: dict[int, set[WebSocket]] = {}  # {exam_id: {ws1, ws2, ...}}

    async def connect(self, ws: WebSocket, exam_id: int):
        await ws.accept()
        self.active.setdefault(exam_id, set()).add(ws)

    def disconnect(self, ws: WebSocket, exam_id: int):
        if exam_id in self.active:
            self.active[exam_id].discard(ws)
            if not self.active[exam_id]:
                del self.active[exam_id]

    async def broadcast(self, exam_id: int, message: dict):
        """Send a JSON message to all clients watching a specific exam."""
        dead = []
        for ws in self.active.get(exam_id, set()):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, exam_id)

ws_manager = ConnectionManager()

@app.websocket("/ws/monitor/{exam_id}")
async def ws_monitor(ws: WebSocket, exam_id: int):
    await ws_manager.connect(ws, exam_id)
    try:
        while True:
            await ws.receive_text()  # keep-alive; client can send pings
    except WebSocketDisconnect:
        ws_manager.disconnect(ws, exam_id)

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        # DB drops removed to persist exams across server restarts
        await conn.run_sync(Base.metadata.create_all)
        
    # Seed a default admin
    async for db in get_db():
        result = await db.execute(select(models.User).where(models.User.username == "admin"))
        if not result.scalars().first():
            admin_pw = os.getenv("ADMIN_PASSWORD")
            if not admin_pw:
                raise RuntimeError("ADMIN_PASSWORD environment variable is missing!")
            admin = models.User(username="admin", hashed_password=get_password_hash(admin_pw))
            db.add(admin)
            await db.commit()

# ========= ROUTES: Views =========

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Student Login Page"""
    return templates.TemplateResponse(request=request, name="student_login.html")

@app.get("/admin-login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    """Admin / Conductor Login Page"""
    return templates.TemplateResponse(request=request, name="login.html")

@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    """Student Password Recovery Page"""
    return templates.TemplateResponse(request=request, name="forgot_password.html")

# ========= Forgot Password OTP Store (in-memory, PoC) =========
import random
from datetime import datetime, timezone

_password_reset_otps: dict[str, dict] = {}  # {email: {"otp": "123456", "expires": datetime}}

@app.post("/api/forgot-password/send-otp")
@limiter.limit("5/minute")
async def send_password_reset_otp(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    """Generate and send a 6-digit OTP to the student's registered email."""
    result = await db.execute(
        select(models.Student).where(models.Student.email == email)
    )
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="No account found with this email.")

    otp_code = str(random.randint(100000, 999999))
    _password_reset_otps[email] = {
        "otp": otp_code,
        "expires": datetime.now(timezone.utc).timestamp() + 300  # 5 minutes
    }

    sent = send_otp_email(email, otp_code)
    if not sent:
        raise HTTPException(status_code=500, detail="Failed to send OTP email. Check SMTP config.")

    return {"status": "success", "message": "OTP sent to your email."}


@app.post("/api/forgot-password/reset")
@limiter.limit("10/minute")
async def reset_password_with_otp(
    request: Request,
    email: str = Form(...),
    otp: str = Form(...),
    new_password: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    """Verify OTP and update the student's password."""
    stored = _password_reset_otps.get(email)
    if not stored:
        raise HTTPException(status_code=400, detail="No OTP was requested for this email.")

    if datetime.now(timezone.utc).timestamp() > stored["expires"]:
        del _password_reset_otps[email]
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

    if stored["otp"] != otp.strip():
        raise HTTPException(status_code=400, detail="Invalid OTP code.")

    # OTP valid — update password
    result = await db.execute(
        select(models.Student).where(models.Student.email == email)
    )
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")

    student.hashed_password = get_password_hash(new_password)
    await db.commit()

    # Clean up used OTP
    del _password_reset_otps[email]

    return {"status": "success", "message": "Password reset successful."}


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Student universal registration"""
    return templates.TemplateResponse(request=request, name="register.html")

@app.get("/student", response_class=HTMLResponse)
async def student_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="student_dashboard.html")

@app.get("/enroll/{exam_id}", response_class=HTMLResponse)
async def enroll_page(request: Request, exam_id: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(models.Exam).where(models.Exam.id == exam_id))
    exam = res.scalars().first()
    return templates.TemplateResponse(request=request, name="enroll.html", context={"exam_id": exam_id, "exam": exam})

@app.get("/gate", response_class=HTMLResponse)
async def gate_page(request: Request):
    token = request.cookies.get("access_token")
    if not token or not token.startswith("Bearer "):
        return RedirectResponse(url="/", status_code=303)
        
    try:
        from dependencies import SECRET_KEY, ALGORITHM
        import jwt
        token_str = token.split("Bearer ")[1]
        payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
        role = payload.get("type", "")
        if role != "admin":
            return RedirectResponse(url="/student", status_code=303)
    except Exception:
        return RedirectResponse(url="/", status_code=303)
        
    return templates.TemplateResponse(request=request, name="gate_entry.html")

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin Exam Management view."""
    return templates.TemplateResponse(request=request, name="admin_dashboard.html")

@app.get("/admin/roster", response_class=HTMLResponse)
async def admin_roster_page(request: Request):
    """Admin Roster Upload and Hall Ticket QR generation view."""
    return templates.TemplateResponse(request=request, name="admin_roster.html")

@app.get("/admin/students", response_class=HTMLResponse)
async def admin_students_page(request: Request):
    """Admin Student Profiles view."""
    return templates.TemplateResponse(request=request, name="admin_students.html")

@app.get("/admin/attendance", response_class=HTMLResponse)
async def admin_attendance_page(request: Request):
    """Admin Attendance & Gate Control view."""
    return templates.TemplateResponse(request=request, name="admin_attendance.html")

@app.get("/admin/monitor", response_class=HTMLResponse)
async def admin_monitor_page(request: Request):
    """Global Monitor Dashboard view."""
    return templates.TemplateResponse(request=request, name="admin_monitor.html")

@app.get("/admin/conductors", response_class=HTMLResponse)
async def admin_conductors_page(request: Request):
    """Admin Conductor Management view."""
    return templates.TemplateResponse(request=request, name="admin_conductors.html")

@app.get("/conductor", response_class=HTMLResponse)
async def conductor_dashboard_page(request: Request):
    """Conductor Dashboard — center-scoped operator view."""
    return templates.TemplateResponse(request=request, name="conductor_dashboard.html")

# ========= ROUTES: Auth API =========

@app.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(
        key="access_token", 
        httponly=True, 
        samesite="lax"
    )
    return {"status": "success"}

@app.get("/api/me")
async def get_current_session(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token or not token.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
        
    token_str = token.split("Bearer ")[1]
    from dependencies import SECRET_KEY, ALGORITHM
    import jwt
    from sqlalchemy.orm import selectinload
    
    try:
        payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        role = payload.get("type", "student")
        
        if role == "admin":
            return {"username": username, "role": "admin"}
            
        # For students, fetch full profile and enrollments
        res = await db.execute(
            select(models.Student)
            .options(
                selectinload(models.Student.enrollments).selectinload(models.ExamEnrollment.exam),
                selectinload(models.Student.enrollments).selectinload(models.ExamEnrollment.exam_session).selectinload(models.ExamSession.center)
            )
            .where(models.Student.username == username)
        )
        student = res.scalars().first()
        
        if not student:
            return {"username": username, "role": "student"}
            
        enrollments_data = []
        for e in student.enrollments:
            center_details = None
            if e.exam_session and e.exam_session.center:
                c = e.exam_session.center
                center_details = f"{c.name}, {c.city}, {c.state}"
                
            enrollments_data.append({
                "exam_id": e.exam_id,
                "exam_name": e.exam.name if e.exam else "Unknown",
                "status": e.status,
                "reg_no": e.enrolled_reg_no,
                "date": e.exam_session.date if e.exam_session else "TBD",
                "time": e.exam_session.session_time if e.exam_session else "TBD",
                "center": center_details if center_details else "Unassigned",
                "attendance_marked": e.attendance_marked,
                "attendance_time": e.attendance_time
            })
            
        return {
            "username": student.username, 
            "role": "student",
            "name": student.name,
            "email": student.email,
            "phone": student.phone_number,
            "kyc_verified": student.kyc_verified,
            "has_face": bool(student.face_embedding),
            "enrollments": enrollments_data
        }
    except Exception as e:
        print(f"Me Endpoint Error: {e}")
        raise HTTPException(status_code=401)


@app.post("/token")
@limiter.limit("10/minute")
async def login_for_access_token(
    request: Request,
    username: str = Form(...), 
    password: str = Form(...), 
    login_type: str = Form("admin"), # student or admin
    db: AsyncSession = Depends(get_db)
):
    if login_type in ("admin", "conductor"):
        result = await db.execute(select(models.User).where(models.User.username == username))
        user = result.scalars().first()
        url = "/admin"
    else:
        result = await db.execute(select(models.Student).where(models.Student.username == username))
        user = result.scalars().first()
        url = "/student"

    if not user or not verify_password(password, user.hashed_password):
         error_url = "/admin-login" if login_type in ("admin", "conductor") else "/"
         return RedirectResponse(url=f"{error_url}?error=Incorrect credentials", status_code=303)

    # Redirect CONDUCTOR role to their own dashboard instead of admin
    if hasattr(user, 'role') and user.role == models.UserRole.CONDUCTOR:
        url = "/conductor"

    access_token = create_access_token(data={"sub": user.username, "type": login_type})
    response = RedirectResponse(url=url, status_code=303)
    response.set_cookie(
        key="access_token", 
        value=f"Bearer {access_token}", 
        httponly=True,
        samesite="lax"
    )
    return response

@app.post("/api/mobile/login")
@limiter.limit("10/minute")
async def mobile_login_for_access_token(
    request: Request,
    username: str = Form(...), 
    password: str = Form(...), 
    login_type: str = Form("student"),
    db: AsyncSession = Depends(get_db)
):
    """Pure REST API for Mobile App Login."""
    if login_type in ("admin", "conductor"):
        result = await db.execute(select(models.User).where(models.User.username == username))
        user = result.scalars().first()
    else:
        result = await db.execute(select(models.Student).where(models.Student.username == username))
        user = result.scalars().first()

    if not user or not verify_password(password, user.hashed_password):
         raise HTTPException(status_code=401, detail="Incorrect credentials")

    access_token = create_access_token(data={"sub": user.username, "type": login_type})
    return {"access_token": access_token, "token_type": "bearer", "role": login_type}

# ========= ROUTES: Admin API =========

from pydantic import BaseModel
from typing import List, Optional

class SessionCreate(BaseModel):
    date: str
    session_time: str
    capacity: int

class CenterCreate(BaseModel):
    state: str
    city: str
    name: str
    sessions: List[SessionCreate]

class ExamCreate(BaseModel):
    name: str
    verification_mode: str = "FACE_ONLY"
    registration_form_config: list = []
    centers: List[CenterCreate] = []

@app.post("/api/admin/exam")
async def create_exam(exam_data: ExamCreate, db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    # Pre-flight check: ensure no past dates are submitted
    for c in exam_data.centers:
        for s in c.sessions:
            if s.date < today_str:
                raise HTTPException(status_code=400, detail=f"Cannot schedule exam for a past date: {s.date}")

    exam = models.Exam(name=exam_data.name, registration_form_config=exam_data.registration_form_config)
    db.add(exam)
    await db.flush()
    
    for c in exam_data.centers:
        new_cntr = models.Center(exam_id=exam.id, state=c.state, city=c.city, name=c.name)
        db.add(new_cntr)
        await db.flush()
        
        for s in c.sessions:
            new_sess = models.ExamSession(center_id=new_cntr.id, date=s.date, session_time=s.session_time, capacity=s.capacity)
            db.add(new_sess)
            
    await db.commit()
    return {"status": "success"}

@app.delete("/api/admin/exam/{exam_id}")
async def delete_exam(exam_id: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(models.Exam).where(models.Exam.id == exam_id))
    exam = res.scalars().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
        
    # Safely clear out Foreign Key dependencies before deleting the parent exam record
    # 1. Harvest conductor user IDs before deleting profiles
    res_cond = await db.execute(select(models.ConductorProfile).where(models.ConductorProfile.exam_id == exam_id))
    user_ids = [c.user_id for c in res_cond.scalars().all() if c.user_id]
        
    # 2. Delete child records
    await db.execute(models.ConductorProfile.__table__.delete().where(models.ConductorProfile.exam_id == exam_id))
    await db.execute(models.PreApprovedCandidate.__table__.delete().where(models.PreApprovedCandidate.exam_id == exam_id))
    await db.execute(models.ExamEnrollment.__table__.delete().where(models.ExamEnrollment.exam_id == exam_id))
    await db.flush() # Commit the deletes to DB level before we delete users
    
    # 3. Delete orphaned conductor users
    for uid in user_ids:
        await db.execute(models.User.__table__.delete().where(models.User.id == uid))
    
    await db.delete(exam)
    await db.commit()
    return {"status": "success"}

@app.post("/api/admin/roster/{exam_id}")
async def upload_roster(exam_id: int, reg_number: str = Form(...), name: str = Form(...), db: AsyncSession = Depends(get_db)):
    roster_entry = models.PreApprovedCandidate(exam_id=exam_id, registration_no=reg_number, name=name)
    db.add(roster_entry)
    await db.commit()
    return {"status": "success"}

def process_bulk_roster_and_email(exam_id: int, exam_name: str, candidates: list):
    """Generates QR codes and saves them to simulate an email dispatch."""
    os.makedirs("mock_sent_emails", exist_ok=True)
    for c in candidates:
        reg_no = c['reg_no']
        name = c['name']
        email = c['email']
        center = c['center']
        date = c['date']
        time = c['time']
        
        email_str = email if email else "no-reply@digiexam.poc"
        
        # Generate QR
        payload = json.dumps({"exam_id": exam_id, "reg_no": reg_no})
        img = qrcode.make(payload)
        buf = BytesIO()
        img.save(buf, format="PNG")
        qr_bytes = buf.getvalue()
        
        # Save a local backup
        clean_email = email_str.replace("@", "_at_").replace(".", "_")
        filename = f"mock_sent_emails/{clean_email}_{reg_no}.png"
        with open(filename, "wb") as f:
            f.write(qr_bytes)
            
        print(f"[DISPATCH LOG] Saved backup QR for: {name}")
        
        # Send REAL email if user provided an email address
        if email:
            send_hall_ticket_email(email_str, name, exam_id, exam_name, reg_no, center, date, time, qr_bytes)

@app.post("/api/admin/roster/bulk/{exam_id}")
async def upload_bulk_roster(
    exam_id: int, 
    background_tasks: BackgroundTasks,
    csv_file: UploadFile = File(...), 
    db: AsyncSession = Depends(get_db)
):
    contents = await csv_file.read()
    decoded = contents.decode("utf-8").splitlines()
    reader = csv.DictReader(decoded)
    
    from sqlalchemy import func
    # 1. Fetch available exam centers and exam name
    res_exam = await db.execute(select(models.Exam).where(models.Exam.id == exam_id))
    exam = res_exam.scalars().first()
    exam_name = exam.name if exam else f"Exam #{exam_id}"
    
    res = await db.execute(select(models.Center).where(models.Center.exam_id == exam_id))
    centers = res.scalars().all()
    center_map = {c.name.strip().lower(): c for c in centers}
    
    # Pre-fetch counts to validate seat capacity
    res_sess = await db.execute(select(models.ExamSession).where(
        models.ExamSession.center_id.in_([c.id for c in centers]) if centers else False
    ))
    sessions = res_sess.scalars().all()
    
    session_map = {}
    for s in sessions:
        res_count = await db.execute(select(func.count(models.PreApprovedCandidate.id)).where(models.PreApprovedCandidate.exam_session_id == s.id))
        count = res_count.scalar()
        session_map[s.id] = {"cap": s.capacity, "used": count}
        
    def find_session(center_name, date_str, time_str):
        c = center_map.get(center_name.strip().lower())
        if not c: return None
        for s in sessions:
            if s.center_id == c.id and s.date == date_str.strip() and s.session_time == time_str.strip():
                return s
        return None

    candidates = []
    db_adds = []
    
    for row in reader:
        reg_no = row.get("reg_no", "").strip()
        name = row.get("name", "").strip()
        email = row.get("email", "").strip()
        center_str = row.get("center", "").strip()
        date_str = row.get("date", "").strip()
        session_str = row.get("session", "").strip()
        
        if reg_no and name and center_str and date_str and session_str:
            target_sess = find_session(center_str, date_str, session_str)
            if not target_sess:
                raise HTTPException(400, f"Session not found: {center_str} on {date_str} ({session_str})")
            
            s_data = session_map[target_sess.id]
            if s_data["used"] >= s_data["cap"]:
                raise HTTPException(400, f"Seat Capacity MAXED OUT for {center_str} {session_str}! Only {s_data['cap']} physical seats exist.")
                
            s_data["used"] += 1
            
            roster_entry = models.PreApprovedCandidate(
                exam_id=exam_id, 
                exam_session_id=target_sess.id,
                registration_no=reg_no, 
                name=name, 
                email=email
            )
            db_adds.append(roster_entry)
            candidates.append({
                "reg_no": reg_no, 
                "name": name, 
                "email": email,
                "center": center_str,
                "date": date_str,
                "time": session_str
            })
            
    for entry in db_adds:
        db.add(entry)
        
    await db.commit()
    
    # Fire off Emails asynchronously
    if candidates:
        background_tasks.add_task(process_bulk_roster_and_email, exam_id, exam_name, candidates)
        
    return {"status": "success", "imported": len(candidates), "message": "Processing emails..."}

@app.get("/api/admin/hallticket")
async def generate_hallticket(exam_id: int, registration_no: str, t: str = None):
    """Generates an image of a QR code containing the payload"""
    payload = json.dumps({"exam_id": exam_id, "reg_no": registration_no})
    img = qrcode.make(payload)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")

@app.get("/api/admin/hallticket/pdf")
async def generate_pdf_hallticket(exam_id: int, registration_no: str, db: AsyncSession = Depends(get_db)):
    from sqlalchemy.orm import selectinload
    from reportlab.lib.pagesizes import A4
    from reportlab.graphics import barcode
    
    # Try to fetch Enrollment first (to get the Student and their passport photo)
    res_enrol = await db.execute(
        select(models.ExamEnrollment)
        .options(
            selectinload(models.ExamEnrollment.student),
            selectinload(models.ExamEnrollment.exam_session).selectinload(models.ExamSession.center), 
            selectinload(models.ExamEnrollment.exam)
        )
        .where(models.ExamEnrollment.exam_id == exam_id, models.ExamEnrollment.enrolled_reg_no == registration_no)
    )
    enrollment = res_enrol.scalars().first()
    
    # Fallback to pure roster if not enrolled yet
    if not enrollment:
        res_roster = await db.execute(
            select(models.PreApprovedCandidate)
            .options(selectinload(models.PreApprovedCandidate.exam_session).selectinload(models.ExamSession.center), selectinload(models.PreApprovedCandidate.exam))
            .where(models.PreApprovedCandidate.exam_id == exam_id, models.PreApprovedCandidate.registration_no == registration_no)
        )
        candidate = res_roster.scalars().first()
        if not candidate:
            raise HTTPException(404, "Candidate not found in roster or enrollments.")
        
        c_name = candidate.name
        e_name = candidate.exam.name if candidate.exam else 'Unknown Exam'
        session = candidate.exam_session
        
        # Heuristic: Try to find their globally registered passport photo via their roster Email
        username = None
        if candidate.email:
            res_user = await db.execute(select(models.Student.username).where(models.Student.email == candidate.email))
            username = res_user.scalars().first()
    else:
        c_name = enrollment.student.name if enrollment.student else "Unknown Student"
        e_name = enrollment.exam.name if enrollment.exam else 'Unknown Exam'
        session = enrollment.exam_session
        username = enrollment.student.username if enrollment.student else None

    # Resolve center details
    if session and session.center:
        center_name = session.center.name
        center_address = f"{session.center.city}, {session.center.state}"
        exam_date = session.date
        exam_time = session.session_time
    else:
        center_name = "Unallocated"
        center_address = "N/A"
        exam_date = "TBD"
        exam_time = "TBD"

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    
    # --- Top Header ---
    c.setFont("Helvetica-Bold", 16)
    c.drawString(45, height - 50, "Digi-Exam Authority")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(220, height - 50, f"Admit Card - {e_name}")
    
    # Top-Right Edge Terminal Sync QR Code with Full Details
    payload = json.dumps({
        "exam_id": exam_id, 
        "reg_no": registration_no,
        "name": c_name.upper(),
        "exam_name": e_name,
        "session": f"{exam_date} {exam_time}",
        "center": center_name
    })
    qr_img = qrcode.make(payload, box_size=4, border=1)
    qr_buf = BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    
    qr_size = 65
    c.drawImage(ImageReader(qr_buf), width - 40 - qr_size, height - 80, width=qr_size, height=qr_size)
    
    # Heavy Double Line
    line_y = height - 90
    c.setLineWidth(2)
    c.line(40, line_y, width - 40, line_y)
    c.setLineWidth(0.5)
    c.line(40, line_y - 3, width - 40, line_y - 3)
    
    # Admit Card Sub-Title
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(width / 2, height - 110, "Admit Card")
    c.line(width/2 - 30, height - 112, width/2 + 30, height - 112)
    
    # --- Passport Photo ---
    photo_path = f"uploads_profile_pics/{username}_passport.jpg" if username else None
    photo_x = width - 160
    photo_y = height - 270
    photo_w = 110
    photo_h = 140
    if photo_path and os.path.exists(photo_path):
        c.drawImage(photo_path, photo_x, photo_y, width=photo_w, height=photo_h)
    else:
        c.rect(photo_x, photo_y, photo_w, photo_h)
        c.setFont("Helvetica", 8)
        c.drawCentredString(photo_x + photo_w/2, photo_y + photo_h/2, "Photo Not Available")
        
    # --- Detail Table ---
    start_y = height - 140
    line_h = 24
    
    details = [
        ("1. Hall Ticket Number", f": {registration_no}"),
        ("2. Name of the Candidate", f": {c_name.upper()}"),
        ("3. Date of Exam", f": {exam_date}"),
        ("4. Sections Appearing", f": {exam_time}"),
        ("5. Courses Applied for", f": Standard Certification"),
        ("6. Name of the Test Centre", f": {center_name}"),
    ]
    
    for i, (label, val) in enumerate(details):
        c.setFont("Helvetica-Bold", 9)
        c.drawString(45, start_y - i*line_h, label)
        c.setFont("Helvetica", 9)
        c.drawString(220, start_y - i*line_h, val)
        
    c.setFont("Helvetica-Bold", 9)
    c.drawString(45, start_y - 6*line_h, "7. Address of the Test Centre")
    c.setFont("Helvetica", 9)
    c.drawString(220, start_y - 6*line_h, f": {center_address}")
    
    # Heavy Double Line
    bottom_y = start_y - 7*line_h - 20
    c.setLineWidth(2)
    c.line(40, bottom_y, width - 40, bottom_y)
    c.setLineWidth(0.5)
    c.line(40, bottom_y - 3, width - 40, bottom_y - 3)
    
    # --- General Instructions ---
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(width / 2, bottom_y - 30, "GENERAL INSTRUCTIONS FOR ALL THE CANDIDATES")
    
    instructions = [
        "1. Candidates are advised to become familiar with the computer-based exam before the session.",
        "2. Candidates must appear at the specified date and time at the venue mentioned on the Admit Card.",
        "3. Candidates will not be allowed to enter the exam hall without the Admit Card.",
        "4. Candidates should bring their photo identity card and this printed Admit Card.",
        "5. Candidates should arrive at the venue 30 minutes before the start of the exam.",
        "6. No extra time will be given to candidates reaching late.",
        "7. Candidates are not allowed to use any books, calculators, mobile phones, or gadgets.",
        "8. Candidates will be disqualified if found indulging in any kind of malpractice.",
    ]
    #hrllo hello
    c.setFont("Helvetica", 9)
    inst_y = bottom_y - 60
    for inst in instructions:
        c.drawString(50, inst_y, inst)
        inst_y -= 22
        
    c.showPage()
    c.save()

    return Response(content=buf.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="AdmitCard_{registration_no}.pdf"'})

@app.get("/api/admin/exams")
async def get_exams(db: AsyncSession = Depends(get_db)):
    from sqlalchemy.orm import selectinload
    res = await db.execute(select(models.Exam).options(selectinload(models.Exam.centers).selectinload(models.Center.sessions)))
    exams = res.scalars().all()
    
    out = []
    for e in exams:
        ctr_list = []
        for c in e.centers:
            sess_list = []
            for s in c.sessions:
                sess_list.append({"id": s.id, "date": s.date, "session_time": s.session_time, "capacity": s.capacity})
            ctr_list.append({"id": c.id, "state": c.state, "city": c.city, "name": c.name, "sessions": sess_list})
            
        out.append({
            "id": e.id,
            "name": e.name,
            "is_open": e.is_open,
            "gate_open": getattr(e, "gate_open", False),
            "verification_mode": getattr(e, "verification_mode", "FACE_ONLY"),
            "centers": ctr_list
        })
    return out

@app.get("/api/admin/roster_list/{exam_id}")
async def get_roster_list(exam_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy.orm import selectinload
    res = await db.execute(select(models.PreApprovedCandidate)
                           .options(selectinload(models.PreApprovedCandidate.exam_session).selectinload(models.ExamSession.center))
                           .where(models.PreApprovedCandidate.exam_id == exam_id))
    out = []
    for r in res.scalars().all():
        center_text = f"{r.exam_session.center.name} ({r.exam_session.date} {r.exam_session.session_time})" if r.exam_session else "Unallocated"
        out.append({"id": r.id, "reg_no": r.registration_no, "name": r.name, "email": r.email, "center": center_text})
    return out

@app.get("/api/admin/enrollments")
async def get_enrollments(db: AsyncSession = Depends(get_db)):
    # Returns enrollments mapped to students and exams
    from sqlalchemy.orm import selectinload
    res = await db.execute(select(models.ExamEnrollment).options(selectinload(models.ExamEnrollment.student), selectinload(models.ExamEnrollment.exam)))
    return [{"id": e.id, "exam": e.exam.name, "student": e.student.name, "reg_no": e.enrolled_reg_no, "status": e.status} for e in res.scalars().all()]

@app.get("/api/admin/students")
async def get_all_students(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(models.Student))
    students = res.scalars().all()
    return [
        {
            "id": s.id, 
            "username": s.username, 
            "name": s.name, 
            "email": s.email, 
            "phone": s.phone_number,
            "has_face": bool(s.face_embedding),
            "status": s.status.value if s.status else "APPROVED",
            "rejection_reason": s.rejection_reason
        } for s in students
    ]

@app.post("/api/admin/exam/{exam_id}/toggle_gate")
async def toggle_exam_gate(exam_id: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(models.Exam).where(models.Exam.id == exam_id))
    exam = res.scalars().first()
    if not exam: raise HTTPException(404, "Exam not found")
    exam.gate_open = not exam.gate_open
    await db.commit()
    await ws_manager.broadcast(exam_id, {
        "type": "gate_toggle",
        "exam_id": exam_id,
        "gate_open": exam.gate_open,
    })
    return {"status": "success", "gate_open": exam.gate_open}

@app.get("/api/admin/exam/{exam_id}/attendance")
async def get_exam_attendance(exam_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy.orm import selectinload
    res = await db.execute(
        select(models.ExamEnrollment)
        .options(
            selectinload(models.ExamEnrollment.student),
            selectinload(models.ExamEnrollment.exam_session).selectinload(models.ExamSession.center)
        )
        .where(models.ExamEnrollment.exam_id == exam_id)
    )
    enrollments = res.scalars().all()
    
    out = []
    for e in enrollments:
        if e.exam_session and e.exam_session.center:
            state_str = e.exam_session.center.state
            city_str = e.exam_session.center.city
            center_str = e.exam_session.center.name
            session_str = f"{e.exam_session.date} {e.exam_session.session_time}"
        else:
            state_str = city_str = center_str = session_str = "Unknown"
            
        out.append({
            "enrollment_id": e.id,
            "student_name": e.student.name if e.student else "Unknown",
            "reg_no": e.enrolled_reg_no,
            "state": state_str,
            "city": city_str,
            "center": center_str,
            "session": session_str,
            "attendance": e.attendance_marked,
            "attendance_time": e.attendance_time
        })
    return out

@app.get("/api/admin/exam/{exam_id}/attendance/export")
async def export_attendance_csv(exam_id: int, db: AsyncSession = Depends(get_db)):
    """Export center-wise attendance for an exam as a downloadable CSV file."""
    from io import StringIO
    from sqlalchemy.orm import selectinload

    res = await db.execute(
        select(models.ExamEnrollment)
        .options(
            selectinload(models.ExamEnrollment.student),
            selectinload(models.ExamEnrollment.exam_session).selectinload(models.ExamSession.center),
            selectinload(models.ExamEnrollment.exam)
        )
        .where(models.ExamEnrollment.exam_id == exam_id)
        .order_by(models.ExamEnrollment.exam_session_id)
    )
    enrollments = res.scalars().all()

    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Reg No", "Candidate Name", "State", "City", "Center", "Session Date", "Session Time", "Status", "Check-in Time"])

    for e in enrollments:
        if e.exam_session and e.exam_session.center:
            c = e.exam_session.center
            state, city, center_name = c.state, c.city, c.name
            date_val, time_val = e.exam_session.date, e.exam_session.session_time
        else:
            state = city = center_name = date_val = time_val = "Unallocated"

        writer.writerow([
            e.enrolled_reg_no,
            e.student.name if e.student else "Unknown",
            state, city, center_name, date_val, time_val,
            "Present" if e.attendance_marked else "Absent",
            e.attendance_time or "—"
        ])

    exam_name = enrollments[0].exam.name.replace(" ", "_") if enrollments and enrollments[0].exam else f"exam_{exam_id}"
    filename = f"Attendance_{exam_name}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/api/admin/monitor/{exam_id}")
async def get_exam_monitor(exam_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func
    from sqlalchemy.orm import selectinload
    
    # 1. Get all sessions for this exam
    res_sess = await db.execute(
        select(models.ExamSession)
        .join(models.Center)
        .where(models.Center.exam_id == exam_id)
        .options(selectinload(models.ExamSession.center))
    )
    sessions = res_sess.scalars().all()
    
    out = []
    for s in sessions:
        # Tally metrics via optimized COUNT queries
        res_roster = await db.execute(select(func.count(models.PreApprovedCandidate.id)).where(models.PreApprovedCandidate.exam_session_id == s.id))
        total_roster = res_roster.scalar() or 0
        
        res_enrolled = await db.execute(select(func.count(models.ExamEnrollment.id)).where(models.ExamEnrollment.exam_session_id == s.id, models.ExamEnrollment.status == "APPROVED"))
        successful_enrollments = res_enrolled.scalar() or 0
        
        res_bio = await db.execute(select(func.count(models.ExamEnrollment.id)).where(models.ExamEnrollment.exam_session_id == s.id, models.ExamEnrollment.attendance_marked == True))
        biometric_success = res_bio.scalar() or 0
        
        pending = total_roster - biometric_success
        
        out.append({
            "session_id": s.id,
            "center_name": s.center.name,
            "city": s.center.city,
            "state": s.center.state,
            "session_time": f"{s.date} {s.session_time}",
            "metrics": {
                "total_roster": total_roster,
                "successful_enrollments": successful_enrollments,
                "biometric_success": biometric_success,
                "pending": pending
            }
        })
        
    return out

# ========= ROUTES: Edge Terminal Export & Sync =========

EDGE_HMAC_SECRET = os.environ.get("EDGE_HMAC_SECRET", "digi-exam-edge-secret-key-2026")

@app.get("/api/admin/exam/{exam_id}/export_package")
async def export_exam_package(exam_id: int, db: AsyncSession = Depends(get_db)):
    """Exports a signed .digipack file for use by the offline Edge Gate Terminal."""
    import hmac
    import hashlib
    from datetime import datetime
    from fastapi.responses import JSONResponse
    from sqlalchemy.orm import selectinload

    # Get exam
    res_exam = await db.execute(select(models.Exam).where(models.Exam.id == exam_id))
    exam = res_exam.scalars().first()
    if not exam:
        raise HTTPException(404, "Exam not found")

    # Get all pre-approved candidates for this exam, joined with their enrolled student
    res_candidates = await db.execute(
        select(models.PreApprovedCandidate)
        .join(models.ExamSession, models.PreApprovedCandidate.exam_session_id == models.ExamSession.id)
        .join(models.Center, models.ExamSession.center_id == models.Center.id)
        .options(
            selectinload(models.PreApprovedCandidate.exam_session).selectinload(models.ExamSession.center)
        )
        .where(models.Center.exam_id == exam_id)
    )
    candidates = res_candidates.scalars().all()

    # For each candidate, attempt to find their face embedding from enrolled student
    candidate_list = []
    for c in candidates:
        # Find enrolled student matching reg_no
        res_enroll = await db.execute(
            select(models.ExamEnrollment)
            .options(selectinload(models.ExamEnrollment.student))
            .where(
                models.ExamEnrollment.exam_id == exam_id,
                models.ExamEnrollment.enrolled_reg_no == c.registration_no
            )
        )
        enrollment = res_enroll.scalars().first()
        embedding = None
        if enrollment and enrollment.student and enrollment.student.face_embedding:
            embedding = enrollment.student.face_embedding  # stored as JSON string

        sess = c.exam_session
        center = sess.center if sess else None
        candidate_list.append({
            "reg_no": c.registration_no,
            "name": c.name,
            "email": c.email or "",
            "session_id": c.exam_session_id,
            "session_label": f"{sess.date} {sess.session_time}" if sess else "Unknown",
            "center_name": center.name if center else "Unknown",
            "city": center.city if center else "",
            "state": center.state if center else "",
            "face_embedding": embedding
        })

    payload = {
        "exam_id": exam_id,
        "exam_name": exam.name,
        "exported_at": datetime.now().isoformat(),
        "candidates": candidate_list
    }

    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sig = hmac.new(EDGE_HMAC_SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()
    signed_package = {"signature": sig, "payload": payload}

    from fastapi.responses import Response as FR
    return FR(
        content=json.dumps(signed_package, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=exam_{exam_id}.digipack"}
    )

@app.post("/api/admin/exam/{exam_id}/sync_logs")
async def sync_edge_logs(exam_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Accepts a batch of attendance logs from the Edge Terminal and syncs them to the DB."""
    import hmac
    import hashlib
    from datetime import datetime

    body = await request.json()
    signature = body.get("signature", "")
    logs = body.get("logs", [])

    # Verify HMAC signature
    logs_bytes = json.dumps(logs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    expected_sig = hmac.new(EDGE_HMAC_SECRET.encode(), logs_bytes, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(signature, expected_sig):
        raise HTTPException(status_code=403, detail="Invalid package signature. Sync rejected.")

    synced = 0
    skipped = 0
    for log_entry in logs:
        reg_no = log_entry.get("reg_no")
        status = log_entry.get("status", "FAIL")
        timestamp = log_entry.get("timestamp", datetime.now().isoformat())
        confidence = str(log_entry.get("confidence", 0.0))

        # Find enrollment
        res_enroll = await db.execute(
            select(models.ExamEnrollment)
            .options(__import__('sqlalchemy.orm', fromlist=['selectinload']).selectinload(models.ExamEnrollment.student))
            .where(
                models.ExamEnrollment.exam_id == exam_id,
                models.ExamEnrollment.enrolled_reg_no == reg_no
            )
        )
        enrollment = res_enroll.scalars().first()
        if not enrollment:
            skipped += 1
            continue

        if status == "PASS" and not enrollment.attendance_marked:
            enrollment.attendance_marked = True
            enrollment.attendance_time = timestamp

        # Write gate log
        try:
            dt_obj = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).replace(tzinfo=None)
        except Exception:
            dt_obj = datetime.now().replace(tzinfo=None)
            
        gate_log = models.GateLog(
            student_id=enrollment.student_id,
            timestamp=dt_obj,
            status="SUCCESS" if status == "PASS" else "FAILURE",
            method="EDGE_TERMINAL",
            confidence_score=confidence
        )
        db.add(gate_log)
        synced += 1

    await db.commit()
    await ws_manager.broadcast(exam_id, {
        "type": "sync_batch",
        "exam_id": exam_id,
        "synced": synced,
        "skipped": skipped,
        "total": len(logs),
    })
    return {"status": "success", "synced": synced, "skipped": skipped, "total": len(logs)}

@app.post("/api/admin/attendance/{enrollment_id}/mark")
async def toggle_attendance_mark(enrollment_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy.orm import selectinload
    res = await db.execute(
        select(models.ExamEnrollment)
        .options(selectinload(models.ExamEnrollment.student))
        .where(models.ExamEnrollment.id == enrollment_id)
    )
    enrollment = res.scalars().first()
    if not enrollment: raise HTTPException(404, "Enrollment not found")
    
    enrollment.attendance_marked = not enrollment.attendance_marked
    
    if enrollment.attendance_marked:
        from datetime import datetime
        enrollment.attendance_time = datetime.now().strftime("%Y-%m-%d %I:%M %p")
        # Also log a MANUAL method gate log if marking present manually
        log = models.GateLog(
            student_id=enrollment.student_id,
            status="SUCCESS",
            method="MANUAL",
            confidence_score="1.0"
        )
        db.add(log)
    else:
        enrollment.attendance_time = None
        
    await db.commit()
    
    # Broadcast to live dashboards
    student_name = enrollment.student.name if enrollment.student else "Unknown"
    await ws_manager.broadcast(enrollment.exam_id, {
        "type": "attendance",
        "exam_id": enrollment.exam_id,
        "enrollment_id": enrollment.id,
        "reg_no": enrollment.enrolled_reg_no,
        "student_name": student_name,
        "attendance_marked": enrollment.attendance_marked,
        "attendance_time": enrollment.attendance_time,
    })
    return {"status": "success", "attendance_marked": enrollment.attendance_marked}

@app.put("/api/admin/student/{student_id}")
async def edit_student(
    student_id: int,
    name: str = Form(...),
    email: str = Form(None),
    username: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(models.Student).where(models.Student.id == student_id))
    student = res.scalars().first()
    if not student: raise HTTPException(404, detail="Not found")
    student.name = name
    student.email = email
    student.username = username
    await db.commit()
    return {"status": "success"}

@app.delete("/api/admin/student/{student_id}")
async def delete_student(student_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import delete
    await db.execute(delete(models.GateLog).where(models.GateLog.student_id == student_id))
    await db.execute(delete(models.ExamEnrollment).where(models.ExamEnrollment.student_id == student_id))
    res = await db.execute(select(models.Student).where(models.Student.id == student_id))
    student = res.scalars().first()
    if not student: raise HTTPException(404, detail="Not found")
    await db.delete(student)
    await db.commit()
    return {"status": "success"}

# ========= ROUTES: Student API =========

@app.post("/api/student/register")
@limiter.limit("5/minute")
async def api_register_student(
    request: Request,
    name: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    email: str = Form(None),
    phone: str = Form(None),
    photo: UploadFile = File(None),
    embedding_json: str = Form(None),
    passport_photo: UploadFile = File(None),
    db: AsyncSession = Depends(get_db)
):
    """Universal Profile Registration"""
    if passport_photo:
        passport_contents = await passport_photo.read()
        if len(passport_contents) > 500 * 1024:
            raise HTTPException(status_code=400, detail="Passport photo exceeds 500KB limit. Please compress.")
            
        os.makedirs("uploads_profile_pics", exist_ok=True)
        with open(f"uploads_profile_pics/{username}_passport.jpg", "wb") as f:
            f.write(passport_contents)

    if embedding_json:
        try:
            embedding = json.loads(embedding_json)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid embedding_json")
    elif photo:
        contents = await photo.read()
        embedding = get_face_embedding(contents)
    else:
        raise HTTPException(status_code=400, detail="Must provide either photo or embedding_json")
    
    if len(embedding) == 0:
        raise HTTPException(status_code=400, detail="No face detected in live photo.")
        
    # Security: Cross-check against all global biometrics for identical matches
    from services.face import compute_similarity
    res = await db.execute(select(models.Student).where(models.Student.face_embedding != None))
    existing_students = res.scalars().all()
    
    similarity_alert = ""
    highest_sim = 0.0
    twin_name = ""
    
    for s in existing_students:
        try:
            stored_emb = json.loads(s.face_embedding)
            sim = compute_similarity(embedding, stored_emb)
            if sim > highest_sim:
                highest_sim = sim
                twin_name = s.name
        except Exception:
            pass
            
    # > 0.85 indicates twin or same person wearing different clothes
    if highest_sim > 0.55: 
        similarity_alert = f"SECURITY WARNING: Extremely high biometric similarity ({highest_sim*100:.1f}%) detected with an existing registered candidate: {twin_name}."
        student_status = models.StudentStatus.PENDING_APPROVAL
        rejection_reason = f"Duplicate Flag: {highest_sim*100:.1f}% match with {twin_name}"
    else:
        similarity_alert = ""
        student_status = models.StudentStatus.APPROVED
        rejection_reason = None
        
    new_student = models.Student(
        username=username,
        hashed_password=get_password_hash(password),
        name=name,
        email=email,
        phone_number=phone,
        face_embedding=json.dumps(embedding),
        kyc_verified=True if highest_sim <= 0.55 else False,
        status=student_status,
        rejection_reason=rejection_reason
    )
    db.add(new_student)
    await db.commit()
    
    msg = "Profile successfully generated!"
    if similarity_alert:
        msg += "\n\n" + similarity_alert
        
    return {"status": "success", "username": username, "message": msg}

def read_qr_from_bytes(file_bytes: bytes) -> dict:
    np_arr = np.frombuffer(file_bytes, np.uint8)
    img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    detector = cv2.QRCodeDetector()
    data, bbox, straight_qrcode = detector.detectAndDecode(img)
    if data:
        try:
            return json.loads(data)
        except:
            return {}
    return {}

@app.get("/api/exam/{exam_id}/sessions")
async def get_exam_sessions(exam_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy.orm import selectinload
    from sqlalchemy import func
    # Find all centers and their sessions for this exam
    res = await db.execute(select(models.Center).options(selectinload(models.Center.sessions)).where(models.Center.exam_id == exam_id))
    centers = res.scalars().all()
    
    out = []
    for c in centers:
        s_list = []
        for s in c.sessions:
            # Count how many pre-approved candidates are already mapped to this session
            res_count = await db.execute(select(func.count(models.ExamEnrollment.id)).where(
                models.ExamEnrollment.exam_session_id == s.id,
                models.ExamEnrollment.status == "APPROVED"
            ))
            used = res_count.scalar()
            
            # Count actual enrollments (if you want to track real enrollments)
            # res_enroll = await db.execute(select(func.count(models.ExamEnrollment.id)).where(models.ExamEnrollment.exam_session_id == s.id))
            # enrolled = res_enroll.scalar()
            
            s_list.append({
                "id": s.id,
                "date": s.date,
                "session_time": s.session_time,
                "capacity": s.capacity,
                "used": used,
                "available": max(0, s.capacity - used)
            })
        out.append({
            "id": c.id,
            "state": c.state,
            "city": c.city,
            "name": c.name,
            "sessions": s_list
        })
    return out

@app.get("/api/exam/{exam_id}/can_enroll")
async def check_can_enroll(exam_id: int, db: AsyncSession = Depends(get_db)):
    """Check if the admin has authorized any candidates for this exam yet."""
    res = await db.execute(select(models.PreApprovedCandidate).where(models.PreApprovedCandidate.exam_id == exam_id).limit(1))
    has_roster = res.scalars().first() is not None
    return {"can_enroll": has_roster}

@app.post("/api/student/enroll/{exam_id}")
@limiter.limit("10/minute")
async def api_enroll_student(
    request: Request,
    exam_id: int,
    username: str = Form(...), # Simulating logged-in user passing ID for PoC
    reg_no: str = Form(...),
    session_id: int = Form(None), # Dynamic Session Choice
    db: AsyncSession = Depends(get_db)
):
    """Enrolls student using Registration Number."""
    # 1. Fetch Student
    res = await db.execute(select(models.Student).where(models.Student.username == username))
    student = res.scalars().first()
    if not student: raise HTTPException(status_code=404, detail="Student not found")

    # 2. Check if student is authorized (in Roster)
    res_roster = await db.execute(select(models.PreApprovedCandidate).where(
        models.PreApprovedCandidate.exam_id == exam_id,
        models.PreApprovedCandidate.registration_no == reg_no
    ))
    roster_entry = res_roster.scalars().first()
    
    if not roster_entry:
        raise HTTPException(status_code=403, detail="Registration Number not authorized for this exam.")
        
    # STRICT CROSS-CHECK: Identity Verification
    # Ensure the registered Global Profile matches the Roster details (Name or Email)
    s_name = student.name.strip().lower() if student.name else ""
    s_email = student.email.strip().lower() if student.email else ""
    
    r_name = roster_entry.name.strip().lower() if roster_entry.name else ""
    r_email = roster_entry.email.strip().lower() if roster_entry.email else ""
    
    # We require either the email to match exactly (if both exist), or the name to match.
    match_email = (s_email != "" and r_email != "" and s_email == r_email)
    match_name = (s_name != "" and r_name != "" and s_name == r_name)
    
    if not (match_email or match_name):
        raise HTTPException(
            status_code=403, 
            detail="Identity Mismatch: Your Global Profile details do not match the assigned candidate for this Registration Number."
        )

    # CROSS-CHECK: Did the student select the exact center/date/time session they were assigned by the Admin?
    if session_id and roster_entry.exam_session_id != session_id:
        raise HTTPException(status_code=403, detail="Mismatch! The location/session you selected does not match the one assigned to your Registration Number.")

    # 3. Extract dynamic form data from the multipart request
    form_data = await request.form()
    dynamic_payload = {}
    for key, val in form_data.items():
        if key not in ("username", "reg_no", "session_id"):
            dynamic_payload[key] = val

    # 4. Success -> Automatically Approve!
    enrollment = models.ExamEnrollment(
        student_id=student.id,
        exam_id=exam_id,
        exam_session_id=session_id,
        enrolled_reg_no=reg_no,
        status="APPROVED",
        dynamic_data=json.dumps(dynamic_payload)
    )
    db.add(enrollment)
    await db.commit()
    return {"status": "success", "message": "Hall Ticket Verified. Automatically Approved!"}



# ========= ROUTES: Admin — Conductor Management =========

@app.get("/api/admin/conductors")
async def list_conductors(db: AsyncSession = Depends(get_db)):
    """List all conductors with their assigned center and exam info."""
    from sqlalchemy.orm import selectinload
    res = await db.execute(
        select(models.ConductorProfile)
        .options(
            selectinload(models.ConductorProfile.user),
            selectinload(models.ConductorProfile.center),
            selectinload(models.ConductorProfile.exam),
        )
    )
    conductors = res.scalars().all()
    return [
        {
            "id": c.id,
            "display_name": c.display_name,
            "username": c.user.username if c.user else None,
            "center_id": c.center_id,
            "center_name": c.center.name if c.center else None,
            "city": c.center.city if c.center else None,
            "state": c.center.state if c.center else None,
            "exam_id": c.exam_id,
            "exam_name": c.exam.name if c.exam else None,
        }
        for c in conductors
    ]

@app.post("/api/admin/conductors")
async def create_conductor(request: Request, db: AsyncSession = Depends(get_db)):
    """Create a new Conductor user and associate them with a center."""
    body = await request.json()
    display_name = body.get("display_name", "").strip()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    center_id = body.get("center_id")
    device_pin = body.get("device_pin", "1234").strip()

    if not all([display_name, username, password, center_id]):
        raise HTTPException(400, "display_name, username, password, and center_id are required")

    # Check username uniqueness
    existing = await db.execute(select(models.User).where(models.User.username == username))
    if existing.scalars().first():
        raise HTTPException(409, f"Username '{username}' is already taken.")

    # Load center to get exam_id
    center_res = await db.execute(select(models.Center).where(models.Center.id == center_id))
    center = center_res.scalars().first()
    if not center:
        raise HTTPException(404, "Center not found")

    # Create User
    new_user = models.User(
        username=username,
        hashed_password=get_password_hash(password),
        role=models.UserRole.CONDUCTOR
    )
    db.add(new_user)
    await db.flush()  # get new_user.id

    # Create ConductorProfile
    profile = models.ConductorProfile(
        user_id=new_user.id,
        center_id=center_id,
        exam_id=center.exam_id,
        display_name=display_name,
        device_pin=device_pin
    )
    db.add(profile)
    await db.commit()
    return {"status": "success", "message": f"Conductor '{username}' created for center '{center.name}'"}

@app.delete("/api/admin/conductors/{conductor_id}")
async def delete_conductor(conductor_id: int, db: AsyncSession = Depends(get_db)):
    """Remove a conductor profile and their user account."""
    res = await db.execute(select(models.ConductorProfile).where(models.ConductorProfile.id == conductor_id))
    profile = res.scalars().first()
    if not profile:
        raise HTTPException(404, "Conductor not found")
    user_id = profile.user_id
    await db.delete(profile)
    user_res = await db.execute(select(models.User).where(models.User.id == user_id))
    user = user_res.scalars().first()
    if user:
        await db.delete(user)
    await db.commit()
    return {"status": "success"}

# ========= ROUTES: Conductor Dashboard APIs =========

async def _get_conductor_profile(request: Request, db: AsyncSession):
    """Helper: resolve the logged-in conductor's profile from JWT cookie."""
    token = request.cookies.get("access_token")
    if not token or not token.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    token_str = token.split("Bearer ")[1]
    
    from dependencies import SECRET_KEY, ALGORITHM
    import jwt
    from jwt.exceptions import PyJWTError as JWTError
    try:
        payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
    except JWTError:
        raise HTTPException(401, "Invalid token")

    user_res = await db.execute(select(models.User).where(models.User.username == username))
    user = user_res.scalars().first()
    if not user or user.role != models.UserRole.CONDUCTOR:
        raise HTTPException(403, "Conductor access only")

    profile_res = await db.execute(
        select(models.ConductorProfile)
        .where(models.ConductorProfile.user_id == user.id)
    )
    profile = profile_res.scalars().first()
    if not profile:
        raise HTTPException(404, "No center assigned to this conductor")
    return profile

@app.get("/api/conductor/my_center")
async def conductor_my_center(request: Request, db: AsyncSession = Depends(get_db)):
    """Returns the conductor's center details, exam info, and current gate/completion status."""
    from sqlalchemy.orm import selectinload
    profile = await _get_conductor_profile(request, db)

    center_res = await db.execute(select(models.Center).options(selectinload(models.Center.sessions)).where(models.Center.id == profile.center_id))
    center = center_res.scalars().first()

    exam_res = await db.execute(select(models.Exam).where(models.Exam.id == profile.exam_id))
    exam = exam_res.scalars().first()

    # Count attendance across all sessions of this center
    from sqlalchemy import func
    session_ids = [s.id for s in center.sessions]
    total_enrolled = 0
    total_attended = 0
    if session_ids:
        t_res = await db.execute(select(func.count()).select_from(models.ExamEnrollment).where(models.ExamEnrollment.exam_session_id.in_(session_ids)))
        total_enrolled = t_res.scalar() or 0
        a_res = await db.execute(select(func.count()).select_from(models.ExamEnrollment).where(models.ExamEnrollment.exam_session_id.in_(session_ids), models.ExamEnrollment.attendance_marked == True))
        total_attended = a_res.scalar() or 0

    return {
        "center": {"id": center.id, "name": center.name, "city": center.city, "state": center.state},
        "exam": {"id": exam.id, "name": exam.name, "gate_open": exam.gate_open, "is_complete": exam.is_complete},
        "stats": {"total_enrolled": total_enrolled, "total_attended": total_attended, "pending": total_enrolled - total_attended}
    }

@app.post("/api/conductor/gate/open")
async def conductor_open_gate(request: Request, db: AsyncSession = Depends(get_db)):
    """Open the gate for the conductor's assigned exam."""
    profile = await _get_conductor_profile(request, db)
    exam_res = await db.execute(select(models.Exam).where(models.Exam.id == profile.exam_id))
    exam = exam_res.scalars().first()
    if not exam:
        raise HTTPException(404, "Exam not found")
    exam.gate_open = True
    await db.commit()
    return {"status": "success", "gate_open": True}

@app.post("/api/conductor/gate/close")
async def conductor_close_gate(request: Request, db: AsyncSession = Depends(get_db)):
    """Close the gate for the conductor's assigned exam."""
    profile = await _get_conductor_profile(request, db)
    exam_res = await db.execute(select(models.Exam).where(models.Exam.id == profile.exam_id))
    exam = exam_res.scalars().first()
    if not exam:
        raise HTTPException(404, "Exam not found")
    exam.gate_open = False
    await db.commit()
    return {"status": "success", "gate_open": False}

@app.post("/api/conductor/mark_exam_complete")
async def conductor_mark_complete(request: Request, db: AsyncSession = Depends(get_db)):
    """Mark the exam as complete, close gate, and freeze attendance."""
    profile = await _get_conductor_profile(request, db)
    exam_res = await db.execute(select(models.Exam).where(models.Exam.id == profile.exam_id))
    exam = exam_res.scalars().first()
    if not exam:
        raise HTTPException(404, "Exam not found")
    exam.gate_open = False
    exam.is_complete = True
    await db.commit()
    return {"status": "success", "is_complete": True}

@app.get("/api/conductor/attendance")
async def conductor_attendance(request: Request, db: AsyncSession = Depends(get_db)):
    """Returns the full attendance list for all sessions in the conductor's center."""
    from sqlalchemy.orm import selectinload
    profile = await _get_conductor_profile(request, db)

    center_res = await db.execute(select(models.Center).options(selectinload(models.Center.sessions)).where(models.Center.id == profile.center_id))
    center = center_res.scalars().first()
    session_ids = [s.id for s in center.sessions]

    if not session_ids:
        return []

    enroll_res = await db.execute(
        select(models.ExamEnrollment)
        .options(selectinload(models.ExamEnrollment.student), selectinload(models.ExamEnrollment.exam_session))
        .where(models.ExamEnrollment.exam_session_id.in_(session_ids))
        .order_by(models.ExamEnrollment.attendance_time.desc())
    )
    enrollments = enroll_res.scalars().all()

    return [
        {
            "reg_no": e.enrolled_reg_no,
            "name": e.student.name if e.student else "Unknown",
            "session": f"{e.exam_session.date} {e.exam_session.session_time}" if e.exam_session else "—",
            "attendance_marked": e.attendance_marked,
            "attendance_time": e.attendance_time or "—",
        }
        for e in enrollments
    ]

@app.get("/api/conductor/export_package")
async def conductor_export_package(request: Request, db: AsyncSession = Depends(get_db)):
    """Conductor downloads the exam package for their specific center only."""
    import hmac
    import hashlib
    from datetime import datetime
    from sqlalchemy.orm import selectinload
    from fastapi.responses import Response as FR

    try:
        profile = await _get_conductor_profile(request, db)

        exam_res = await db.execute(select(models.Exam).where(models.Exam.id == profile.exam_id))
        exam = exam_res.scalars().first()
        if not exam:
            raise HTTPException(404, "Exam not found")

        center_res = await db.execute(
            select(models.Center)
            .options(selectinload(models.Center.sessions))
            .where(models.Center.id == profile.center_id)
        )
        center = center_res.scalars().first()
        session_ids = [s.id for s in center.sessions]

        candidates_res = await db.execute(
            select(models.PreApprovedCandidate)
            .options(selectinload(models.PreApprovedCandidate.exam_session))
            .where(models.PreApprovedCandidate.exam_session_id.in_(session_ids))
        )
        candidates = candidates_res.scalars().all()

        candidate_list = []
        for c in candidates:
            enroll_res = await db.execute(
                select(models.ExamEnrollment)
                .options(selectinload(models.ExamEnrollment.student))
                .where(models.ExamEnrollment.exam_id == profile.exam_id, models.ExamEnrollment.enrolled_reg_no == c.registration_no)
            )
            enrollment = enroll_res.scalars().first()
            embedding = None
            if enrollment and enrollment.student and enrollment.student.face_embedding:
                embedding = enrollment.student.face_embedding
            sess = c.exam_session
            candidate_list.append({
                "reg_no": c.registration_no, "name": c.name, "email": c.email or "",
                "session_id": c.exam_session_id,
                "session_label": f"{sess.date} {sess.session_time}" if sess else "Unknown",
                "center_name": center.name, "city": center.city, "state": center.state,
                "face_embedding": embedding
            })

        payload = {"exam_id": profile.exam_id, "exam_name": exam.name, "device_pin": profile.device_pin or "1234", "exported_at": datetime.now().isoformat(), "candidates": candidate_list}
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        sig = hmac.new(EDGE_HMAC_SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()
        signed_package = {"signature": sig, "payload": payload}

        return FR(
            content=json.dumps(signed_package, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=center_{profile.center_id}.digipack"}
        )
    except Exception as e:
        import traceback
        err_str = traceback.format_exc()
        return FR(content=err_str, status_code=500, media_type="text/plain")


# ========= ROUTES: Mobile App API Gateway =========
# Pure REST endpoints designed for the Flutter student mobile application.
# Auth: These endpoints expect `Authorization: Bearer <token>` header (not cookies).

async def _get_mobile_student(request: Request, db: AsyncSession):
    """Helper: Extract student from Authorization header JWT."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token_str = auth.split("Bearer ")[1]
    from dependencies import SECRET_KEY, ALGORITHM
    import jwt
    try:
        payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    res = await db.execute(select(models.Student).where(models.Student.username == username))
    student = res.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found")
    return student


@app.get("/api/mobile/me")
async def mobile_get_profile(request: Request, db: AsyncSession = Depends(get_db)):
    """Returns the full student profile + all enrollments for the logged-in mobile user."""
    from sqlalchemy.orm import selectinload
    
    # Extract username from header token first
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token_str = auth.split("Bearer ")[1]
    from dependencies import SECRET_KEY, ALGORITHM
    import jwt
    try:
        payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    res = await db.execute(
        select(models.Student)
        .options(
            selectinload(models.Student.enrollments).selectinload(models.ExamEnrollment.exam),
            selectinload(models.Student.enrollments).selectinload(models.ExamEnrollment.exam_session).selectinload(models.ExamSession.center)
        )
        .where(models.Student.username == username)
    )
    student = res.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found")

    enrollments_data = []
    for e in student.enrollments:
        center_details = None
        if e.exam_session and e.exam_session.center:
            c = e.exam_session.center
            center_details = {"name": c.name, "city": c.city, "state": c.state}

        enrollments_data.append({
            "enrollment_id": e.id,
            "exam_id": e.exam_id,
            "exam_name": e.exam.name if e.exam else "Unknown",
            "status": e.status,
            "reg_no": e.enrolled_reg_no,
            "date": e.exam_session.date if e.exam_session else "TBD",
            "time": e.exam_session.session_time if e.exam_session else "TBD",
            "center": center_details,
            "attendance_marked": e.attendance_marked,
            "attendance_time": e.attendance_time
        })

    return {
        "id": student.id,
        "username": student.username,
        "name": student.name,
        "email": student.email,
        "phone": student.phone_number,
        "kyc_verified": student.kyc_verified,
        "has_face": bool(student.face_embedding),
        "status": student.status.value if student.status else "APPROVED",
        "enrollments": enrollments_data
    }


@app.get("/api/mobile/exams")
async def mobile_list_open_exams(db: AsyncSession = Depends(get_db)):
    """Lists all currently open exams with their centers and sessions for the student app."""
    from sqlalchemy.orm import selectinload
    res = await db.execute(
        select(models.Exam)
        .options(selectinload(models.Exam.centers).selectinload(models.Center.sessions))
        .where(models.Exam.is_open == True)
    )
    exams = res.scalars().all()
    out = []
    for e in exams:
        centers = []
        for c in e.centers:
            sessions = [{"id": s.id, "date": s.date, "session_time": s.session_time, "capacity": s.capacity} for s in c.sessions]
            centers.append({"id": c.id, "state": c.state, "city": c.city, "name": c.name, "sessions": sessions})
        out.append({
            "id": e.id,
            "name": e.name,
            "verification_mode": getattr(e, "verification_mode", "FACE_ONLY"),
            "registration_form_config": e.registration_form_config or [],
            "centers": centers
        })
    return out


@app.get("/api/mobile/exams/{exam_id}/sessions")
async def mobile_get_exam_sessions(exam_id: int, db: AsyncSession = Depends(get_db)):
    """Returns all centers and their sessions (with availability) for a specific exam."""
    from sqlalchemy.orm import selectinload
    from sqlalchemy import func
    res = await db.execute(
        select(models.Center)
        .options(selectinload(models.Center.sessions))
        .where(models.Center.exam_id == exam_id)
    )
    centers = res.scalars().all()
    out = []
    for c in centers:
        s_list = []
        for s in c.sessions:
            res_count = await db.execute(
                select(func.count(models.ExamEnrollment.id)).where(
                    models.ExamEnrollment.exam_session_id == s.id,
                    models.ExamEnrollment.status == "APPROVED"
                )
            )
            used = res_count.scalar()
            s_list.append({
                "id": s.id, "date": s.date, "session_time": s.session_time,
                "capacity": s.capacity, "used": used, "available": max(0, s.capacity - used)
            })
        out.append({"id": c.id, "state": c.state, "city": c.city, "name": c.name, "sessions": s_list})
    return out


@app.get("/api/mobile/exams/{exam_id}/can_enroll")
async def mobile_check_can_enroll(exam_id: int, db: AsyncSession = Depends(get_db)):
    """Check if any roster candidates exist for this exam (prerequisite for enrollment)."""
    res = await db.execute(
        select(models.PreApprovedCandidate).where(models.PreApprovedCandidate.exam_id == exam_id).limit(1)
    )
    has_roster = res.scalars().first() is not None
    return {"can_enroll": has_roster}


@app.post("/api/mobile/exams/{exam_id}/enroll")
@limiter.limit("10/minute")
async def mobile_enroll_student(
    request: Request,
    exam_id: int,
    reg_no: str = Form(...),
    session_id: int = Form(None),
    db: AsyncSession = Depends(get_db)
):
    """Enrolls a student in an exam using their reg_no. Auth via Bearer header."""
    student = await _get_mobile_student(request, db)

    # Check roster
    res_roster = await db.execute(select(models.PreApprovedCandidate).where(
        models.PreApprovedCandidate.exam_id == exam_id,
        models.PreApprovedCandidate.registration_no == reg_no
    ))
    roster_entry = res_roster.scalars().first()
    if not roster_entry:
        raise HTTPException(status_code=403, detail="Registration Number not authorized for this exam.")

    # Identity cross-check
    s_name = (student.name or "").strip().lower()
    s_email = (student.email or "").strip().lower()
    r_name = (roster_entry.name or "").strip().lower()
    r_email = (roster_entry.email or "").strip().lower()
    match_email = (s_email and r_email and s_email == r_email)
    match_name = (s_name and r_name and s_name == r_name)
    if not (match_email or match_name):
        raise HTTPException(status_code=403, detail="Identity Mismatch: Your profile details do not match the assigned candidate.")

    # Session cross-check
    if session_id and roster_entry.exam_session_id != session_id:
        raise HTTPException(status_code=403, detail="Session mismatch! Selected session does not match your assigned session.")

    # Capture dynamic form data
    form_data = await request.form()
    dynamic_payload = {k: v for k, v in form_data.items() if k not in ("reg_no", "session_id")}

    enrollment = models.ExamEnrollment(
        student_id=student.id,
        exam_id=exam_id,
        exam_session_id=session_id or roster_entry.exam_session_id,
        enrolled_reg_no=reg_no,
        status="APPROVED",
        dynamic_data=json.dumps(dynamic_payload)
    )
    db.add(enrollment)
    await db.commit()
    return {"status": "success", "message": "Enrollment approved!", "enrollment_id": enrollment.id}


@app.post("/api/mobile/upload_passport")
@limiter.limit("5/minute")
async def mobile_upload_passport(
    request: Request,
    passport_photo: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """Upload or update the passport photo for the logged-in student."""
    student = await _get_mobile_student(request, db)
    contents = await passport_photo.read()
    if len(contents) > 500 * 1024:
        raise HTTPException(status_code=400, detail="Passport photo exceeds 500KB limit.")
    os.makedirs("uploads_profile_pics", exist_ok=True)
    path = f"uploads_profile_pics/{student.username}_passport.jpg"
    with open(path, "wb") as f:
        f.write(contents)
    return {"status": "success", "message": "Passport photo uploaded."}


@app.get("/api/mobile/hallticket/{exam_id}/{registration_no}")
async def mobile_download_hallticket(exam_id: int, registration_no: str, db: AsyncSession = Depends(get_db)):
    """Downloads the Admit Card PDF for a specific enrollment. Reuses the existing PDF generator."""
    return await generate_pdf_hallticket(exam_id, registration_no, db)


@app.post("/api/mobile/update_embedding")
@limiter.limit("5/minute")
async def mobile_update_embedding(
    request: Request,
    embedding_json: str = Form(None),
    photo: UploadFile = File(None),
    db: AsyncSession = Depends(get_db)
):
    """Update the face embedding for the logged-in student (re-capture scenario)."""
    student = await _get_mobile_student(request, db)

    if embedding_json:
        try:
            embedding = json.loads(embedding_json)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid embedding_json format.")
    elif photo:
        contents = await photo.read()
        embedding = get_face_embedding(contents)
    else:
        raise HTTPException(status_code=400, detail="Must provide either photo or embedding_json.")

    if len(embedding) == 0:
        raise HTTPException(status_code=400, detail="No face detected.")

    student.face_embedding = json.dumps(embedding)
    student.kyc_verified = True
    await db.commit()
    return {"status": "success", "message": "Face embedding updated successfully."}

