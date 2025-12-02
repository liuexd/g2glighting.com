#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Push.py — 在线抓取中兴路由器已连接设备。

主要功能：
    * 周期性抓取在线设备列表（默认 5s）。
    * 监控目标设备上线/下线，控制台高亮提示并蜂鸣。
    * 通过 PushPlus 与企业微信 webhook 推送上下线通知。
    * 解析接口返回的 JSON/HTML，提取 RSSI/信号强度信息。

使用方法：
    1. 根据自己的环境修改 URL、COOKIE、EXTRA_HEADERS 以及 TARGET_* 常量。
    2. 若启用自动登录，请确认 ``get_cookie`` 中的逻辑与路由器登录流程一致。
    3. 运行脚本后即可在控制台查看扫描结果。
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# === 推送配置 ================================================================
PUSHPLUS_TOKEN = "335f64dea84b4b728b99adad123b86b7"  # 在 PushPlus 网站中获取
PUSHPLUS_TOPIC = "1"
PUSHPLUS_TITLE = "Warming"
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=815b7393-110d-417c-b688-2ebe9e19fab8"

# === RSSI 阈值（单位 dB）：绝对值 < 65 认为“在线”，>= 65 认为“下线” ===
RSSI_THRESHOLD = 65

# === Cookie/URL 配置 =========================================================
COOKIE_FILE = Path("cookie.txt")
STATE_FILE = Path("zte_clients_state.json")
EVENT_LOG = Path("target_events.log")
STATUS_LOG = Path("target_status.log")
URL = "http://192.168.5.1/?_type=vueData&_tag=vue_topo_data&Action=GetALLClients"
EXTRA_HEADERS: Dict[str, str] = {}

# === 持续扫描配置 ============================================================
WATCH = True
INTERVAL = 5  # seconds
SAVE_RAW = False

# === 目标设备匹配条件（命中任意一项即认为是目标设备） =======================
TARGET_MAC = ""  # 例如 "58:A0:23:7E:F6:XX"
TARGET_IP = ""
TARGET_NAME_CONTAINS = "FOA-AL00"

# === 警告提示设置 ============================================================
BEEP_ON_EVENT = True


# ---------------------------------------------------------------------------
# 通用工具函数
# ---------------------------------------------------------------------------

def ts() -> str:
    """返回当前时间的格式化字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_rssi_value(rssi_text: str) -> Optional[int]:
    """从字符串提取 RSSI 数值的绝对值。"""
    if not rssi_text:
        return None
    match = re.search(r"(-?\d+)", str(rssi_text))
    if not match:
        return None
    try:
        return abs(int(match.group(1)))
    except ValueError:
        return None


def is_target_online(devices: List[Dict[str, str]]) -> bool:
    """根据 RSSI 阈值判断目标设备是否在线。"""
    for device in devices:
        if match_target(device):
            value = parse_rssi_value(device.get("rssi", ""))
            return value is not None and value < RSSI_THRESHOLD
    return False


def warn(message: str) -> None:
    """控制台高亮提示，并尝试蜂鸣。"""
    flag = f"*** {message} ***"
    print(f"\n{flag}")
    if BEEP_ON_EVENT:
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 通知相关逻辑
# ---------------------------------------------------------------------------

def send_pushplus_notification(content: str) -> None:
    """通过 PushPlus 推送消息。"""
    params = {
        "token": PUSHPLUS_TOKEN,
        "title": PUSHPLUS_TITLE,
        "content": content,
        "topic": PUSHPLUS_TOPIC,
    }
    try:
        requests.get("http://www.pushplus.plus/send", params=params, timeout=10)
    except Exception as exc:  # 网络错误无需中断主流程
        print(f"[{ts()}] PushPlus 推送失败：{exc}")


def send_webhook_notification(text: str) -> None:
    """通过企业微信机器人推送消息。"""
    payload = {
        "msgtype": "text",
        "text": {"content": text},
    }
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
    except Exception as exc:
        print(f"[{ts()}] Webhook 推送失败：{exc}")


def notify_status_change(is_online: bool) -> None:
    """根据状态发送 PushPlus 与企业微信通知。"""
    content = "U" if is_online else "D"
    status_text = "UP" if is_online else "DOWN"
    send_pushplus_notification(content)
    send_webhook_notification(f"[{ts()}]+{status_text}")
    log_status(f"[{ts()}] 状态变化：{status_text}")


def notify_presence_change(is_present: bool) -> None:
    """推送目标设备是否已连接（online/offline）。"""
    status_text = "ONLINE" if is_present else "OFFLINE"
    send_webhook_notification(f"[{ts()}]+{status_text}")
    log_status(f"[{ts()}] 连接状态：{status_text}")


# ---------------------------------------------------------------------------
# Cookie 管理
# ---------------------------------------------------------------------------

def read_cookie_from_file(path: Path = COOKIE_FILE) -> str:
    """读取 cookie.txt 中的 SID。"""
    if not path.exists():
        raise FileNotFoundError(f"未找到 {path}，请先生成或手动创建。")
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        return line if line.upper().startswith("SID=") else f"SID={line}"
    raise ValueError(f"{path} 内容为空或未包含可用的 SID 行。")


def get_cookie() -> None:
    """使用 Selenium 自动登录以刷新 cookie。"""
    import time as _time

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    base_url = "http://192.168.5.1"
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,900")

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(base_url)
        wait = WebDriverWait(driver, 10)

        password_input = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'input[placeholder="请输入登录密码"]'))
        )
        password_input.clear()
        password_input.send_keys("307307307")

        login_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, '//button[contains(normalize-space(.), "登录")]'))
        )
        login_button.click()

        _time.sleep(1)
        wait.until(EC.url_contains("#/"))
        sid = next((c["value"] for c in driver.get_cookies() if c["name"].lower() == "sid"), None)
        cookie_value = f"SID={sid}" if sid else ""
        if cookie_value:
            COOKIE_FILE.write_text(cookie_value + "\n", encoding="utf-8")
            print(f"[{ts()}] 已刷新 Cookie: {cookie_value}")
        else:
            print(f"[{ts()}] 未获取到 SID，未写入 cookie.txt")
    finally:
        driver.quit()


def load_cookie(force_refresh: bool = False) -> str:
    """返回有效的 cookie，必要时自动刷新。"""
    if force_refresh:
        get_cookie()
    try:
        return read_cookie_from_file()
    except (FileNotFoundError, ValueError):
        if not force_refresh:
            get_cookie()
            return read_cookie_from_file()
        raise


# ---------------------------------------------------------------------------
# 设备抓取与解析
# ---------------------------------------------------------------------------

def fetch_url(url: str, cookie: Optional[str], extra_headers: Dict[str, str]) -> Tuple[str, Optional[str]]:
    """携带 Cookie 请求路由器接口。"""
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    headers = {"User-Agent": "Mozilla/5.0"}
    if cookie:
        headers["Cookie"] = cookie
    headers.update(extra_headers)

    response = requests.get(url, headers=headers, timeout=10, verify=False)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower() or None
    text = response.text

    if SAVE_RAW:
        extension = (
            "json"
            if content_type == "application/json" or text.strip().startswith("{") or text.strip().startswith("[")
            else "html"
        )
        Path(f"router_raw.{extension}").write_text(text, encoding="utf-8", errors="ignore")

    return text, content_type


def extract_clients_from_json(data: Any) -> List[Dict[str, str]]:
    """从 JSON 结构中提取设备信息。"""
    found: Dict[str, Dict[str, str]] = {}
    rssi_keys = {"rssi", "signal", "signal_strength", "sig", "rx_signal", "wifi_signal", "ap_rssi", "sta_rssi"}

    def normalize_mac(value: str) -> str:
        return value.strip().upper().replace("-", ":")

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            lower_keys = {k.lower(): k for k in node}
            mac_key = next((lower_keys[k] for k in lower_keys if k in {"mac", "macaddr", "mac_addr", "macaddress"}), None)
            ip_key = next((lower_keys[k] for k in lower_keys if k in {"ip", "ipv4", "ipaddr", "ip_addr", "ipaddress"}), None)
            name_key = next((lower_keys[k] for k in lower_keys if k in {"name", "hostname", "devname", "device_name", "dev_name"}), None)
            rssi_key = next((lower_keys[k] for k in lower_keys if k in rssi_keys), None)

            if mac_key and ip_key:
                mac = normalize_mac(str(node.get(mac_key, "")))
                ip = str(node.get(ip_key, ""))
                if ip and mac:
                    found[mac] = {
                        "name": str(node.get(name_key, "")) if name_key else "",
                        "ip": ip,
                        "mac": mac,
                        "rssi": str(node.get(rssi_key, "")) if rssi_key else "",
                    }
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(data)
    return list(found.values())


def parse_clients_from_html(html: str) -> List[Dict[str, str]]:
    """解析 HTML 表格中的设备信息。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    candidate_tables = []

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        header_text = "|".join(headers)
        if (
            any(keyword in header_text for keyword in ["All Clients", "设备名称"])
            and "IP" in header_text
            and any(keyword in header_text for keyword in ["MAC", "Mac", "MAC 地址"])
        ):
            candidate_tables.append(table)

    if not candidate_tables:
        label = soup.find(lambda tag: tag.name in {"h2", "h3", "div", "span"} and "All Clients" in tag.get_text())
        if label:
            table = label.find_next("table")
            if table:
                candidate_tables.append(table)

    if not candidate_tables:
        best_table = None
        best_columns = 0
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            if "IP" in "|".join(headers):
                column_count = len(headers)
                if column_count >= best_columns:
                    best_table = table
                    best_columns = column_count
        if best_table:
            candidate_tables.append(best_table)

    if not candidate_tables:
        raise RuntimeError("未找到包含客户端列表的表格（可能为 SPA 页面，请改抓 JSON 接口）。")

    def index_of(candidates: List[str], headers: List[str]) -> Optional[int]:
        for index, header in enumerate(headers):
            for candidate in candidates:
                if candidate in header:
                    return index
        return None

    table = candidate_tables[0]
    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    name_index = index_of(["设备名称", "Name", "主机名"], headers)
    ip_index = index_of(["IP", "IPv4"], headers)
    mac_index = index_of(["MAC", "Mac", "MAC 地址"], headers)
    rssi_index = index_of(["RSSI", "信号", "信号强度", "Signal"], headers)

    devices: List[Dict[str, str]] = []
    for row in table.find_all("tr"):
        columns = row.find_all("td")
        if not columns:
            continue

        def get_value(index: Optional[int]) -> str:
            return columns[index].get_text(strip=True) if index is not None and index < len(columns) else ""

        name = get_value(name_index)
        ip = get_value(ip_index)
        mac = get_value(mac_index).upper().replace("-", ":")
        rssi = get_value(rssi_index)

        if ip and mac:
            devices.append({"name": name, "ip": ip, "mac": mac, "rssi": rssi})

    return devices


def parse_clients_auto(text: str, content_type: Optional[str]) -> List[Dict[str, str]]:
    """自动选择 JSON 或 HTML 解析方式。"""
    if content_type == "application/json":
        try:
            data = json.loads(text)
            devices = extract_clients_from_json(data)
            if devices:
                return devices
        except Exception:
            pass

    try:
        data = json.loads(text)
        devices = extract_clients_from_json(data)
        if devices:
            return devices
    except Exception:
        pass

    return parse_clients_from_html(text)


# ---------------------------------------------------------------------------
# 设备列表处理与状态维护
# ---------------------------------------------------------------------------

def save_state(state: Dict[str, Dict[str, Any]]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state() -> Dict[str, Dict[str, Any]]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def diff(prev: Dict[str, Dict[str, Any]], current: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
    current_by_mac = {device["mac"]: device for device in current}
    previously_online = {mac for mac, info in prev.items() if info.get("online", False)}
    current_macs = set(current_by_mac.keys())

    new_devices = [current_by_mac[mac] for mac in current_macs - previously_online]
    gone_macs = sorted(previously_online - current_macs)
    gone_devices = [
        {
            "name": prev[mac].get("name", ""),
            "ip": prev[mac].get("ip", ""),
            "mac": mac,
            "rssi": prev[mac].get("rssi", ""),
        }
        for mac in gone_macs
    ]
    current_devices = [current_by_mac[mac] for mac in sorted(current_macs)]

    for mac, device in current_by_mac.items():
        prev.setdefault(mac, {}).update(
            {
                "name": device.get("name", ""),
                "ip": device.get("ip", ""),
                "rssi": device.get("rssi", ""),
                "online": True,
            }
        )
    for mac in gone_macs:
        if mac in prev:
            prev[mac]["online"] = False

    return current_devices, new_devices, gone_devices


# ---------------------------------------------------------------------------
# 目标设备监控
# ---------------------------------------------------------------------------

def match_target(device: Dict[str, str]) -> bool:
    if TARGET_MAC and device.get("mac", "").upper() == TARGET_MAC.strip().upper():
        return True
    if TARGET_IP and device.get("ip", "") == TARGET_IP.strip():
        return True
    if TARGET_NAME_CONTAINS and TARGET_NAME_CONTAINS.strip().lower() in device.get("name", "").lower():
        return True
    return False


def append_event_log(message: str) -> None:
    with EVENT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def log_status(message: str) -> None:
    try:
        with STATUS_LOG.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except Exception as exc:
        print(f"[{ts()}] 写入状态日志失败：{exc}")


def log_program_exit(reason: str, notify: bool = True) -> None:
    message = f"[{ts()}] 程序退出：{reason}"
    log_status(message)
    if notify:
        send_webhook_notification(message)


def check_target(devices: List[Dict[str, str]], last_state: Optional[bool]) -> Optional[bool]:
    online_device = next((device for device in devices if match_target(device)), None)
    online = online_device is not None

    if last_state is None:
        if online and online_device:
            warn(f"[{ts()}] 目标初始在线，RSSI={online_device.get('rssi', '')}")
        else:
            print(f"[{ts()}] 目标初始离线")
        notify_presence_change(online)
        return online

    if online != last_state:
        status_text = "上线" if online else "下线"
        rssi_info = f"，RSSI={online_device.get('rssi', '')}" if online and online_device else ""
        message = f"[{ts()}] 目标设备{status_text}{rssi_info}"
        warn(message)
        append_event_log(message)
        notify_presence_change(online)

    return online


def render_target_status(devices: List[Dict[str, str]]) -> None:
    target = next((device for device in devices if match_target(device)), None)
    if target:
        print(
            f"\n[目标设备] 在线 ✓  IP={target.get('ip', '')}  MAC={target.get('mac', '')}  RSSI={target.get('rssi', '')}"
        )
    else:
        print("\n[目标设备] 离线 ✗")


# ---------------------------------------------------------------------------
# 扫描主流程
# ---------------------------------------------------------------------------

def scan_once(prev_state: Dict[str, Dict[str, Any]], cookie: str) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]], str]:
    """抓取一次设备列表，必要时刷新 Cookie。"""

    def _fetch(active_cookie: str) -> List[Dict[str, str]]:
        text, content_type = fetch_url(URL, active_cookie, EXTRA_HEADERS)
        return parse_clients_auto(text, content_type)

    try:
        current = _fetch(cookie)
    except requests.exceptions.HTTPError as error:
        status_code = getattr(error.response, "status_code", None)
        if status_code in {401, 403}:
            print(f"[{ts()}] HTTP {status_code}：Cookie 可能失效，正在刷新…")
            cookie = load_cookie(force_refresh=True)
            current = _fetch(cookie)
        else:
            print(f"[{ts()}] HTTP 错误：{error}")
            return [], [], [], cookie
    except RuntimeError as error:
        if "未找到包含客户端列表的表格" in str(error):
            print(f"[{ts()}] 解析失败（可能跳回登录页），正在刷新 Cookie…")
            cookie = load_cookie(force_refresh=True)
            current = _fetch(cookie)
        else:
            print(f"[{ts()}] 解析异常：{error}")
            return [], [], [], cookie
    except Exception as error:
        print(f"[{ts()}] 抓取/解析失败：{error}")
        return [], [], [], cookie

    current_devices, new_devices, gone_devices = diff(prev_state, current)
    save_state(prev_state)
    return current_devices, new_devices, gone_devices, cookie


def print_table(title: str, rows: List[Dict[str, str]], columns: Tuple[str, str, str, str] = ("name", "ip", "mac", "rssi"), headers: Tuple[str, str, str, str] = ("设备名称", "IP", "MAC", "RSSI")) -> None:
    print(f"\n== {title} ({len(rows)}) ==")
    if not rows:
        print("(无)")
        return

    widths = [24, 16, 18, 8]
    line_format = "{:<{w0}}  {:<{w1}}  {:<{w2}}  {:<{w3}}"
    print(line_format.format(*headers, w0=widths[0], w1=widths[1], w2=widths[2], w3=widths[3]))
    print("-" * (sum(widths) + 6))

    for row in rows:
        values = [row.get(column, "") for column in columns]
        print(line_format.format(*values, w0=widths[0], w1=widths[1], w2=widths[2], w3=widths[3]))


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    prev_state = load_state()
    target_last_state: Optional[bool] = None
    target_online_last: Optional[bool] = None

    cookie = load_cookie()

    if WATCH:
        print(f"[INFO] 持续扫描已开启，每 {INTERVAL}s 一次。按 Ctrl+C 停止。")
        try:
            while True:
                try:
                    print(f"\n[{ts()}] 扫描中：{URL}")
                    current, new, gone, cookie = scan_once(prev_state, cookie)

                    print_table("当前在线设备", current)
                    print_table("新上线", new)
                    print_table("下线", gone)
                    render_target_status(current)

                    if TARGET_MAC or TARGET_IP or TARGET_NAME_CONTAINS:
                        target_last_state = check_target(current, target_last_state)

                        current_online = is_target_online(current)
                        if target_online_last is None:
                            target_online_last = current_online
                            initial_status = "UP" if current_online else "DOWN"
                            log_status(f"[{ts()}] 状态初始化：{initial_status}")
                        elif current_online != target_online_last:
                            notify_status_change(current_online)
                            target_online_last = current_online

                    time.sleep(INTERVAL)
                except Exception as loop_error:
                    error_message = f"[{ts()}] 扫描异常：{loop_error}"
                    warn(error_message)
                    log_status(error_message)
                    time.sleep(INTERVAL)
        except KeyboardInterrupt:
            print("\n[INFO] 已停止。")
            log_program_exit("用户终止", notify=True)
        except Exception as exc:
            log_program_exit(f"异常退出：{exc}", notify=True)
            raise
    else:
        completed = False
        try:
            current, new, gone, _ = scan_once(prev_state, cookie)
            print_table("当前在线设备", current)
            print_table("新上线", new)
            print_table("下线", gone)
            render_target_status(current)

            if TARGET_MAC or TARGET_IP or TARGET_NAME_CONTAINS:
                check_target(current, None)
            completed = True
        except Exception as exc:
            log_program_exit(f"异常退出：{exc}", notify=True)
            raise
        finally:
            if completed:
                log_program_exit("单次扫描完成", notify=False)


if __name__ == "__main__":
    main()
