# TeamSpeak CDK 自动化开通与端口防冲突管理系统

基于 Python FastAPI + Docker Compose 构建的 TeamSpeak 3 自动化分发开通平台。用户在网页端（默认端口 `12345`）输入 CDK 后，系统将自动分配独立端口、在 `/data/teamspeak/ts<N>` 目录下生成专属 `docker-compose.yml` 并通过 Docker 启动实例。

---

## 🌟 系统核心特性

1. **TeamSpeak 语音服务器 CDK 自动化开通**：
   - **分段自增规则**：
     - `ts1`：`60001` (Voice), `20001` (File), `30001` (Query), `40001` (TSDNS)
     - `ts2`：`60002` (Voice), `20002` (File), `30002` (Query), `40002` (TSDNS)
     - `tsN`：`60000 + N`, `20000 + N`, `30000 + N`, `40000 + N`
   - **空号自动回收与填补**：销毁旧实例后，自动补上空缺编号与端口，零端口浪费。
   - **全自动凭据提取**：首启自动提取客户端管理员 Token (`admin_token`) 与 ServerQuery 超级管理员账号密码 (`serveradmin` / `password` / `apikey`)。

2. **TeamSpeak 音乐机器人（Music Bot）CDK 兑换与自动化对接**：
   - **多档位时长控制**：支持生成 1 个月（月卡）、3 个月（季卡）、6 个月（半年卡）、12 个月（年卡）及永久卡 CDK。
   - **远程平台自动接入**：直接对接音乐机器人服务中心（`http://103.71.69.156:23467/`），用户兑换时仅需填写目标 TS 地址与端口，即可全自动创建实例并启动进入指定语音房间。
   - **双端管理与控制**：支持在平台内一键启动、停止、重启机器人，并提供直达网页点歌/播放控制台的一键链接。

3. **双端界面与批量导出**：
   - **用户端**（`http://IP:12345/`）：自适应识别 TS 服务器卡密与音乐机器人卡密，智能引导开通。
   - **管理员后台**（`http://IP:12345/admin`）：
     - 管理密码：由 `.env` 中的 `ADMIN_PASSWORD` 指定（不再内置弱默认值；未配置时启动日志会打印一次性随机口令）
     - TS 服务器集群与音乐机器人实例双列表实时监控与控制
     - 批量生成、TXT 导出（未使用/全部）、一键复制

---

## 🚀 快速部署与运行

### 方式一：Linux 宿主机直接运行（推荐）

1. **环境准备**：
   确保服务器已安装 Python 3.8+ 及 Docker / Docker Compose：
   ```bash
   docker --version
   docker compose version
   ```

2. **克隆/进入目录**：
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

# 管理后台密码（必填；不填会生成一次性随机口令并打印到启动日志，重启即失效）
ADMIN_PASSWORD=please-change-me

# 数据与 compose 根目录（Linux 默认为 /data/teamspeak）
TS_DATA_DIR=/data/teamspeak

# TS 服务器对外公网 IP 或域名（强烈建议显式配置，留空会退化为使用请求 Host 头）
PUBLIC_SERVER_IP=123.45.67.89

# 是否信任反向代理下发的 X-Forwarded-For / X-Forwarded-Host
# 默认 0（不信任）。只有部署在自建可信反代之后才设为 1，
# 否则客户端可伪造请求头绕过体验卡 IP 防刷并影响对外地址判定。
TRUST_PROXY_HEADERS=0

# 端口分段基础（ts1 -> 60001 / 20001 / 30001 / 40001）
BASE_VOICE_PORT=60000
BASE_FILE_PORT=20000
BASE_QUERY_PORT=30000
BASE_TSDNS_PORT=40000

# 自动防火墙预放行端口段宽度（运行期会随实际最大实例号自动扩展）
FIREWALL_PORT_SPAN=200
```

> **安全提示**：管理后台默认口令已取消内置弱值；首次部署请务必在 `.env` 中设置强口令，
> 登录后也可在后台【修改密码】随时更换（改密会立即吊销所有历史登录会话）。

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
