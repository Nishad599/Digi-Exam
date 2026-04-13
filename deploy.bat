@echo off
echo Starting Deploy...

git add -A
set /p msg="Commit message: "
git commit -m "%msg%"
git push origin main

echo Done! Check: https://github.com/Nishad599/Digi-Exam/actions
pause