import json
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Enum, Text, JSON
from sqlalchemy.orm import relationship
import enum
from datetime import datetime, timezone
from database import Base

class VerificationMode(str, enum.Enum):
    QR_ONLY = "QR_ONLY"
    FACE_ONLY = "FACE_ONLY"
    DUAL_AUTH = "DUAL_AUTH"

class UserRole(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    CONDUCTOR = "CONDUCTOR"
    GATE_DEVICE = "GATE_DEVICE"

class StudentStatus(str, enum.Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    ENROLLMENT_PENDING = "ENROLLMENT_PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(Enum(UserRole), default=UserRole.ADMIN)

class Exam(Base):
    __tablename__ = "exams"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    verification_mode = Column(Enum(VerificationMode), default=VerificationMode.FACE_ONLY)
    face_match_threshold = Column(String, default="0.85")
    registration_form_config = Column(JSON, nullable=True) # Dynamic Fields Schema
    is_open = Column(Boolean, default=True) # Used for Student Dashboard visibility
    gate_open = Column(Boolean, default=False) # Used for Gate Terminal (Allow entry)
    is_complete = Column(Boolean, default=False) # Set by Conductor after exam ends
    
    # Relationships
    centers = relationship("Center", back_populates="exam", cascade="all, delete-orphan")
    pre_approved = relationship("PreApprovedCandidate", back_populates="exam")
    enrollments = relationship("ExamEnrollment", back_populates="exam")
    students = relationship("Student", back_populates="exam") # Legacy global, we will use enrollments now, keeping for compatibility
    conductors = relationship("ConductorProfile", back_populates="exam", cascade="all, delete-orphan")

class Center(Base):
    __tablename__ = "centers"
    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id"))
    state = Column(String)
    city = Column(String)
    name = Column(String)
    
    exam = relationship("Exam", back_populates="centers")
    sessions = relationship("ExamSession", back_populates="center", cascade="all, delete-orphan")
    conductors = relationship("ConductorProfile", back_populates="center", cascade="all, delete-orphan")


class ExamSession(Base):
    __tablename__ = "exam_sessions"
    id = Column(Integer, primary_key=True, index=True)
    center_id = Column(Integer, ForeignKey("centers.id"))
    date = Column(String)
    session_time = Column(String)
    capacity = Column(Integer, default=50)
    
    center = relationship("Center", back_populates="sessions")
    pre_approved = relationship("PreApprovedCandidate", back_populates="exam_session", cascade="all, delete-orphan")
    enrollments = relationship("ExamEnrollment", back_populates="exam_session", cascade="all, delete-orphan")

class PreApprovedCandidate(Base):
    """Data uploaded by Admin representing allowed students for an exam."""
    __tablename__ = "pre_approved_candidates"
    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id"))
    exam_session_id = Column(Integer, ForeignKey("exam_sessions.id"), nullable=True) # Seat mapping
    registration_no = Column(String, index=True) # Expected QR payload e.g. "JEE-1001"
    name = Column(String)
    email = Column(String, nullable=True) # Added for automated Hall Ticket dispatch
    
    exam = relationship("Exam", back_populates="pre_approved")
    exam_session = relationship("ExamSession", back_populates="pre_approved")

class Student(Base):
    """Universal Profile for a Candidate App"""
    __tablename__ = "students"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True) # Added for student login
    hashed_password = Column(String)                   # Added for student login
    name = Column(String)
    email = Column(String, unique=True, index=True, nullable=True)
    phone_number = Column(String, nullable=True)
    
    # Global Face Embedding
    face_embedding = Column(Text, nullable=True) 
    kyc_verified = Column(Boolean, default=False)
    
    # Legacy fields (kept for older views)
    registration_no = Column(String, unique=True, index=True, nullable=True)
    exam_id = Column(Integer, ForeignKey("exams.id"), nullable=True)
    dynamic_data = Column(JSON, nullable=True)
    encrypted_docs = Column(JSON, nullable=True)
    status = Column(Enum(StudentStatus), default=StudentStatus.APPROVED)
    rejection_reason = Column(String, nullable=True)
    
    # Relationships
    exam = relationship("Exam", back_populates="students")
    enrollments = relationship("ExamEnrollment", back_populates="student")
    gate_logs = relationship("GateLog", back_populates="student")

class ExamEnrollment(Base):
    """An authorized claim linking a student to an exam."""
    __tablename__ = "exam_enrollments"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"))
    exam_id = Column(Integer, ForeignKey("exams.id"))
    exam_session_id = Column(Integer, ForeignKey("exam_sessions.id"), nullable=True)
    enrolled_reg_no = Column(String, index=True)
    status = Column(String, default="APPROVED") 
    attendance_marked = Column(Boolean, default=False)
    attendance_time = Column(String, nullable=True) # ISO format time
    dynamic_data = Column(JSON, nullable=True) # Form answers for this specific exam
    
    student = relationship("Student", back_populates="enrollments")
    exam = relationship("Exam", back_populates="enrollments")
    exam_session = relationship("ExamSession", back_populates="enrollments")

class GateLog(Base):
    __tablename__ = "gate_logs"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"))
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    status = Column(String) # SUCCESS, FAILURE, FALLBACK
    method = Column(String) # QR, FACE, MANUAL
    confidence_score = Column(String, nullable=True)

    student = relationship("Student", back_populates="gate_logs")

class ConductorProfile(Base):
    """Links a User (role=CONDUCTOR) to a specific Center and Exam."""
    __tablename__ = "conductor_profiles"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    center_id = Column(Integer, ForeignKey("centers.id"))
    exam_id = Column(Integer, ForeignKey("exams.id"))
    display_name = Column(String)
    device_pin = Column(String, default="1234")  # PIN for Edge Terminal login, set by Admin

    user = relationship("User")
    center = relationship("Center", back_populates="conductors")
    exam = relationship("Exam", back_populates="conductors")
