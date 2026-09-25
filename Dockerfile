FROM python:3.10-slim

# 安装 Docker CLI 和基础工具，以便在容器内管理宿主机 Docker
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    dnsutils && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py config.py database.py dns_service.py docker_service.py firewall_service.py \
     music_bot_service.py port_manager.py rate_limit.py requirements.txt ./
COPY templates ./templates
COPY static ./static

EXPOSE 12345

# 健康检查：命中公开状态接口（返回 200 说明服务进程与 HTTP 监听正常）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:12345/api/dns-info > /dev/null || exit 1

CMD ["python", "app.py"]
