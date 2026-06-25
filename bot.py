import os, json, logging, asyncio, calendar
from datetime import datetime, timezone
from aiohttp import web, ClientSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tgbot")

TOKEN    = os.environ["TG_BOT_TOKEN"]
CHAT_ID  = os.environ["TG_CHAT_ID"]
PROM_URL = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:36602/prometheus")
TG_API   = f"https://api.telegram.org/bot{TOKEN}"
BILLING_FILE = os.environ.get("BILLING_FILE", "/app/billing.json")

# ── Billing config ─────────────────────────────────────────
def load_billing() -> dict:
    try:
        with open(BILLING_FILE) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"billing.json load failed: {e}")
        return {}

def get_billing_cycle(billing_day: int):
    now = datetime.now(timezone.utc)
    y, m, d = now.year, now.month, now.day
    if d >= billing_day:
        start = now.replace(day=billing_day, hour=0, minute=0, second=0, microsecond=0)
        nm = m + 1 if m < 12 else 1
        ny = y if m < 12 else y + 1
        end = now.replace(year=ny, month=nm, day=billing_day, hour=0, minute=0, second=0, microsecond=0)
    else:
        pm = m - 1 if m > 1 else 12
        py = y if m > 1 else y - 1
        start = now.replace(year=py, month=pm, day=billing_day, hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(day=billing_day, hour=0, minute=0, second=0, microsecond=0)
    elapsed_s = (now - start).total_seconds()
    total_s   = (end - start).total_seconds()
    days_left = (end - now).days + 1
    return start, end, elapsed_s, total_s, days_left

# ── Telegram helpers ───────────────────────────────────────
async def tg_send(text: str, markup: dict = None) -> int | None:
    payload = {"chat_id": CHAT_ID, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    async with ClientSession() as s:
        r = await s.post(f"{TG_API}/sendMessage", json=payload)
        data = await r.json()
        return data.get("result", {}).get("message_id")

async def tg_edit(chat_id, msg_id, text: str, markup: dict = None):
    payload = {"chat_id": chat_id, "message_id": msg_id,
               "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    async with ClientSession() as s:
        await s.post(f"{TG_API}/editMessageText", json=payload)

async def tg_answer(cb_id: str, text: str = ""):
    async with ClientSession() as s:
        await s.post(f"{TG_API}/answerCallbackQuery",
                     json={"callback_query_id": cb_id, "text": text})

# ── PromQL helpers ─────────────────────────────────────────
async def prom_query(q: str) -> list:
    try:
        async with ClientSession() as s:
            async with s.get(f"{PROM_URL}/api/v1/query",
                             params={"query": q}, timeout=10) as r:
                data = await r.json()
                return data.get("data", {}).get("result", [])
    except Exception as e:
        log.error(f"PromQL error [{q}]: {e}")
        return []

def val(results, instance, default=None):
    for r in results:
        if r["metric"].get("instance") == instance:
            try: return float(r["value"][1])
            except: return default
    return default

def fmt_bytes(b):
    if b is None: return "N/A"
    for u in ['B','KB','MB','GB','TB']:
        if abs(b) < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"

def fmt_bps(b):
    if b is None: return "N/A"
    for u in ['B/s','KB/s','MB/s','GB/s']:
        if abs(b) < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB/s"

def bar(pct, width=15):
    pct = max(0, min(100, pct or 0))
    filled = round(pct / 100 * width)
    empty  = width - filled
    if pct >= 85:   fill, empty_c = "🟥", "⬜"
    elif pct >= 60: fill, empty_c = "🟨", "⬜"
    else:           fill, empty_c = "🟩", "⬜"
    return fill * filled + empty_c * empty + f" {pct:.1f}%"

# ── Prometheus data fetch ──────────────────────────────────
async def fetch_all_instances() -> list:
    results = await prom_query('up{job!="prometheus"}')
    seen, instances = set(), []
    for r in results:
        inst = r["metric"].get("instance")
        if inst and inst not in seen:
            seen.add(inst)
            instances.append(inst)
    return sorted(instances)

async def fetch_metrics(instances: list) -> dict:
    """Fetch all needed metrics in parallel."""
    queries = {
        "up":     'up',
        "cpu":    '100 - (avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m]))*100)',
        "mem":    '(1-(node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes))*100',
        "mem_used": 'node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes',
        "mem_total": 'node_memory_MemTotal_bytes',
        "disk":   '(1-(node_filesystem_avail_bytes{mountpoint="/",fstype!="rootfs"}/node_filesystem_size_bytes{mountpoint="/",fstype!="rootfs"}))*100',
        "disk_used": 'node_filesystem_size_bytes{mountpoint="/",fstype!="rootfs"} - node_filesystem_avail_bytes{mountpoint="/",fstype!="rootfs"}',
        "disk_total": 'node_filesystem_size_bytes{mountpoint="/",fstype!="rootfs"}',
        "load1":  'node_load1',
        "load5":  'node_load5',
        "load15": 'node_load15',
        "net_rx": 'rate(node_network_receive_bytes_total{device!~"lo|docker.*|veth.*|br.*"}[5m])',
        "net_tx": 'rate(node_network_transmit_bytes_total{device!~"lo|docker.*|veth.*|br.*"}[5m])',
        "uptime": 'node_time_seconds - node_boot_time_seconds',
    }
    results_list = await asyncio.gather(*[prom_query(q) for q in queries.values()])
    return dict(zip(queries.keys(), results_list))

# ── Message builders ───────────────────────────────────────
async def fetch_instance_traffic(inst: str, billing: dict):
    """Fetch traffic for one instance, returns (rx_total, tx_total, limit_gb, days_left)."""
    cfg = billing.get(inst, {})
    bd  = cfg.get("billing_day", 1)
    limit_gb = cfg.get("limit_gb")
    ifaces = cfg.get("interfaces", ["eth0"])
    iface_filter = "|".join(ifaces)
    _, _, elapsed_s, _, days_left = get_billing_cycle(bd)

    rx_res, tx_res = await asyncio.gather(
        prom_query(f'increase(node_network_receive_bytes_total{{instance="{inst}",device=~"{iface_filter}"}}[{int(elapsed_s)}s])'),
        prom_query(f'increase(node_network_transmit_bytes_total{{instance="{inst}",device=~"{iface_filter}"}}[{int(elapsed_s)}s])')
    )
    rx_total = sum(float(r["value"][1]) for r in rx_res if r["value"][1] not in ("NaN", "+Inf"))
    tx_total = sum(float(r["value"][1]) for r in tx_res if r["value"][1] not in ("NaN", "+Inf"))
    return rx_total, tx_total, limit_gb, days_left

async def build_overview() -> str:
    instances = await fetch_all_instances()
    if not instances:
        return "⚠️ <b>无监控目标</b>\n\n未发现任何 node_exporter 实例，请检查隧道连接。"

    billing = load_billing()
    # Fetch all metrics and all traffic in parallel
    m, traffic_results = await asyncio.gather(
        fetch_metrics(instances),
        asyncio.gather(*[fetch_instance_traffic(inst, billing) for inst in instances])
    )
    traffic_map = dict(zip(instances, traffic_results))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"🖥 <b>服务器总览</b>  <i>{now}</i>\n"]

    for inst in instances:
        is_up = val(m["up"], inst, 0) == 1.0
        if not is_up:
            lines.append(f"🔴 <b>{inst}</b> — 离线\n")
            continue

        cpu  = val(m["cpu"], inst)
        mem  = val(m["mem"], inst)
        disk = val(m["disk"], inst)
        load = val(m["load1"], inst)
        rx   = val(m["net_rx"], inst)
        tx   = val(m["net_tx"], inst)

        rx_total, tx_total, limit_gb, days_left = traffic_map[inst]
        total_gb = (rx_total + tx_total) / 1e9

        if limit_gb:
            pct = total_gb / limit_gb * 100
            traffic_bar  = f"  TRF  {bar(pct, 8)}"
            traffic_info = f"  TRF  已用{total_gb:.1f}GB/限{limit_gb}GB 剩{days_left}天"
        else:
            traffic_bar  = f"  TRF  ↓{fmt_bytes(rx_total)} ↑{fmt_bytes(tx_total)}"
            traffic_info = f"  TRF  剩余{days_left}天"

        load1 = val(m["load1"], inst)
        load5 = val(m.get("load5", []), inst)
        load15 = val(m.get("load15", []), inst)
        load_str = f"{load1:.2f}" if load1 is not None else "N/A"
        if load5 is not None: load_str += f" {load5:.2f}"
        if load15 is not None: load_str += f" {load15:.2f}"

        lines.append(
            f"🟢 <b>{inst}</b>\n"
            f"  CPU  {bar(cpu, 8)}\n"
            f"  MEM {bar(mem, 8)}\n"
            f"  DISK {bar(disk, 8)}\n"
            f"  LOAD {load_str}\n"
            f"{traffic_bar}\n"
            f"{traffic_info}\n"
            f"  NET  ↓{fmt_bps(rx)} ↑{fmt_bps(tx)}\n"
        )

    return "\n".join(lines)

def kb_overview() -> dict:
    return {"inline_keyboard": [[
        {"text": "🔄 刷新", "callback_data": "overview"},
        {"text": "🖥 选择 VPS", "callback_data": "menu"},
        {"text": "📦 流量", "callback_data": "traffic"},
    ]]}

async def kb_vps_list() -> dict:
    instances = await fetch_all_instances()
    up_results = await prom_query('up')
    def is_up(inst):
        return val(up_results, inst, 0) == 1.0
    rows = []
    for i in range(0, len(instances), 2):
        icon0 = "🟢" if is_up(instances[i]) else "🔴"
        row = [{"text": f"{icon0} {instances[i]}", "callback_data": f"vps:{instances[i]}"}]
        if i + 1 < len(instances):
            icon1 = "🟢" if is_up(instances[i+1]) else "🔴"
            row.append({"text": f"{icon1} {instances[i+1]}", "callback_data": f"vps:{instances[i+1]}"})
        rows.append(row)
    rows.append([{"text": "◀️ 总览", "callback_data": "overview"}])
    return {"inline_keyboard": rows}

async def build_vps_detail(inst: str) -> str:
    m = await fetch_metrics([inst])
    is_up = val(m["up"], inst, 0) == 1.0
    if not is_up:
        return f"🔴 <b>{inst}</b>\n\n主机离线或隧道断开。"

    cpu  = val(m["cpu"], inst)
    mem  = val(m["mem"], inst)
    mu   = val(m["mem_used"], inst)
    mt   = val(m["mem_total"], inst)
    disk = val(m["disk"], inst)
    du   = val(m["disk_used"], inst)
    dt   = val(m["disk_total"], inst)
    load = val(m["load1"], inst)
    rx   = val(m["net_rx"], inst)
    tx   = val(m["net_tx"], inst)
    upt  = val(m["uptime"], inst)

    upt_str = "N/A"
    if upt is not None:
        d = int(upt // 86400); h = int((upt % 86400) // 3600)
        upt_str = f"{d}天 {h}小时"

    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    return (
        f"🖥 <b>{inst}</b>  <i>{now}</i>\n\n"
        f"<b>CPU</b>\n{bar(cpu, 12)}\n  Load avg: {load:.2f}\n\n"
        f"<b>内存</b>\n{bar(mem, 12)}\n  {fmt_bytes(mu)} / {fmt_bytes(mt)}\n\n"
        f"<b>磁盘 (/)</b>\n{bar(disk, 12)}\n  {fmt_bytes(du)} / {fmt_bytes(dt)}\n\n"
        f"<b>网络实时</b>\n"
        f"  ↓ {fmt_bps(rx)}   ↑ {fmt_bps(tx)}\n\n"
        f"<b>运行时间</b>  {upt_str}"
    )

def kb_back(inst: str) -> dict:
    return {"inline_keyboard": [
        [
            {"text": "🔄 刷新", "callback_data": f"vps:{inst}"},
            {"text": "📦 流量", "callback_data": f"traffic:{inst}"},
        ],
        [
            {"text": "◀️ 返回列表", "callback_data": "menu"},
            {"text": "📊 总览", "callback_data": "overview"},
        ],
    ]}

async def build_traffic_overview() -> str:
    billing = load_billing()
    instances = await fetch_all_instances()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"📦 <b>流量总览</b>  <i>{now_str}</i>\n"]

    for inst in instances:
        cfg = billing.get(inst, {})
        bd  = cfg.get("billing_day", 1)
        limit_gb = cfg.get("limit_gb")
        ifaces = cfg.get("interfaces", ["eth0"])
        iface_filter = "|".join(ifaces)

        _, _, elapsed_s, total_s, days_left = get_billing_cycle(bd)

        rx_res = await prom_query(
            f'increase(node_network_receive_bytes_total{{instance="{inst}",device=~"{iface_filter}"}}[{int(elapsed_s)}s])'
        )
        tx_res = await prom_query(
            f'increase(node_network_transmit_bytes_total{{instance="{inst}",device=~"{iface_filter}"}}[{int(elapsed_s)}s])'
        )

        rx_total = sum(float(r["value"][1]) for r in rx_res if r["value"][1] not in ("NaN", "+Inf"))
        tx_total = sum(float(r["value"][1]) for r in tx_res if r["value"][1] not in ("NaN", "+Inf"))
        total_bytes = rx_total + tx_total
        total_gb = total_bytes / 1e9

        if limit_gb:
            pct = total_gb / limit_gb * 100
            proj = total_gb / (elapsed_s / total_s) if elapsed_s > 0 else 0
            lines.append(
                f"<b>{inst}</b>\n"
                f"  {bar(pct, 10)}\n"
                f"  已用 {total_gb:.1f} GB / {limit_gb} GB  剩余 {limit_gb - total_gb:.1f} GB\n"
                f"  预计月末: {proj:.1f} GB  剩余天数: {days_left}天\n"
            )
        else:
            lines.append(
                f"<b>{inst}</b>\n"
                f"  本周期: {fmt_bytes(total_bytes)}  (↓{fmt_bytes(rx_total)} ↑{fmt_bytes(tx_total)})\n"
                f"  剩余天数: {days_left}天\n"
            )

    if not instances:
        return "⚠️ 无监控目标"
    return "\n".join(lines)

def kb_traffic() -> dict:
    return {"inline_keyboard": [[
        {"text": "🔄 刷新", "callback_data": "traffic"},
        {"text": "◀️ 总览", "callback_data": "overview"},
    ]]}

async def build_traffic_detail(inst: str) -> str:
    billing = load_billing()
    cfg = billing.get(inst, {})
    bd  = cfg.get("billing_day", 1)
    limit_gb = cfg.get("limit_gb")
    ifaces = cfg.get("interfaces", ["eth0"])
    iface_filter = "|".join(ifaces)

    _, _, elapsed_s, total_s, days_left = get_billing_cycle(bd)

    rx_res = await prom_query(
        f'increase(node_network_receive_bytes_total{{instance="{inst}",device=~"{iface_filter}"}}[{int(elapsed_s)}s])'
    )
    tx_res = await prom_query(
        f'increase(node_network_transmit_bytes_total{{instance="{inst}",device=~"{iface_filter}"}}[{int(elapsed_s)}s])'
    )
    rx_rt = await prom_query(
        f'rate(node_network_receive_bytes_total{{instance="{inst}",device=~"{iface_filter}"}}[5m])'
    )
    tx_rt = await prom_query(
        f'rate(node_network_transmit_bytes_total{{instance="{inst}",device=~"{iface_filter}"}}[5m])'
    )

    rx_total = sum(float(r["value"][1]) for r in rx_res if r["value"][1] not in ("NaN", "+Inf"))
    tx_total = sum(float(r["value"][1]) for r in tx_res if r["value"][1] not in ("NaN", "+Inf"))
    rx_now   = sum(float(r["value"][1]) for r in rx_rt  if r["value"][1] not in ("NaN", "+Inf"))
    tx_now   = sum(float(r["value"][1]) for r in tx_rt  if r["value"][1] not in ("NaN", "+Inf"))

    total_bytes = rx_total + tx_total
    total_gb = total_bytes / 1e9
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")

    lines = [f"📦 <b>{inst} 流量详情</b>  <i>{now_str}</i>\n"]

    if limit_gb:
        pct  = total_gb / limit_gb * 100
        proj = total_gb / (elapsed_s / total_s) if elapsed_s > 0 else 0
        remaining = limit_gb - total_gb
        daily_budget = remaining / days_left if days_left > 0 else 0
        lines += [
            f"<b>本周期用量</b>\n{bar(pct, 12)}\n",
            f"  已用:   {total_gb:.2f} GB",
            f"  限额:   {limit_gb} GB",
            f"  剩余:   {remaining:.2f} GB",
            f"  每日预算: {daily_budget:.2f} GB/天",
            f"  月末预计: {proj:.1f} GB {'⚠️ 超额' if proj > limit_gb else '✅ 正常'}",
            "",
        ]
    else:
        lines += [f"<b>本周期用量</b>: {fmt_bytes(total_bytes)}\n"]

    lines += [
        f"<b>本周期收发</b>",
        f"  ↓ 下载: {fmt_bytes(rx_total)}",
        f"  ↑ 上传: {fmt_bytes(tx_total)}",
        "",
        f"<b>实时速率</b>",
        f"  ↓ {fmt_bps(rx_now)}   ↑ {fmt_bps(tx_now)}",
        "",
        f"  统计网卡: {', '.join(ifaces)}",
        f"  剩余天数: {days_left} 天",
    ]
    return "\n".join(lines)

def kb_traffic_detail(inst: str) -> dict:
    return {"inline_keyboard": [
        [
            {"text": "🔄 刷新", "callback_data": f"traffic:{inst}"},
            {"text": "🖥 VPS 详情", "callback_data": f"vps:{inst}"},
        ],
        [
            {"text": "◀️ 流量总览", "callback_data": "traffic"},
            {"text": "📊 总览", "callback_data": "overview"},
        ],
    ]}

# ── Auto-refresh tracking ──────────────────────────────────
# {(chat_id, msg_id): asyncio.Task}
live_sessions: dict = {}

async def auto_refresh_vps(chat_id: str, msg_id: int, inst: str):
    """Auto-refresh VPS detail every 5s until user navigates away."""
    log.info(f"Auto-refresh started for {inst} chat={chat_id} msg={msg_id}")
    try:
        while True:
            await asyncio.sleep(5)
            key = (chat_id, msg_id)
            if key not in live_sessions:
                break
            try:
                text = await build_vps_detail(inst)
                await tg_edit(chat_id, msg_id, text, kb_back(inst))
            except Exception as e:
                log.warning(f"Auto-refresh vps error for {inst}: {e}")
    except asyncio.CancelledError:
        pass

async def auto_refresh_overview(chat_id: str, msg_id: int):
    """Auto-refresh overview every 5s until user navigates away."""
    log.info(f"Auto-refresh overview started chat={chat_id} msg={msg_id}")
    try:
        while True:
            await asyncio.sleep(5)
            key = (chat_id, msg_id)
            if key not in live_sessions:
                break
            try:
                text = await build_overview()
                await tg_edit(chat_id, msg_id, text, kb_overview())
            except Exception as e:
                log.warning(f"Auto-refresh overview error: {e}")
    except asyncio.CancelledError:
        pass

def start_live(chat_id: str, msg_id: int, inst: str = None):
    key = (chat_id, msg_id)
    old = live_sessions.pop(key, None)
    if old:
        old.cancel()
    if inst:
        live_sessions[key] = asyncio.create_task(auto_refresh_vps(chat_id, msg_id, inst))
    else:
        live_sessions[key] = asyncio.create_task(auto_refresh_overview(chat_id, msg_id))

def stop_live(chat_id: str, msg_id: int):
    key = (chat_id, msg_id)
    task = live_sessions.pop(key, None)
    if task:
        task.cancel()

# ── Alertmanager webhook ───────────────────────────────────
async def handle_alert(request):
    try:
        body = await request.json()
        for alert in body.get("alerts", []):
            status = alert.get("status", "unknown")
            labels = alert.get("labels", {})
            ann    = alert.get("annotations", {})
            name   = labels.get("alertname", "Unknown")
            inst   = labels.get("instance", "?")
            sev    = labels.get("severity", "info")
            summary = ann.get("summary", name)

            if status == "firing":
                icon = "🔴" if sev == "critical" else "⚠️"
                text = f"{icon} <b>告警触发</b>\n\n{summary}\n\n实例: <code>{inst}</code>\n严重级别: {sev}"
            else:
                text = f"✅ <b>告警恢复</b>\n\n{summary}\n\n实例: <code>{inst}</code>"
            await tg_send(text)
    except Exception as e:
        log.error(f"Alert webhook error: {e}")
    return web.Response(text="ok")

# ── Telegram polling ───────────────────────────────────────
async def poll_telegram():
    offset = 0
    log.info("Starting Telegram polling...")
    while True:
        try:
            async with ClientSession() as s:
                async with s.get(f"{TG_API}/getUpdates",
                                 params={"offset": offset, "timeout": 30},
                                 timeout=35) as r:
                    data = await r.json()

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1

                # Handle messages
                msg = upd.get("message", {})
                if msg:
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text    = msg.get("text", "").strip()
                    if chat_id != str(CHAT_ID):
                        continue
                    if text.startswith("/status") or text.startswith("/start"):
                        mid = await tg_send(await build_overview(), kb_overview())
                        if mid:
                            start_live(chat_id, mid)
                    elif text.startswith("/vps"):
                        await tg_send("🖥 <b>选择 VPS:</b>", await kb_vps_list())
                    elif text.startswith("/traffic"):
                        await tg_send(await build_traffic_overview(), kb_traffic())
                    elif text.startswith("/help"):
                        await tg_send(
                            "📋 <b>命令列表</b>\n\n"
                            "/status  — 所有服务器总览\n"
                            "/vps     — 选择查看指定 VPS\n"
                            "/traffic — 流量统计\n"
                            "/help    — 帮助\n\n"
                            "💡 所有面板支持按钮交互和刷新"
                        )

                # Handle callback queries
                cb = upd.get("callback_query", {})
                if cb:
                    cid  = cb["id"]
                    cd   = cb.get("data", "")
                    cm   = cb.get("message", {})
                    cc   = str(cm.get("chat", {}).get("id", ""))
                    mid  = cm.get("message_id")
                    if cc != str(CHAT_ID):
                        await tg_answer(cid, "⛔"); continue
                    await tg_answer(cid, "⏳")
                    try:
                        if cd == "overview":
                            await tg_edit(cc, mid, await build_overview(), kb_overview())
                            start_live(cc, mid)
                        elif cd == "menu":
                            stop_live(cc, mid)
                            await tg_edit(cc, mid, "🖥 <b>选择 VPS:</b>", await kb_vps_list())
                        elif cd == "traffic":
                            stop_live(cc, mid)
                            await tg_edit(cc, mid, await build_traffic_overview(), kb_traffic())
                        elif cd.startswith("traffic:"):
                            stop_live(cc, mid)
                            inst = cd[8:]
                            await tg_edit(cc, mid, await build_traffic_detail(inst), kb_traffic_detail(inst))
                        elif cd.startswith("vps:"):
                            inst = cd[4:]
                            await tg_edit(cc, mid, await build_vps_detail(inst), kb_back(inst))
                            start_live(cc, mid, inst)
                    except Exception as e:
                        log.error(f"Callback error: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Poll error: {e}")
            await asyncio.sleep(5)

# ── App ────────────────────────────────────────────────────
async def on_start(app):
    app["poller"] = asyncio.create_task(poll_telegram())
    log.info("Bot started — /status /vps /traffic /help")

async def on_stop(app):
    app["poller"].cancel()

async def handle_login(request):
    try:
        body = await request.json()
        user = body.get("user", "unknown")
        host = body.get("host", "unknown")
        ip   = body.get("ip", "unknown")
        port = body.get("port", "")
        time = body.get("time", "")
        text = (
            f"🔐 <b>SSH 登录通知</b>\n\n"
            f"主机: <code>{host}</code>\n"
            f"用户: <code>{user}</code>\n"
            f"来源: <code>{ip}</code>\n"
            f"端口: <code>{port}</code>\n"
            f"时间: {time}"
        )
        await tg_send(text)
    except Exception as e:
        log.error(f"Login notify error: {e}")
    return web.Response(text="ok")

app = web.Application()
app.router.add_post("/alertmanager", handle_alert)
app.router.add_post("/login-notify", handle_login)
app.on_startup.append(on_start)
app.on_cleanup.append(on_stop)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8000)
