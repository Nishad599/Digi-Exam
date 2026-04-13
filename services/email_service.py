import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import os

# Configure your real SMTP credentials here! 
# Gmail requires setting up an "App Password" (2FA must be on).
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.environ.get("SMTP_EMAIL", "rahulkharote89@gmail.com")
SENDER_PASSWORD = os.environ.get("SMTP_PASSWORD", "srgaeskhifmcoszu")

def send_hall_ticket_email(to_email: str, student_name: str, exam_id: int, exam_name: str, reg_no: str, center: str, date: str, time: str, qr_bytes: bytes):
    """Sends an actual email with the QR code attached using SMTP."""
    if SENDER_EMAIL == "your_email@gmail.com":
        print(f"[SMTP ERROR] Cannot send real email to {to_email}. Please configure SENDER_EMAIL and SENDER_PASSWORD in services/email_service.py")
        return False
        
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = to_email
        msg['Subject'] = f"Official Hall Ticket QR Code - {exam_name}"
        
        body = f"""Hello {student_name},

You have been successfully authorized for {exam_name}.

--- EXAM DETAILS ---
Registration No:  {reg_no}
Exam Center:      {center}
Date:             {date}
Session Time:     {time}

--- DOS AND DON'TS ---
• DO arrive at the center at least 30 minutes before the session starts.
• DO bring your original government-issued ID card.
• DO ensure your facial profile is clearly visible.
• DON'T bring any electronic devices (phones, smartwatches, calculators).
• DON'T wear masks, sunglasses, or hats during biometric verification.

Please find your official Hall Ticket QR Code attached to this email. You must upload this exact QR code image on your Student dashboard to formally complete your enrollment.

Best of luck,
Admin Team"""
        msg.attach(MIMEText(body, 'plain'))
        
        image_attachment = MIMEImage(qr_bytes, name="hall_ticket_qr.png")
        msg.attach(image_attachment)
        
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        
        print(f"[SMTP SUCCESS] ✅ Real email sent to {to_email}")
        return True
    except Exception as e:
        print(f"[SMTP FAILURE] Failed to send email to {to_email}: {e}")
        return False


def send_otp_email(to_email: str, otp_code: str) -> bool:
    """Sends a 6-digit OTP code for password reset via SMTP."""
    if SENDER_EMAIL == "your_email@gmail.com":
        print(f"[SMTP ERROR] Cannot send OTP to {to_email}. SMTP not configured.")
        return False

    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = to_email
        msg['Subject'] = "Digi-Exam — Password Reset OTP"

        body = f"""Hello,

Your password reset OTP is:

    {otp_code}

This code is valid for 5 minutes. Do not share it with anyone.

If you did not request a password reset, please ignore this email.

— Digi-Exam Admin Team"""
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()

        print(f"[SMTP SUCCESS] OTP email sent to {to_email}")
        return True
    except Exception as e:
        print(f"[SMTP FAILURE] Failed to send OTP to {to_email}: {e}")
        return False
