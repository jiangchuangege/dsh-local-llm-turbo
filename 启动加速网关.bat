@echo off
chcp 65001 >nul
title 本地模型加速网关 (DSH)
cd /d "%~dp0"
echo [提示] 启动本地模型加速网关: 8081 -> 8080 (流式 + 防退化)...
echo [提示] 关闭本窗口即停止网关。
python local_llm_gateway.py --port 8081 --upstream http://127.0.0.1:8080 --max-tools 4 --repeat-penalty 1.2
pause
