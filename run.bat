cd D:\WORK\LUMEN
$env:PYTHONPATH='src'
uvicorn lumen.main:app --app-dir src --host 0.0.0.0 --port 8010