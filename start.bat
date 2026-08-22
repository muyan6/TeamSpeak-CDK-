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

echo [*] 安装依赖...
call venv\Scripts\activate.bat
pip install -r requirements.txt

echo [*] 正在启动管理服务，默认监听 12345 端口...
python app.py
pause
