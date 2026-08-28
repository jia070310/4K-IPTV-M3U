import requests
import os
import re
import time
import subprocess
import argparse
import hmac
import hashlib
import random
import string
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import quote, urlencode
try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None

# ================= 配置区域 =================
# 1. 组播源网站配置（IPTV神器Pro）
IPTV_BASE_URL = "https://iptv.cqshushu.com/"
IPTV_INDEX = "index.php"
PAER_HMAC_KEY = "tdSQ4QZEaQPPff7e4wMReKjhnwXecJUxJTdAVDGIql9xR3fIAf"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 2. GitHub 推送配置
# 提交说明前缀；为空时使用默认文案
GITHUB_COMMIT_PREFIX = "Auto update"
# ============================================
EPG_URL = "http://epg.51zmt.top:8000/e.xml.gz"
TVG_LOGO_BASE_URL = "https://gcore.jsdelivr.net/gh/taksssss/tv/icon/"
README_FILE = "README.md"
RAW_BASE_URL = "https://raw.githubusercontent.com/jia070310/4K-IPTV-M3U/main"
PROXY_PREFIX = "https://gh-proxy.org/"

# 中国省份全称及简称对照表，用于智能嗅探
PROVINCES = ["北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海",
             "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
             "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西",
             "甘肃", "青海", "宁夏", "新疆"]

# 新站点省份筛选 code（与 iptv.cqshushu.com 下拉框一致）
PROVINCE_CODES = {
    "北京": "bj", "天津": "tj", "河北": "he", "山西": "sx", "内蒙古": "nm",
    "辽宁": "ln", "吉林": "jl", "黑龙江": "hl", "上海": "sh",
    "江苏": "js", "浙江": "zj", "安徽": "ah", "福建": "fj", "江西": "jx",
    "山东": "sd", "河南": "ha", "湖北": "hb", "湖南": "hn", "广东": "gd",
    "广西": "gx", "海南": "hi", "重庆": "cq", "四川": "sc", "贵州": "gz",
    "云南": "yn", "陕西": "sn", "甘肃": "gs", "青海": "qh", "宁夏": "nx",
    "新疆": "xj",
}


def get_root_domain(domain):
    """提取根域名，防 DDNS 假去重"""
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', domain): return domain
    parts = domain.split('.')
    if len(parts) >= 3:
        if parts[-2] in ['com', 'net', 'org', 'gov', 'edu', 'gx'] or len(parts[-2]) <= 2:
            return ".".join(parts[-3:])
        else: return ".".join(parts[-2:])
    return domain

def check_and_clear_existing(txt_file, m3u_file):
    """不做测流，直接清空旧文件并重新导出。"""
    if not os.path.exists(txt_file):
        return False
    print(f"[*] 不做测流，清空旧文件后重新导出...")
    for file in [txt_file, m3u_file]:
        with open(file, 'w', encoding='utf-8') as f: f.write("")
    return False


def clear_output_files(txt_output_dir, m3u_output_dir):
    """运行前清理历史产物，避免旧命名文件残留。"""
    for out_dir, suffix in ((txt_output_dir, ".txt"), (m3u_output_dir, ".m3u")):
        if not os.path.exists(out_dir):
            continue
        for name in os.listdir(out_dir):
            if name.endswith(suffix):
                try:
                    os.remove(os.path.join(out_dir, name))
                except OSError:
                    pass

def _strip_html(raw):
    no_tags = re.sub(r"<[^>]+>", "", raw)
    return unescape(no_tags).replace("\xa0", " ").strip()


def _parse_site_datetime(value: str) -> datetime | None:
    s = (value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def generate_paer_token() -> str:
    """生成 iptv.cqshushu.com 请求签名（X-CSRF-TOKEN）。"""
    ts = str(int(time.time()))
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=20))
    msg = f"{ts}|{rand}"
    sig = hmac.new(PAER_HMAC_KEY.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts}|{rand}|{sig}"


REQUEST_DELAY_SEC = 0.8
REQUEST_MAX_RETRIES = 5


def signed_get(path_query: str, session: requests.Session | None = None) -> dict:
    """带签名的 GET 请求，返回 JSON（含 html 字段）。"""
    url = IPTV_BASE_URL + path_query.lstrip("/")
    sess = session or requests.Session()
    last_error = None

    for attempt in range(REQUEST_MAX_RETRIES):
        headers = {
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-TOKEN": generate_paer_token(),
        }
        try:
            resp = sess.get(url, headers=headers, timeout=30)
            if resp.status_code == 429:
                wait = min(30, REQUEST_DELAY_SEC * (2 ** attempt) * 3)
                print(f"[!] 请求过于频繁，{wait:.1f}s 后重试 ({attempt + 1}/{REQUEST_MAX_RETRIES})...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY_SEC)
            return resp.json()
        except requests.HTTPError as e:
            last_error = e
            if e.response is not None and e.response.status_code in (429, 503):
                wait = min(30, REQUEST_DELAY_SEC * (2 ** attempt) * 3)
                print(f"[!] HTTP {e.response.status_code}，{wait:.1f}s 后重试 ({attempt + 1}/{REQUEST_MAX_RETRIES})...")
                time.sleep(wait)
                continue
            raise
        except requests.RequestException as e:
            last_error = e
            if attempt + 1 >= REQUEST_MAX_RETRIES:
                raise
            wait = REQUEST_DELAY_SEC * (2 ** attempt)
            print(f"[!] 网络异常，{wait:.1f}s 后重试 ({attempt + 1}/{REQUEST_MAX_RETRIES}): {e}")
            time.sleep(wait)

    if last_error:
        raise last_error
    raise RuntimeError(f"请求失败: {url}")


def _parse_list_rows(html: str) -> list[dict]:
    """从 IP 列表页 HTML 解析组播行。"""
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.IGNORECASE | re.DOTALL):
        ip_match = re.search(
            r"gotoIP\('([^']+)',\s*'multicast'\)",
            row_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not ip_match:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.IGNORECASE | re.DOTALL)
        if len(tds) < 6:
            continue
        rows.append({
            "p_token": ip_match.group(1).strip(),
            "host": _strip_html(tds[0]),
            "type": _strip_html(tds[2]),
            "online_time": _strip_html(tds[3]),
            "update_time": _strip_html(tds[4]),
            "status": _strip_html(tds[5]),
        })
    return rows


def fetch_region_rows_by_ajax(province, limit=20, max_pages=30, session=None):
    """按省份+组播类型分页抓取 IP 列表。"""
    region_code = PROVINCE_CODES.get(province)
    if not region_code:
        print(f"[-] 未找到省份 [{province}] 的 region code，跳过。")
        return []

    print(f"[*] 正在抓取组播源: {IPTV_BASE_URL}{IPTV_INDEX}?t=multicast&province={region_code}")
    all_rows = []
    seen_tokens = set()
    empty_page_hits = 0

    for page_num in range(1, max_pages + 1):
        query = urlencode({
            "t": "multicast",
            "province": region_code,
            "limit": limit,
            "page": page_num,
        })
        path = f"{IPTV_INDEX}?{query}"
        try:
            data = signed_get(path, session=session)
        except Exception as e:
            print(f"[-] 请求省份 [{province}] 第{page_num}页失败: {e}")
            break
        if data.get("status") != "success":
            msg = data.get("message", "unknown error")
            print(f"[-] 第{page_num}页返回失败: {msg}")
            break

        rows = _parse_list_rows(data.get("html", ""))
        if not rows:
            empty_page_hits += 1
            if empty_page_hits >= 2:
                break
            continue

        empty_page_hits = 0
        added = 0
        for row in rows:
            token = row.get("p_token")
            if not token or token in seen_tokens:
                continue
            seen_tokens.add(token)
            all_rows.append(row)
            added += 1
        print(f"[*] [{province}] 第{page_num}页 {len(rows)} 条，新增 {added} 条。")

    print(f"[*] [{province}] 全分页合计 {len(all_rows)} 条服务器。")
    return all_rows


def get_region_assets(province, rows=None):
    """按地区提取服务器，优先新上线，再存活，最多返回前5条。"""
    rows = rows if rows is not None else fetch_region_rows_by_ajax(province)
    region_all = [r for r in rows if province in r.get("type", "")]
    if not region_all:
        print(f"[-] 未找到 [{province}] 地区服务器。")
        return [], []

    preferred_new = [r for r in region_all if "新上线" in r.get("status", "")]
    preferred_alive = [r for r in region_all if "存活" in r.get("status", "")]
    preferred = (preferred_new + preferred_alive)[:5]
    if not preferred:
        print(f"[-] [{province}] 当前没有“新上线”或“存活”服务器，本次不提取。")
        return region_all, []
    return region_all, preferred

def parse_s_token(detail_html: str) -> str | None:
    """从 IP 详情页提取频道列表 s token。"""
    m = re.search(
        r"href=['\"]\?s=([^&'\"]+)&t=multicast['\"]",
        detail_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1)
    m = re.search(r'data-s="([^"]+)"', detail_html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r'href="[^"]*[?&]s=([^"&]+)', detail_html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1)
    return None


def fetch_detail_html(p_token: str, session: requests.Session | None = None) -> str:
    query = urlencode({"p": p_token, "t": "multicast"})
    path = f"{IPTV_INDEX}?{query}"
    data = signed_get(path, session=session)
    return data.get("html", "") or ""


def fetch_channel_lines_by_s(s_token: str, session: requests.Session | None = None, max_pages: int = 50) -> list[str]:
    """分页抓取完整频道列表。"""
    all_lines: list[str] = []
    seen: set[str] = set()
    empty_hits = 0

    for page_num in range(1, max_pages + 1):
        query = urlencode({"s": s_token, "t": "multicast", "page": page_num})
        path = f"{IPTV_INDEX}?{query}"
        try:
            data = signed_get(path, session=session)
        except Exception as e:
            print(f"[-] 频道列表第{page_num}页失败: {e}")
            break
        if data.get("status") != "success":
            break
        html = data.get("html", "")
        page_lines = parse_channel_lines(html)
        if not page_lines:
            empty_hits += 1
            if empty_hits >= 2:
                break
            continue
        empty_hits = 0
        for line in page_lines:
            if line in seen:
                continue
            seen.add(line)
            all_lines.append(line)
        if "下一页" not in html and page_num > 1:
            break
    return all_lines

def parse_channel_lines(channels_html: str) -> list[str]:
    lines = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", channels_html, flags=re.IGNORECASE | re.DOTALL):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.IGNORECASE | re.DOTALL)
        if len(tds) < 3:
            continue
        name = _strip_html(tds[1])
        play_url = _strip_html(tds[2])
        if not name or not play_url:
            continue
        # 保留站点返回的完整播放地址（含服务器 IP:PORT），避免只剩组播段地址
        if not re.search(r"(https?://|rtp/|udp/|igmp/)", play_url, flags=re.IGNORECASE):
            continue
        lines.append(f"{name},{play_url}")
    return lines


def normalize_group_title(raw_type: str, province: str) -> str:
    """将站点 type 字段规范化为“省份+运营商”格式（如：江西电信）。"""
    text = (raw_type or "").strip()
    if not text:
        return province
    # 常见格式：江西上饶组播|江西电信，优先使用“|”后半段
    if "|" in text:
        right = text.split("|")[-1].strip()
        if right:
            return right
    # 兜底：统一裁剪为“省份+运营商”，去掉城市等中间信息
    carriers = ("电信", "联通", "移动", "广电")
    for carrier in carriers:
        if carrier in text:
            return f"{province}{carrier}"
    return province


def parse_operator_name(detail_html: str, province: str) -> str:
    """优先从详情页“运营商”字段提取文件名，如：湖北电信。"""
    carriers = ("电信", "联通", "移动", "广电")
    # 先在“运营商”附近做精确提取（兼容 th/td 或 div 结构）
    m = re.search(
        r"运营商[\s\S]{0,120}?(" + re.escape(province) + r"(?:电信|联通|移动|广电))",
        detail_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        value = _strip_html(m.group(1))
        if value:
            return value
    # 次级匹配：不限定“运营商”字样，直接在详情中找“省份+运营商”
    m = re.search(
        r"(" + re.escape(province) + r"(?:电信|联通|移动|广电))",
        detail_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        value = _strip_html(m.group(1))
        if value:
            return value
    # 最后兜底：匹配任意运营商后缀
    for carrier in carriers:
        if carrier in detail_html:
            return f"{province}{carrier}"
    return province

def fetch_channel_lines_by_province(
    province: str,
    max_per_carrier: int = 5,
    max_pages: int = 30,
    max_age_hours: int = 24,
):
    session = requests.Session()
    rows = fetch_region_rows_by_ajax(province, limit=20, max_pages=max_pages, session=session)
    if not rows:
        return [], "list_empty", province

    now_dt = datetime.now()

    def _is_usable_status(status: str) -> bool:
        return ("新上线" in status) or ("存活" in status)

    def _is_recent_update(row: dict) -> bool:
        dt = _parse_site_datetime(row.get("update_time", ""))
        if not dt:
            # 更新时间缺失时降级看上线时间；都缺失则判定为不新鲜
            dt = _parse_site_datetime(row.get("online_time", ""))
        if not dt:
            return False
        age_hours = (now_dt - dt).total_seconds() / 3600
        return age_hours <= max_age_hours

    def _status_rank(status: str) -> int:
        # 新上线优先于存活
        return 2 if "新上线" in status else 1

    def _pick_many(rows_pool, carrier: str, limit: int):
        carrier_rows = [
            r
            for r in rows_pool
            if carrier in r.get("type", "")
            and _is_usable_status(r.get("status", ""))
            and _is_recent_update(r)
        ]
        if not carrier_rows or limit <= 0:
            return []

        def _sort_key(row: dict):
            dt = _parse_site_datetime(row.get("update_time", "")) or _parse_site_datetime(row.get("online_time", ""))
            ts = dt.timestamp() if dt else 0.0
            return (_status_rank(row.get("status", "")), ts)

        carrier_rows = sorted(carrier_rows, key=_sort_key, reverse=True)
        picked = []
        seen = set()
        for row in carrier_rows:
            token = row.get("p_token")
            if not token or token in seen:
                continue
            seen.add(token)
            picked.append(row)
            if len(picked) >= limit:
                break
        return picked

    selected_rows = []
    selected_tokens = set()
    for carrier in ("电信", "移动", "联通"):
        for row in _pick_many(rows, carrier, max_per_carrier):
            token = row.get("p_token")
            if not token or token in selected_tokens:
                continue
            selected_rows.append(row)
            selected_tokens.add(token)

    # 兜底：三网都没有可用源时，至少保留1条“新上线/存活”
    if not selected_rows:
        for row in rows:
            if _is_usable_status(row.get("status", "")) and _is_recent_update(row):
                selected_rows = [row]
                break

    if not selected_rows:
        return [], "no_recent_new_or_alive", province

    # group_title -> list of sources, each source is list of "name,url" lines
    group_to_sources: dict[str, list[list[str]]] = {}
    selected_ops: list[str] = []

    for picked in selected_rows:
        picked_type = picked.get("type", "")
        group_title = normalize_group_title(picked_type, province)
        selected_ops.append(group_title)
        try:
            detail_html = fetch_detail_html(picked.get("p_token", ""), session=session)
        except Exception as e:
            print(f"[-] [{province}] IP 详情获取失败: {e}")
            continue
        if not detail_html:
            continue

        s_token = parse_s_token(detail_html)
        if not s_token:
            print(f"[-] [{province}] 未找到频道列表 token: {picked.get('host', '')}")
            continue

        lines = fetch_channel_lines_by_s(s_token, session=session)
        if not lines:
            continue
        group_to_sources.setdefault(group_title, []).append(lines)

    if not group_to_sources:
        return [], "channel_lines_empty", province

    unique_ops = sorted(set(selected_ops))
    print(
        f"[*] [{province}] 已提取源数量: {len(selected_rows)}"
        f"（电信/移动/联通各最多{max_per_carrier}条，更新时间<= {max_age_hours}小时），来源: {', '.join(unique_ops)}"
    )
    return group_to_sources, "ok", province


def extract_test_targets(template_content, max_targets=5):
    """从模板中提取最多 N 个组播测试目标。"""
    matches = re.findall(
        r'(?:https?://[^/,]+/)?(udp|rtp|igmp)(?:/|://)(\d+\.\d+\.\d+\.\d+:\d+)',
        template_content,
        flags=re.IGNORECASE,
    )
    targets = []
    seen = set()
    for protocol, target in matches:
        protocol = protocol.lower()
        key = f"{protocol}://{target}"
        if key in seen:
            continue
        seen.add(key)
        targets.append((protocol, target))
        if len(targets) >= max_targets:
            break
    return targets

def build_tvg_logo_url(channel_name: str) -> str:
    safe_name = quote(channel_name.strip(), safe="")
    return f"{TVG_LOGO_BASE_URL}{safe_name}.png"

def txt_to_m3u_format(txt_content, group_title):
    """智能转换 M3U 分组格式"""
    m3u_lines = []
    for line in txt_content.splitlines():
        line = line.strip()
        if not line: continue
        if '#genre#' in line:
            continue
        elif ',' in line:
            name, url = [p.strip() for p in line.split(',', 1)]
            m3u_lines.append(
                f'#EXTINF:-1 tvg-id="{name}" tvg-logo="{build_tvg_logo_url(name)}" group-title="{group_title}",{name}\n{url}'
            )
    return "\n".join(m3u_lines)


def _build_readme_table_rows(repo_root: str, subdir: str, ext: str, updated_at: str) -> str:
    target_dir = os.path.join(repo_root, subdir)
    if not os.path.exists(target_dir):
        return '<tr><td colspan="4">暂无文件</td></tr>'
    names = sorted([n for n in os.listdir(target_dir) if n.endswith(ext)])
    if not names:
        return '<tr><td colspan="4">暂无文件</td></tr>'

    rows = []
    for name in names:
        file_path = os.path.join(target_dir, name)
        encoded_name = quote(name)
        raw_url = f"{RAW_BASE_URL}/{subdir}/{encoded_name}"
        proxy_url = f"{PROXY_PREFIX}{raw_url}"
        rows.append(
            "<tr>"
            f'<td style="white-space:nowrap;">{name}</td>'
            f'<td style="white-space:nowrap;"><a href="{proxy_url}">下载链接</a></td>'
            f'<td style="white-space:nowrap;">{updated_at}</td>'
            f'<td><code>{proxy_url}</code></td>'
            "</tr>"
        )
    return "\n".join(rows)


def _build_readme_section_table(repo_root: str, subdir: str, ext: str, updated_at: str) -> str:
    rows = _build_readme_table_rows(repo_root, subdir, ext, updated_at)
    return (
        '<table style="width:100%; table-layout:auto;">\n'
        "<colgroup>\n"
        '<col style="width: 220px;" />\n'
        '<col style="width: 120px;" />\n'
        '<col style="width: 170px;" />\n'
        "<col />\n"
        "</colgroup>\n"
        "<thead>\n"
        "<tr>\n"
        '<th style="white-space:nowrap;">文件名</th>\n'
        '<th style="white-space:nowrap;">加速链接</th>\n'
        '<th style="white-space:nowrap;">最近更新时间</th>\n'
        '<th style="white-space:nowrap;">可复制直链</th>\n'
        "</tr>\n"
        "</thead>\n"
        "<tbody>\n"
        f"{rows}\n"
        "</tbody>\n"
        "</table>"
    )


def beijing_now() -> datetime:
    """返回北京时间；Windows 无 tzdata 时回退到 UTC+8。"""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("Asia/Shanghai"))
        except Exception:
            pass
    return datetime.now(timezone(timedelta(hours=8)))


def update_readme_file_list(repo_root: str) -> None:
    readme_path = os.path.join(repo_root, README_FILE)
    if not os.path.exists(readme_path):
        print("[-] README.md 不存在，跳过列表更新。")
        return
    with open(readme_path, "r", encoding="utf-8") as f:
        content = f.read()

    # GitHub Actions runs in UTC by default; use Beijing time for display.
    updated_at = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    m3u_table = _build_readme_section_table(repo_root, "m3u", ".m3u", updated_at)
    txt_table = _build_readme_section_table(repo_root, "txt", ".txt", updated_at)
    m3u_block = f"## M3U 文件列表\n\n{m3u_table}\n"
    txt_block = f"## TXT 文件列表\n\n{txt_table}\n"

    content, m3u_count = re.subn(
        r"## M3U 文件列表[\s\S]*?(?=\r?\n## TXT 文件列表)",
        m3u_block.rstrip(),
        content,
        count=1,
    )
    if "## 免责声明" in content:
        content, txt_count = re.subn(
            r"## TXT 文件列表[\s\S]*?(?=\r?\n---\r?\n\r?\n## 免责声明)",
            txt_block.rstrip(),
            content,
            count=1,
        )
    else:
        content, txt_count = re.subn(
            r"## TXT 文件列表[\s\S]*$",
            txt_block.rstrip(),
            content,
            count=1,
        )

    if m3u_count == 0 or txt_count == 0:
        print("[-] README 结构不匹配（未找到列表区块），跳过自动更新。")
        return

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("[+] README.md 文件列表已自动更新。")

def process_province(
    province,
    txt_output_dir,
    m3u_output_dir,
    max_pages=30,
    max_per_carrier=5,
    max_age_hours=72,
):
    """单一省份核心流水线"""
    group_title = province
    out_txt = os.path.join(txt_output_dir, f"{group_title}.txt")
    out_m3u = os.path.join(m3u_output_dir, f"{group_title}.m3u")

    # 1. 检测已有文件
    if check_and_clear_existing(out_txt, out_m3u): return

    # 2. 直接从频道列表提取 频道名+播放地址
    grouped_sources, status, _ = fetch_channel_lines_by_province(
        province,
        max_pages=max_pages,
        max_per_carrier=max_per_carrier,
        max_age_hours=max_age_hours,
    )
    if not grouped_sources:
        print(f"[-] [{province}] 频道提取失败: {status}")
        return

    # 3. 按运营商分组、按源序号分别生成 txt/m3u
    #    例：山东电信.m3u、山东电信1.m3u、山东电信2.m3u ...
    total_channels = 0
    exported_sources = 0
    for group_title, sources in grouped_sources.items():
        for idx, channel_lines in enumerate(sources):
            if not channel_lines:
                continue
            suffix = "" if idx == 0 else str(idx)
            file_stem = f"{group_title}{suffix}"
            out_txt = os.path.join(txt_output_dir, f"{file_stem}.txt")
            out_m3u = os.path.join(m3u_output_dir, f"{file_stem}.m3u")
            txt_content = "\n".join(channel_lines)
            with open(out_txt, 'w', encoding='utf-8') as f_txt, open(out_m3u, 'w', encoding='utf-8') as f_m3u:
                f_txt.write(txt_content + "\n")
                f_m3u.write(f'#EXTM3U x-tvg-url="{EPG_URL}"\n')
                f_m3u.write(txt_to_m3u_format(txt_content, group_title) + "\n")
            exported_sources += 1
            total_channels += len(channel_lines)
    if exported_sources == 0:
        print(f"[-] [{province}] 频道提取失败: channel_lines_empty")
        return
    print(f"[+] 完美！[{province}] 更新完成，导出 {total_channels} 条频道，生成 {exported_sources} 条源文件（每运营商多条）。")

def push_to_github(files):
    """
    将本次生成文件提交并推送到当前 GitHub 仓库。
    依赖本机已配置好 git 远程与认证（SSH 或凭据管理器）。
    """
    existing_files = [f for f in files if os.path.exists(f)]
    if not existing_files:
        print("[-] 没有可推送文件，跳过 GitHub 同步。")
        return

    print("\n[*] 正在同步到 GitHub 当前仓库...")
    try:
        add_cmd = ["git", "add", "--"] + existing_files
        add_run = subprocess.run(add_cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if add_run.returncode != 0:
            print(f"[-] git add 失败:\n{add_run.stderr.strip()}")
            return

        check_run = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        if check_run.returncode == 0:
            print("[*] 没有新增变更，无需提交。")
            return

        commit_msg = f"{GITHUB_COMMIT_PREFIX} multicast files at {time.strftime('%Y-%m-%d %H:%M:%S')}"
        commit_run = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        if commit_run.returncode != 0:
            print(f"[-] git commit 失败:\n{commit_run.stderr.strip()}")
            return
        print("[+] git commit 成功。")

        push_run = subprocess.run(
            ["git", "push"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        if push_run.returncode != 0:
            print(f"[-] git push 失败:\n{push_run.stderr.strip()}")
            return
        print("[+] 已成功推送到 GitHub。")
    except Exception as e:
        print(f"[!] GitHub 同步异常: {e}")

def parse_args():
    ap = argparse.ArgumentParser(description="按省份抓取频道并生成 txt/m3u。")
    ap.add_argument(
        "--push",
        action="store_true",
        help="生成完成后执行 git add/commit/push（默认关闭，便于在 GitHub Actions 由工作流统一提交）。",
    )
    ap.add_argument(
        "--test-region",
        default="",
        help="仅测试提取某地区全部服务器，不生成文件。例如：--test-region 湖北",
    )
    ap.add_argument(
        "--only-province",
        default="",
        help="仅处理指定省份。例如：--only-province 湖北",
    )
    ap.add_argument(
        "--max-pages",
        type=int,
        default=30,
        help="每个省份最多抓取分页数量（默认30）。",
    )
    ap.add_argument(
        "--max-per-carrier",
        type=int,
        default=5,
        help="每个运营商最多选取“新上线/存活”源数量（默认5）。",
    )
    ap.add_argument(
        "--max-age-hours",
        type=int,
        default=72,
        help="仅提取最近更新 N 小时内的源（默认72，约3天）。",
    )
    return ap.parse_args()


def main():
    args = parse_args()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    txt_output_dir = os.path.join(repo_root, "txt")
    m3u_output_dir = os.path.join(repo_root, "m3u")

    if args.test_region:
        grouped_sources, status, group_title = fetch_channel_lines_by_province(
            args.test_region,
            max_pages=args.max_pages,
            max_per_carrier=args.max_per_carrier,
            max_age_hours=args.max_age_hours,
        )
        total = (
            sum(len(lines) for sources in grouped_sources.values() for lines in sources)
            if grouped_sources
            else 0
        )
        print(f"\n[*] 测试结果: 地区={args.test_region}，分组={group_title}，状态={status}，频道数={total}")
        for k, sources in grouped_sources.items():
            n_sources = len(sources)
            n_lines = sum(len(x) for x in sources)
            print(f"  - {k}: {n_sources} 条源，共 {n_lines} 条")
        return

    os.makedirs(txt_output_dir, exist_ok=True)
    os.makedirs(m3u_output_dir, exist_ok=True)
    # Only clear outputs on full runs; avoid wiping other provinces during partial updates.
    if not args.only_province:
        clear_output_files(txt_output_dir, m3u_output_dir)

    # 流水线处理各省份
    for province in PROVINCES:
        if args.only_province and args.only_province not in province:
            continue
        print(f"\n" + "="*50)
        print(f" 正在处理地区任务: {province}")
        print("="*50)
        process_province(
            province,
            txt_output_dir,
            m3u_output_dir,
            max_pages=args.max_pages,
            max_per_carrier=args.max_per_carrier,
            max_age_hours=args.max_age_hours,
        )

    generated_files = []
    generated_files.extend(
        [os.path.join("txt", f) for f in os.listdir(txt_output_dir) if f.endswith('.txt')]
    )
    generated_files.extend(
        [os.path.join("m3u", f) for f in os.listdir(m3u_output_dir) if f.endswith('.m3u')]
    )
    update_readme_file_list(repo_root)
    generated_files.append(README_FILE)
    if args.push:
        print("\n[] 流水线本地文件生成完毕，准备执行 GitHub 同步...")
        push_to_github(generated_files)
        print("\n[] 史诗级闭环！全网搜源 -> 深度测流 -> 覆盖生成 -> GitHub 发布，全部完成！")
    else:
        print("\n[] 流水线本地文件生成完毕（未启用 --push，跳过 git 推送）。")
        print(f"[] 本次生成文件数量: {len(generated_files)}")

if __name__ == '__main__':
    main()