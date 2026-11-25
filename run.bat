@echo off
REM 本地一键：生成一期日报并起一个本地预览服务
REM 用法：run.bat          （正式生成，需要 DEEPSEEK_API_KEY）
REM       run.bat mock     （演练，不需要 Key）
setlocal
cd /d "%~dp0"

if /i "%~1"=="mock" (
  python -m brief build --mock --force
) else (
  python -m brief build --offline --force
)

if errorlevel 1 (
  echo.
  echo 生成失败，请检查上面的日志。
  pause
  exit /b 1
)

echo.
echo 本地预览： http://localhost:8000  （Ctrl+C 结束）
python -m http.server 8000 -d site
endlocal