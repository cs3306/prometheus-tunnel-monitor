# server-monitor

基于 Prometheus + Grafana + Telegram Bot 的多节点服务器监控系统，通过反向 SSH 隧道接入远程客户端，无需客户端开放任何公网端口。

A multi-node server monitoring system based on Prometheus + Grafana + Telegram Bot. Remote clients connect via reverse SSH tunnels — no inbound ports required on clients.

---

## 功能 / Features

- Telegram Bot 实时查看所有节点状态（CPU、内存、磁盘、负载、网速）
- 流量统计与计费周期管理，支持月流量限额和双向流量
- VPS 详情页 5 秒自动刷新
- 总览页 5 秒自动刷新，所有节点数据并行获取
- Alertmanager 告警推送（节点离线、CPU/内存/磁盘过高）
- Grafana 仪表盘可视化

---

## 架构 / Architecture

```
┌─────────────────────────────────┐
│  中控节点 (Control Node)         │
│                                 │
│  Prometheus  127.0.0.1:36602    │
│  Alertmanager 127.0.0.1:36603   │
│  Grafana     127.0.0.1:36601    │
│  TG Bot      127.0.0.1:36605    │
│                                 │
│  Nginx → monitor.example.com    │
└────────────┬────────────────────┘
             │ 反向 SSH 隧道
    ┌────────┴────────┐
    │   客户端节点     │
    │  node_exporter  │
    │  + autossh      │
    └─────────────────┘
```

---

## 快速部署 / Quick Start

### 中控节点 / Control Node

**1. 克隆项目**

```bash
git clone https://github.com/yourname/server-monitor.git /docker/monitoring
cd /docker/monitoring
```

**2. 创建配置文件**

```bash
cp .env.example .env
nano .env  # 填入 TG_BOT_TOKEN、TG_CHAT_ID、GF_ADMIN_PASSWORD

cp prometheus/prometheus.yml.example prometheus/prometheus.yml
cp alertmanager/alertmanager.yml.example alertmanager/alertmanager.yml
cp tgbot/billing.json.example tgbot/billing.json
```

**3. 创建 montunnel 用户**

```bash
useradd -r -s /usr/sbin/nologin -m -d /home/montunnel montunnel
mkdir -p /home/montunnel/.ssh
chmod 700 /home/montunnel/.ssh
touch /home/montunnel/.ssh/authorized_keys
chmod 600 /home/montunnel/.ssh/authorized_keys
chown -R montunnel:montunnel /home/montunnel/.ssh
```

在 `/etc/ssh/sshd_config` 末尾加：

```
Match User montunnel
    AllowTcpForwarding yes
    X11Forwarding no
    AllowAgentForwarding no
    ForceCommand /bin/false
    GatewayPorts clientspecified
```

**4. 创建 Docker 网络并启动**

```bash
docker network create devtools_shared
docker compose build tgbot
docker compose up -d
```

**5. 允许 Docker 容器访问宿主机隧道端口**

```bash
# 每台客户端的隧道端口都需要加
iptables -I INPUT 1 -p tcp --dport TUNNEL_PORT -s 172.16.0.0/12 -j ACCEPT
```

持久化（写入开机脚本）：

```bash
cat > /etc/network/if-up.d/monitoring-iptables << 'EOF'
#!/bin/sh
iptables -I INPUT 1 -p tcp --dport 9100 -s 172.16.0.0/12 -j ACCEPT
# 每台客户端一行
# iptables -I INPUT 1 -p tcp --dport 19100 -s 172.16.0.0/12 -j ACCEPT
EOF
chmod +x /etc/network/if-up.d/monitoring-iptables
```

---

### 客户端节点 / Client Node

**1. 安装依赖**

```bash
apt install -y autossh
```

**2. 安装 node_exporter**

```bash
curl -s https://api.github.com/repos/prometheus/node_exporter/releases/latest \
  | grep browser_download_url | grep linux-amd64 | cut -d'"' -f4 | wget -i -
tar xzf node_exporter-*.tar.gz
mv node_exporter-*/node_exporter /usr/local/bin/
```

复制 `client/node_exporter.service` 到 `/etc/systemd/system/` 并启动：

```bash
systemctl daemon-reload && systemctl enable --now node_exporter
```

**3. 配置隧道**

```bash
mkdir -p /opt/monitoring/ssh
ssh-keygen -t ed25519 -f /opt/monitoring/ssh/tunnel_key -N ""
ssh-keyscan -p YOUR_SSH_PORT YOUR_SERVER_IP > /opt/monitoring/ssh/known_hosts
```

编辑 `client/monitoring-tunnel.service`，填入 `YOUR_SSH_PORT`、`TUNNEL_PORT`、`YOUR_SERVER_IP`，复制到 `/etc/systemd/system/`：

```bash
systemctl daemon-reload && systemctl enable --now monitoring-tunnel
```

**4. 在中控节点添加公钥**

```bash
echo 'restrict,port-forwarding,command="/bin/false" ssh-ed25519 AAAA... user@client' \
  >> /home/montunnel/.ssh/authorized_keys
```

**5. 在 prometheus.yml 添加 target**

```yaml
  - job_name: 'client-name'
    static_configs:
      - targets: ['host-gateway:TUNNEL_PORT']
        labels:
          instance: 'client-name'
```

```bash
docker compose kill -s SIGHUP prometheus
```

---

## billing.json 说明

```json
{
  "instance-name": {
    "billing_day": 1,        // 每月计费重置日
    "limit_gb": 1024,        // 月流量上限 GB（不填表示无限制）
    "interfaces": ["eth0"]   // 统计哪些网卡
  }
}
```

---

## Telegram Bot 命令

| 命令 | 说明 |
|------|------|
| `/status` | 所有节点总览（5秒自动刷新） |
| `/vps` | 选择节点查看详情（5秒自动刷新） |
| `/traffic` | 流量统计 |
| `/help` | 帮助 |
