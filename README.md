# TeamSpeak CDK 自动化开通与端口防冲突管理系统

基于 Python FastAPI + Docker Compose 构建的 TeamSpeak 3 自动化分发开通平台。用户在网页端（默认端口 `12345`）输入 CDK 后，系统将自动分配独立端口、在 `/data/teamspeak/ts<N>` 目录下生成专属 `docker-compose.yml` 并通过 Docker 启动实例。系统还会自动抓取首启生成的管理员权限秘钥（ServerAdmin Token），让用户开箱即用。

---

## 🌟 系统核心特性

1. **大容量分段端口分配与自动防冲突**：
   - **分段自增规则**：
     - `ts1`：`60001` (Voice), `20001` (File), `30001` (Query), `40001` (TSDNS)
     - `ts2`：`60002` (Voice), `20002` (File), `30002` (Query), `40002` (TSDNS)
     - `tsN`：`60000 + N`, `20000 + N`, `30000 + N`, `40000 + N`
   - **空号自动回收与填补**：当后台销毁 `ts2` 之后，下一个兑换的用户会自动补上 `ts2` 编号与对应的全部端口，零端口浪费。
2. **标准 Docker Compose 编排生成**：
   - 自动在 `/data/teamspeak/ts<N>` 目录下创建 `docker-compose.yml`：
     ```yaml
     version: '3.8'
     services:
       teamspeak2:
         image: teamspeak:latest
         container_name: ts-teamspeak-2
         restart: always
         environment:
           - TS3SERVER_LICENSE=accept
         ports:
           - "9988:9987/udp"    # 语音服务 (已避开 9987)
           - "30034:30033"      # 文件传输 (已避开 30033)
           - "10012:10011"      # 服务器查询 raw (已避开 10011)
           - "41145:41144"      # DNS域名解析（可选，已避开 41144）
         volumes:
           - ./data:/var/ts3server
     ```
3. **管理员权限秘钥 (Token) 自动捕获**：
   - 实例启动后自动异步分析容器日志，抓取 `token=...` 管理员秘钥并展示在前端，支持一键复制。
4. **双端界面**：
   - **用户端**（`http://IP:12345/`）：输入 CDK 快速开通，支持一键复制连接串、管理员秘钥，支持唤起客户端。
   - **管理员后台**（`http://IP:12345/admin`）：
     - 默认管理密码：`admin123456`
     - 批量生成、导出、删除 CDK
     - 实时监控所有 TS 容器状态（启动、停止、重启、彻底销毁）
     - 在线查看容器实时 Docker 日志与端口分布

---

## 🚀 快速部署与运行

### 方式一：Linux 宿主机直接运行（推荐）

1. **环境准备**：
   确保服务器已安装 Python 3.8+ 及 Docker / Docker Compose：
   ```bash
   # 测试 docker 是否正常可用
   docker --version
   docker compose version
   ```

2. **克隆/解压本项目并进入目录**：
   ```bash
   cd Teamspeak
   ```

3. **一键启动**：
   ```bash
   chmod +x start.sh
   ./start.sh
   ```
   或者后台运行：
   ```bash
   nohup ./start.sh > /var/log/ts_manager.log 2>&1 &
   ```

4. **通过 systemd 开机自启（可选）**：
   创建 `/etc/systemd/system/teamspeak-manager.service`：
   ```ini
   [Unit]
   Description=TeamSpeak CDK Manager Service
   After=network.target docker.service

   [Service]
   Type=simple
   User=root
   WorkingDirectory=/path/to/Teamspeak
   ExecStart=/usr/bin/python3 /path/to/Teamspeak/app.py
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```
   启用并启动：
   ```bash
   systemctl daemon-reload
   systemctl enable teamspeak-manager
   systemctl start teamspeak-manager
   ```

---

### 方式二：Docker 容器化运行管理平台

本平台自身也可以通过 Docker 运行（通过挂载 Docker 套接字调度宿主机 Docker）：

```bash
docker compose up -d
```

---

### 方式三：Windows 环境本地测试

双击运行 `start.bat` 即可快速拉起服务，默认将在 `./data/teamspeak` 创建测试目录并监听 `http://localhost:12345`。

---

## ⚙️ 配置文件说明 (`.env`)

可通过根目录下的 `.env` 文件定制系统参数（如不存在可复制 `.env.example`）：

```ini
# 服务监听配置
SERVER_HOST=0.0.0.0
SERVER_PORT=12345

# 管理后台密码
ADMIN_PASSWORD=admin123456

# 数据与 compose 根目录（Linux 默认为 /data/teamspeak）
TS_DATA_DIR=/data/teamspeak

# TS 服务器对外公网 IP 或域名（留空则根据用户访问地址自动提取）
PUBLIC_SERVER_IP=123.45.67.89

# 官方基础占用端口（避让起始点）
BASE_VOICE_PORT=9987
BASE_FILE_PORT=30033
BASE_QUERY_PORT=10011
BASE_TSDNS_PORT=41144
```

---

## 📖 使用操作手册

### 1. 管理员生成 CDK
1. 访问 `http://服务器IP:12345/admin`
2. 输入管理员密码（默认 `admin123456`）
3. 切换至 **“CDK 激活码管理”** 选项卡
4. 输入生成数量（例如 `5`）与备注信息，点击 **“立即生成”**
5. 点击 **“批量复制未使用 CDK”** 发送给玩家/客户

### 2. 用户兑换开通服务器
1. 用户访问 `http://服务器IP:12345`
2. 输入管理员发放的 CDK（例如 `TS-A7B2-C9D1`）
3. 点击 **“立即开通 / 查询”**
4. 系统将在数秒内创建 `/data/teamspeak/ts<N>`，运行 Docker 并返回：
   - 语音连接地址：`IP:端口`
   - 管理员权限秘钥：`token=xxxx`
5. 用户在 TeamSpeak 客户端连接该地址，弹窗提示时粘贴秘钥，即可成为服务器超级管理员！
6. 用户后续如需再次查看，只需在网页重新输入同一个 CDK 即可查回所有信息。
