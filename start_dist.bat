@echo off
set HARNESS_PORT=8088
cd /d "%~dp0"
python server_dev.py > server.log 2> server.err
