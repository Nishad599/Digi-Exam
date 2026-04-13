@echo off
echo Starting Digi-Exam PoC...
call .\venv\Scripts\activate.bat
echo Make sure you have installed requirements via 'pip install -r requirements.txt'
uvicorn main:app --reload --host 0.0.0.0 --port 8081
