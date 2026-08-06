@echo off
cd /d "%~dp0"
echo Building Commission PDF...
python "7. Presentation/build_commission_pack.py" --year 2026 --output "Commission.pdf" %*
echo Done.
pause
