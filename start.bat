@echo off
chcp 65001 > nul
echo === TeamSpeak CDK 自动开通管理系统 (Windows 启动脚本) ===

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8 并配置环境变量
    pause
    exit /b
)

if not exist venv (
    echo [*] 创建虚拟环境 venv...
    python -m venv venv
)

call venv\Scripts\activate.bat

rem 仅在依赖清单变化时重新安装，避免每次启动都联网拉取
if not exist "venv\.requirements.sha256" goto install
findstr /v /c:"" "requirements.txt" > "%TEMP%\req_now.txt" 2>nul
fc "venv\.requirements.sha256" "%TEMP%\req_now.txt" >nul 2>&1
if errorlevel 1 goto install
echo [*] 依赖无变化，跳过安装
goto run

:install
echo [*] 安装依赖...
pip install -r requirements.txt
findstr /v /c:"" "requirements.txt" > "venv\.requirements.sha256" 2>nul

:run
if not exist .env (
    echo [!] 未检测到 .env 文件，服务将生成一次性随机管理员口令并打印到控制台。
    echo [!] 建议复制 .env.example 为 .env 并设置 ADMIN_PASSWORD 后再启动。
)

echo [*] 正在启动管理服务，默认监听 12345 端口...
python app.py
pause
