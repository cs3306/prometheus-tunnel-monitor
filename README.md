# prometheus-tunnel-monitor

**[中文](#中文)** | **[English](#english)**

---

<a name="中文"></a>
# 中文

基于 **Prometheus + Grafana + Telegram Bot** 的多节点服务器监控系统，通过**反向 SSH 隧道**接入远程客户端，无需客户端开放任何公网端口。

## 功能

- Telegram Bot 实时查看所有节点状态（CPU、内存、磁盘、负载、网速）
- 流量统计与计费周期管理，支持月流量限额
- 总览页和详情页均 5 秒自动刷新，所有节点数据并行获取
- Alertmanager 告警推送（节点离线、CPU/内存/磁盘过高）
- Grafana 仪表盘可视化

## 架构

```
┌─────────────────────────────────────┐
│  中控节点                            │
│                                     │
│  Prometheus   127.0.0.1:36602       │
│  Alertmanager 127.0.0.1:36603       │
│  Grafana      127.0.0.1:36601       │
│  TG Bot       127.0.0.1:36605       │
│                                     │
│  Nginx → monitor.example.com        │
└──────────────┬──────────────────────┘
               │ 反向 SSH 隧道
     ┌─────────┴──────────┐
     │      客户端节点      │
     │   node_exporter    │
     │   + autossh        │
     └────────────────────┘
```

## 项目结构

```
prometheus-tunnel-monitor/
├── docker-compose.yml
├── .env.example
├── prometheus/
│   ├── prometheus.yml.example
│   └── alert_rules.yml
├── alertmanager/
│   └── alertmanager.yml.example
├── tgbot/
│   ├── bot.py
│   ├── Dockerfile
│   ├── requirements.txt
│   └── billing.json.example
└── client/
    ├── monitoring-tunnel.service
    └── node_exporter.service
```

## 中控节点部署

### 1. 克隆项目

```bash
git clone https://github.com/yourname/prometheus-tunnel-monitor.git /docker/monitoring
cd /docker/monitoring
```

### 2. 创建配置文件

```bash
cp .env.example .env
nano .env

cp prometheus/prometheus.yml.example prometheus/prometheus.yml
cp alertmanager/alertmanager.yml.example alertmanager/alertmanager.yml
cp tgbot/billing.json.example tgbot/billing.json
```

`.env` 说明：

| 变量 | 说明 |
|------|------|
| `TG_BOT_TOKEN` | Telegram Bot Token，从 [@BotFather](https://t.me/BotFather) 获取 |
| `TG_CHAT_ID` | 接收消息的 Chat ID，从 [@userinfobot](https://t.me/userinfobot) 获取 |
| `GF_ADMIN_PASSWORD` | Grafana 管理员密码 |

### 3. 创建隧道专用用户

```bash
useradd -r -s /usr/sbin/nologin -m -d /home/montunnel montunnel
mkdir -p /home/montunnel/.ssh
chmod 700 /home/montunnel/.ssh
touch /home/montunnel/.ssh/authorized_keys
chmod 600 /home/montunnel/.ssh/authorized_keys
chown -R montunnel:montunnel /home/montunnel/.ssh
```

在 `/etc/ssh/sshd_config` 末尾追加：

```
Match User montunnel
    AllowTcpForwarding yes
    X11Forwarding no
    AllowAgentForwarding no
    ForceCommand /bin/false
    GatewayPorts clientspecified
```

```bash
sshd -t && systemctl reload sshd
```

### 4. 启动服务

```bash
docker network create devtools_shared
docker compose build tgbot
docker compose up -d
```

验证：

```bash
curl -s http://127.0.0.1:36602/prometheus/-/healthy
curl -s http://127.0.0.1:36603/alertmanager/-/healthy
curl -s http://127.0.0.1:36601/grafana/api/health
```

### 5. 配置 Nginx 反代

```nginx
server {
    listen 443 ssl http2;
    server_name monitor.example.com;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/cert.key;

    location /grafana/ {
        proxy_pass http://127.0.0.1:36601/grafana/;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    location /prometheus/ {
        proxy_pass http://127.0.0.1:36602/prometheus/;
        proxy_set_header Host $host;
    }

    location /alertmanager/ {
        proxy_pass http://127.0.0.1:36603/alertmanager/;
        proxy_set_header Host $host;
    }
}
```

### 6. 开放隧道端口给容器

Prometheus 容器需要通过 `host-gateway` 访问宿主机上的隧道端口，需要 iptables 放行：

```bash
# 本机 node_exporter
iptables -I INPUT 1 -p tcp --dport 9100 -s 172.16.0.0/12 -j ACCEPT

# 每台客户端隧道端口加一行
# iptables -I INPUT 1 -p tcp --dport 19100 -s 172.16.0.0/12 -j ACCEPT
```

持久化：

```bash
cat > /etc/network/if-up.d/monitoring-iptables << 'EOFRULES'
#!/bin/sh
iptables -I INPUT 1 -p tcp --dport 9100 -s 172.16.0.0/12 -j ACCEPT
EOFRULES
chmod +x /etc/network/if-up.d/monitoring-iptables
```

## 客户端节点部署

### 1. 安装依赖

```bash
apt install -y autossh
```

### 2. 安装 node_exporter

```bash
cd /tmp
curl -s https://api.github.com/repos/prometheus/node_exporter/releases/latest \
  | grep browser_download_url | grep linux-amd64 | cut -d'"' -f4 | wget -i -
tar xzf node_exporter-*.tar.gz
mv node_exporter-*/node_exporter /usr/local/bin/
```

> ARM 架构将 `linux-amd64` 替换为 `linux-arm64`

```bash
cp client/node_exporter.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now node_exporter
```

### 3. 生成密钥并配置隧道

```bash
mkdir -p /opt/monitoring/ssh
ssh-keygen -t ed25519 -f /opt/monitoring/ssh/tunnel_key -N ""
ssh-keyscan -p YOUR_SSH_PORT YOUR_SERVER_IP > /opt/monitoring/ssh/known_hosts
cat /opt/monitoring/ssh/tunnel_key.pub  # 复制此内容到中控节点
```

编辑 `client/monitoring-tunnel.service`，替换 `YOUR_SSH_PORT`、`YOUR_SERVER_IP`、`TUNNEL_PORT`（每台客户端分配唯一端口），复制到 `/etc/systemd/system/`：

```bash
systemctl daemon-reload && systemctl enable monitoring-tunnel
# 先不启动，等中控节点添加公钥后再启动
```

### 4. 在中控节点添加公钥

```bash
echo 'restrict,port-forwarding,command="/bin/false" ssh-ed25519 AAAA...公钥... user@client' \
  >> /home/montunnel/.ssh/authorized_keys
```

### 5. 启动并验证

客户端：

```bash
systemctl start monitoring-tunnel
```

中控节点验证：

```bash
netstat -tlnp | grep TUNNEL_PORT
curl -s http://127.0.0.1:TUNNEL_PORT/metrics | head -3
```

### 6. 注册到 Prometheus

编辑 `prometheus/prometheus.yml`：

```yaml
  - job_name: 'client-name'
    static_configs:
      - targets: ['host-gateway:TUNNEL_PORT']
        labels:
          instance: 'client-name'  # 必须和 billing.json 的 key 一致
```

```bash
docker compose kill -s SIGHUP prometheus  # 热重载，无需重启
```

## 配置文件说明

### prometheus.yml

`host-gateway` 是固定写法，Docker 会将其解析为宿主机 IP。每台客户端分配唯一隧道端口。

### billing.json

```json
{
  "instance-name": {
    "billing_day": 1,
    "limit_gb": 1024,
    "interfaces": ["eth0"]
  }
}
```

| 字段 | 说明 |
|------|------|
| `billing_day` | 每月流量重置日（1-28） |
| `limit_gb` | 月流量上限 GB，不填表示无限制 |
| `interfaces` | 统计哪些网卡，路由器通常需要 `["br-lan", "eth0"]` |

> 修改后**实时生效**，无需重启 bot。

### alertmanager.yml

Webhook 地址固定为 `http://tgbot:8000/alertmanager`，无需修改。

## Telegram Bot 命令

| 命令 | 说明 |
|------|------|
| `/status` | 所有节点总览，5 秒自动刷新 |
| `/vps` | 选择节点查看详情，5 秒自动刷新 |
| `/traffic` | 流量统计 |
| `/help` | 帮助 |

## 告警规则

| 告警 | 触发条件 | 持续时间 |
|------|----------|----------|
| `InstanceDown` | 节点无响应 | 2 分钟 |
| `HighCPU` | CPU > 85% | 5 分钟 |
| `HighMemory` | 内存 > 90% | 5 分钟 |
| `DiskAlmostFull` | 磁盘 > 85% | 10 分钟 |

## SSH 登录通知

有人登录任意节点时，Telegram Bot 自动推送通知。

### 标准 Linux 客户端（Debian/Ubuntu/CentOS）

**① 添加隧道端口转发**

在 `monitoring-tunnel.service` 的 `-N \` 后面加一行：

```
    -L 127.0.0.1:36605:127.0.0.1:36605 \
```

```bash
systemctl daemon-reload && systemctl restart monitoring-tunnel
```

**② 安装脚本和 PAM 配置**

```bash
cp notify-login.sh /opt/monitoring/notify-login.sh
chmod +x /opt/monitoring/notify-login.sh
echo 'session optional pam_exec.so /opt/monitoring/notify-login.sh' >> /etc/pam.d/sshd
```

**③ 验证**

```bash
PAM_TYPE=open_session PAM_USER=root PAM_RHOST=1.2.3.4 \
  SSH_CONNECTION="1.2.3.4 12345 0.0.0.0 34521" \
  bash /opt/monitoring/notify-login.sh
```

### ImmortalWrt / OpenWrt

**① 添加隧道端口转发**

在 `/etc/init.d/monitoring-tunnel` 的 `-R` 行后面加：

```
        -L 127.0.0.1:36605:127.0.0.1:36605 \
```

```bash
/etc/init.d/monitoring-tunnel restart
```

**② 安装脚本**

```bash
cp notify-login-openwrt.sh /root/monitoring/notify-login.sh
chmod +x /root/monitoring/notify-login.sh
echo '. /root/monitoring/notify-login.sh' >> /etc/profile
```

**③ 验证**

```bash
SSH_CLIENT="1.2.3.4 12345 22" sh /root/monitoring/notify-login.sh
```

### 中控节点

中控节点 bot 在本机运行，不需要隧道，直接装脚本：

```bash
cp notify-login.sh /opt/monitoring/notify-login.sh
chmod +x /opt/monitoring/notify-login.sh
echo 'session optional pam_exec.so /opt/monitoring/notify-login.sh' >> /etc/pam.d/sshd
```

## 故障排查

**隧道没建立**

```bash
journalctl -u monitoring-tunnel -n 20

# 手动测试连接，正常输出为 "This account is currently not available."
ssh -i /opt/monitoring/ssh/tunnel_key -p YOUR_SSH_PORT \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=/opt/monitoring/ssh/known_hosts \
    montunnel@YOUR_SERVER_IP
```

**Prometheus targets 显示 down**

```bash
netstat -tlnp | grep TUNNEL_PORT
docker exec prometheus wget -qO- http://host-gateway:TUNNEL_PORT/metrics | head -3
iptables -L INPUT -n | grep TUNNEL_PORT
```

**Bot 无响应**

```bash
docker logs tgbot --tail 30
```

**Alertmanager 反复重启**

```bash
docker logs alertmanager --tail 10
# 常见原因：--web.external-url 必须是完整 URL，不能只写路径
```

---

<a name="english"></a>
# English

A multi-node server monitoring system based on **Prometheus + Grafana + Telegram Bot**. Remote clients connect via **reverse SSH tunnels** — no inbound ports required on clients.

## Features

- Real-time status for all nodes (CPU, memory, disk, load, network speed) via Telegram Bot
- Traffic statistics with billing cycle management and monthly quota support
- 5-second auto-refresh on both overview and detail pages, all nodes fetched in parallel
- Alertmanager push alerts (node down, high CPU/memory/disk)
- Grafana dashboard visualization

## Architecture

```
┌─────────────────────────────────────┐
│  Control Node                       │
│                                     │
│  Prometheus   127.0.0.1:36602       │
│  Alertmanager 127.0.0.1:36603       │
│  Grafana      127.0.0.1:36601       │
│  TG Bot       127.0.0.1:36605       │
│                                     │
│  Nginx → monitor.example.com        │
└──────────────┬──────────────────────┘
               │ Reverse SSH Tunnel
     ┌─────────┴──────────┐
     │   Client Nodes      │
     │   node_exporter     │
     │   + autossh         │
     └────────────────────┘
```

## Project Structure

```
prometheus-tunnel-monitor/
├── docker-compose.yml
├── .env.example
├── prometheus/
│   ├── prometheus.yml.example
│   └── alert_rules.yml
├── alertmanager/
│   └── alertmanager.yml.example
├── tgbot/
│   ├── bot.py
│   ├── Dockerfile
│   ├── requirements.txt
│   └── billing.json.example
└── client/
    ├── monitoring-tunnel.service
    └── node_exporter.service
```

## Control Node Setup

### 1. Clone the Repository

```bash
git clone https://github.com/yourname/prometheus-tunnel-monitor.git /docker/monitoring
cd /docker/monitoring
```

### 2. Create Config Files

```bash
cp .env.example .env
nano .env

cp prometheus/prometheus.yml.example prometheus/prometheus.yml
cp alertmanager/alertmanager.yml.example alertmanager/alertmanager.yml
cp tgbot/billing.json.example tgbot/billing.json
```

`.env` variables:

| Variable | Description |
|----------|-------------|
| `TG_BOT_TOKEN` | Telegram Bot Token from [@BotFather](https://t.me/BotFather) |
| `TG_CHAT_ID` | Target Chat ID from [@userinfobot](https://t.me/userinfobot) |
| `GF_ADMIN_PASSWORD` | Grafana admin password |

### 3. Create Tunnel User

```bash
useradd -r -s /usr/sbin/nologin -m -d /home/montunnel montunnel
mkdir -p /home/montunnel/.ssh
chmod 700 /home/montunnel/.ssh
touch /home/montunnel/.ssh/authorized_keys
chmod 600 /home/montunnel/.ssh/authorized_keys
chown -R montunnel:montunnel /home/montunnel/.ssh
```

Append to `/etc/ssh/sshd_config`:

```
Match User montunnel
    AllowTcpForwarding yes
    X11Forwarding no
    AllowAgentForwarding no
    ForceCommand /bin/false
    GatewayPorts clientspecified
```

```bash
sshd -t && systemctl reload sshd
```

### 4. Start Services

```bash
docker network create devtools_shared
docker compose build tgbot
docker compose up -d
```

Verify:

```bash
curl -s http://127.0.0.1:36602/prometheus/-/healthy
curl -s http://127.0.0.1:36603/alertmanager/-/healthy
curl -s http://127.0.0.1:36601/grafana/api/health
```

### 5. Nginx Reverse Proxy

```nginx
server {
    listen 443 ssl http2;
    server_name monitor.example.com;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/cert.key;

    location /grafana/ {
        proxy_pass http://127.0.0.1:36601/grafana/;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    location /prometheus/ {
        proxy_pass http://127.0.0.1:36602/prometheus/;
        proxy_set_header Host $host;
    }

    location /alertmanager/ {
        proxy_pass http://127.0.0.1:36603/alertmanager/;
        proxy_set_header Host $host;
    }
}
```

### 6. Allow Container Access to Tunnel Ports

```bash
iptables -I INPUT 1 -p tcp --dport 9100 -s 172.16.0.0/12 -j ACCEPT
# Add one line per client tunnel port
# iptables -I INPUT 1 -p tcp --dport 19100 -s 172.16.0.0/12 -j ACCEPT
```

Persist across reboots:

```bash
cat > /etc/network/if-up.d/monitoring-iptables << 'EOFRULES'
#!/bin/sh
iptables -I INPUT 1 -p tcp --dport 9100 -s 172.16.0.0/12 -j ACCEPT
EOFRULES
chmod +x /etc/network/if-up.d/monitoring-iptables
```

## Client Node Setup

### 1. Install Dependencies

```bash
apt install -y autossh
```

### 2. Install node_exporter

```bash
cd /tmp
curl -s https://api.github.com/repos/prometheus/node_exporter/releases/latest \
  | grep browser_download_url | grep linux-amd64 | cut -d'"' -f4 | wget -i -
tar xzf node_exporter-*.tar.gz
mv node_exporter-*/node_exporter /usr/local/bin/
```

> For ARM (aarch64), replace `linux-amd64` with `linux-arm64`

```bash
cp client/node_exporter.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now node_exporter
```

### 3. Generate SSH Key and Configure Tunnel

```bash
mkdir -p /opt/monitoring/ssh
ssh-keygen -t ed25519 -f /opt/monitoring/ssh/tunnel_key -N ""
ssh-keyscan -p YOUR_SSH_PORT YOUR_SERVER_IP > /opt/monitoring/ssh/known_hosts
cat /opt/monitoring/ssh/tunnel_key.pub  # Copy this to control node
```

Edit `client/monitoring-tunnel.service`, replace `YOUR_SSH_PORT`, `YOUR_SERVER_IP`, `TUNNEL_PORT` (unique per client), then:

```bash
cp client/monitoring-tunnel.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable monitoring-tunnel
# Don't start yet — add public key to control node first
```

### 4. Add Public Key on Control Node

```bash
echo 'restrict,port-forwarding,command="/bin/false" ssh-ed25519 AAAA...pubkey... user@client' \
  >> /home/montunnel/.ssh/authorized_keys
```

### 5. Start and Verify

On client:

```bash
systemctl start monitoring-tunnel
```

On control node:

```bash
netstat -tlnp | grep TUNNEL_PORT
curl -s http://127.0.0.1:TUNNEL_PORT/metrics | head -3
```

### 6. Register in Prometheus

Edit `prometheus/prometheus.yml`:

```yaml
  - job_name: 'client-name'
    static_configs:
      - targets: ['host-gateway:TUNNEL_PORT']
        labels:
          instance: 'client-name'  # Must match key in billing.json
```

```bash
docker compose kill -s SIGHUP prometheus  # Hot-reload, no restart needed
```

## Configuration Reference

### billing.json

```json
{
  "instance-name": {
    "billing_day": 1,
    "limit_gb": 1024,
    "interfaces": ["eth0"]
  }
}
```

| Field | Description |
|-------|-------------|
| `billing_day` | Monthly traffic reset day (1–28) |
| `limit_gb` | Monthly quota in GB; omit for unlimited |
| `interfaces` | Interfaces to monitor; routers typically need `["br-lan", "eth0"]` |

> Changes take effect **immediately** without restarting the bot.

### alertmanager.yml

Webhook URL is fixed as `http://tgbot:8000/alertmanager`. No changes needed.

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/status` | All nodes overview, auto-refreshes every 5s |
| `/vps` | Select node for detail view, auto-refreshes every 5s |
| `/traffic` | Traffic usage overview |
| `/help` | Show help |

## Alert Rules

| Alert | Condition | Duration |
|-------|-----------|----------|
| `InstanceDown` | Node unreachable | 2 minutes |
| `HighCPU` | CPU > 85% | 5 minutes |
| `HighMemory` | Memory > 90% | 5 minutes |
| `DiskAlmostFull` | Disk > 85% | 10 minutes |

## SSH Login Notification

When anyone logs into any monitored node, the Telegram Bot sends an automatic push notification.

### Standard Linux Client (Debian/Ubuntu/CentOS)

**① Add tunnel port forwarding**

Add one line after `-N \` in `monitoring-tunnel.service`:

```
    -L 127.0.0.1:36605:127.0.0.1:36605 \
```

```bash
systemctl daemon-reload && systemctl restart monitoring-tunnel
```

**② Install script and PAM config**

```bash
cp notify-login.sh /opt/monitoring/notify-login.sh
chmod +x /opt/monitoring/notify-login.sh
echo 'session optional pam_exec.so /opt/monitoring/notify-login.sh' >> /etc/pam.d/sshd
```

**③ Verify**

```bash
PAM_TYPE=open_session PAM_USER=root PAM_RHOST=1.2.3.4 \
  SSH_CONNECTION="1.2.3.4 12345 0.0.0.0 34521" \
  bash /opt/monitoring/notify-login.sh
```

### ImmortalWrt / OpenWrt

**① Add tunnel port forwarding**

Add after the `-R` line in `/etc/init.d/monitoring-tunnel`:

```
        -L 127.0.0.1:36605:127.0.0.1:36605 \
```

```bash
/etc/init.d/monitoring-tunnel restart
```

**② Install script**

```bash
cp notify-login-openwrt.sh /root/monitoring/notify-login.sh
chmod +x /root/monitoring/notify-login.sh
echo '. /root/monitoring/notify-login.sh' >> /etc/profile
```

**③ Verify**

```bash
SSH_CLIENT="1.2.3.4 12345 22" sh /root/monitoring/notify-login.sh
```

### Control Node

The bot runs locally — no tunnel needed. Install directly:

```bash
cp notify-login.sh /opt/monitoring/notify-login.sh
chmod +x /opt/monitoring/notify-login.sh
echo 'session optional pam_exec.so /opt/monitoring/notify-login.sh' >> /etc/pam.d/sshd
```

## Troubleshooting

**Tunnel not connecting**

```bash
journalctl -u monitoring-tunnel -n 20

# Manual test — expected output: "This account is currently not available."
ssh -i /opt/monitoring/ssh/tunnel_key -p YOUR_SSH_PORT \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=/opt/monitoring/ssh/known_hosts \
    montunnel@YOUR_SERVER_IP
```

**Prometheus targets showing down**

```bash
netstat -tlnp | grep TUNNEL_PORT
docker exec prometheus wget -qO- http://host-gateway:TUNNEL_PORT/metrics | head -3
iptables -L INPUT -n | grep TUNNEL_PORT
```

**Bot not responding**

```bash
docker logs tgbot --tail 30
```

**Alertmanager crash-looping**

```bash
docker logs alertmanager --tail 10
# Common cause: --web.external-url must be a full URL, not just a path
```

## License

[MIT](LICENSE)
