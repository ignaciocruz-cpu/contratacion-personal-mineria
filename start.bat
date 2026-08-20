@echo off
echo Iniciando Plataforma Reclutamiento Flesan Mineria...
cd /d "%~dp0"
"C:\Users\ignacio.cruz\AppData\Local\Programs\Python\Python312\python.exe" -m pip install -r requirements.txt -q
"C:\Users\ignacio.cruz\AppData\Local\Programs\Python\Python312\python.exe" api.py
pause
