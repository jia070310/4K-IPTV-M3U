import requests
import os
import re
import time
import subprocess
import argparse
import json
import base64
from datetime import datetime
from html import unescape
from urllib.parse import quote
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# ================= 配置区域 =================
# 1. 组播源网站配置
MULTICAST_SOURCE_URL = "https://blog.cqshushu.com/multicast-iptv"

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


def _encrypt_token(raw_token):
    key = b"cQshuShu88888888"
    cipher = AES.new(key, AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(raw_token.encode("utf-8"), AES.block_size))
    return base64.b64encode(encrypted).decode("utf-8")


def _extract_ajax_config(html):
    m = re.search(r"var\s+multicastIptvAjax\s*=\s*(\{.*?\});", html, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _extract_region_code_map(html):
    code_map = {}
    m = re.search(r'<select\s+name="region"[^>]*>(.*?)</select>', html, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return code_map
    options_html = m.group(1)
    for code, name in re.findall(r'<option\s+value="([^"]*)"\s*[^>]*>(.*?)</option>', options_html, flags=re.IGNORECASE | re.DOTALL):
        code = code.strip()
        if not code:
            continue
        code_map[_strip_html(name)] = code
    return code_map


def _parse_rows_from_html_fragment(fragment_html):
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", fragment_html, flags=re.IGNORECASE | re.DOTALL)
    result = []
    for row_html in rows:
        ip_match = re.search(
            r'<a[^>]*class="[^"]*ip-link[^"]*"[^>]*data-p="([^"]+)"[^>]*>\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+)\s*</a>',
            row_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not ip_match:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.IGNORECASE | re.DOTALL)
        if len(tds) < 6:
            continue
        result.append({
            "p_token": ip_match.group(1).strip(),
            "host": ip_match.group(2).strip(),
            "type": _strip_html(tds[2]),
            "status": _strip_html(tds[5]),
        })
    return result


def fetch_region_rows_by_ajax(province, limit=20, max_pages=30):
    print(f"[*] 正在抓取组播源页面: {MULTICAST_SOURCE_URL}")
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    })
    try:
        home_resp = session.get(MULTICAST_SOURCE_URL, timeout=15)
        home_resp.raise_for_status()
    except Exception as e:
        print(f"[-] 访问组播源页面失败: {e}")
        return []

    home_html = home_resp.text
    ajax_cfg = _extract_ajax_config(home_html)
    code_map = _extract_region_code_map(home_html)
    region_code = code_map.get(province)
    if not ajax_cfg:
        print("[-] 页面中未找到 Ajax 配置。")
        return []
    if not region_code:
        print(f"[-] 页面中未找到省份 [{province}] 的 region code。")
        return []

    all_rows = []
    seen_tokens = set()
    empty_page_hits = 0

    for page_num in range(1, max_pages + 1):
        payload = {
            "action": "multicast_iptv_ajax",
            "action_type": "list",
            "page_num": page_num,
            "limit": limit,
            "region": region_code,
            "search": "",
            "nonce": ajax_cfg.get("nonce", ""),
            "token": _encrypt_token(ajax_cfg.get("token", "")),
        }
        try:
            resp = session.post(ajax_cfg.get("ajaxUrl", ""), data=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[-] Ajax 请求省份 [{province}] 第{page_num}页失败: {e}")
            break
        if not data.get("success"):
            msg = data.get("data", {}).get("message", "unknown error")
            print(f"[-] Ajax 第{page_num}页返回失败: {msg}")
            break

        fragment = data.get("data", {}).get("html", "")
        rows = _parse_rows_from_html_fragment(fragment)
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
    m = re.search(r'data-s="([^"]+)"', detail_html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r'href="[^"]*[?&]s=([^"&]+)', detail_html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1)
    return None

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

def fetch_channel_lines_by_province(province: str, max_per_carrier: int = 5, max_pages: int = 30):
    rows = fetch_region_rows_by_ajax(province, limit=20, max_pages=max_pages)
    if not rows:
        return [], "list_empty", province

    def _is_usable_status(status: str) -> bool:
        return ("新上线" in status) or ("存活" in status)

    def _pick_many(rows_pool, carrier: str, limit: int):
        carrier_rows = [r for r in rows_pool if carrier in r.get("type", "") and _is_usable_status(r.get("status", ""))]
        if not carrier_rows or limit <= 0:
            return []
        new_rows = [r for r in carrier_rows if "新上线" in r.get("status", "")]
        alive_rows = [r for r in carrier_rows if "存活" in r.get("status", "")]
        picked = []
        seen = set()
        for row in (new_rows + alive_rows):
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
            if _is_usable_status(row.get("status", "")):
                selected_rows = [row]
                break

    if not selected_rows:
        return [], "no_new_or_alive", province

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )
    home_resp = session.get(MULTICAST_SOURCE_URL, timeout=15)
    home_resp.raise_for_status()
    ajax_cfg = _extract_ajax_config(home_resp.text)
    if not ajax_cfg:
        return [], "ajax_cfg_missing", province
    token_plain = ajax_cfg.get("token", "")

    group_to_lines: dict[str, list[str]] = {}
    group_to_seen: dict[str, set[str]] = {}
    selected_ops: list[str] = []

    for picked in selected_rows:
        picked_type = picked.get("type", "")
        group_title = normalize_group_title(picked_type, province)
        selected_ops.append(group_title)
        detail_payload = {
            "action": "multicast_iptv_ajax",
            "action_type": "detail",
            "p": picked.get("p_token", ""),
            "nonce": ajax_cfg.get("nonce", ""),
            "token": _encrypt_token(token_plain),
        }
        detail_resp = session.post(ajax_cfg.get("ajaxUrl", ""), data=detail_payload, timeout=15)
        detail_resp.raise_for_status()
        detail_json = detail_resp.json()
        detail_html = detail_json.get("data", {}).get("html", "")
        if not detail_html:
            continue
        token_plain = detail_json.get("data", {}).get("new_token", token_plain)

        s_token = parse_s_token(detail_html)
        if not s_token:
            continue

        channels_payload = {
            "action": "multicast_iptv_ajax",
            "action_type": "channels",
            "s": s_token,
            "nonce": ajax_cfg.get("nonce", ""),
            "token": _encrypt_token(token_plain),
        }
        channels_resp = session.post(ajax_cfg.get("ajaxUrl", ""), data=channels_payload, timeout=15)
        channels_resp.raise_for_status()
        channels_json = channels_resp.json()
        channels_html = channels_json.get("data", {}).get("html", "")
        if not channels_html:
            continue
        lines = parse_channel_lines(channels_html)
        if not lines:
            continue
        bucket = group_to_lines.setdefault(group_title, [])
        bucket_seen = group_to_seen.setdefault(group_title, set())
        for line in lines:
            if line in bucket_seen:
                continue
            bucket_seen.add(line)
            bucket.append(line)

    if not group_to_lines:
        return [], "channel_lines_empty", province

    unique_ops = sorted(set(selected_ops))
    print(f"[*] [{province}] 已提取源数量: {len(selected_rows)}（电信/移动/联通各最多{max_per_carrier}条），来源: {', '.join(unique_ops)}")
    return group_to_lines, "ok", province


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


def update_readme_file_list(repo_root: str) -> None:
    readme_path = os.path.join(repo_root, README_FILE)
    if not os.path.exists(readme_path):
        print("[-] README.md 不存在，跳过列表更新。")
        return
    with open(readme_path, "r", encoding="utf-8") as f:
        content = f.read()

    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

def process_province(province, txt_output_dir, m3u_output_dir, max_pages=30, max_per_carrier=5):
    """单一省份核心流水线"""
    group_title = province
    out_txt = os.path.join(txt_output_dir, f"{group_title}.txt")
    out_m3u = os.path.join(m3u_output_dir, f"{group_title}.m3u")

    # 1. 检测已有文件
    if check_and_clear_existing(out_txt, out_m3u): return

    # 2. 直接从频道列表提取 频道名+播放地址
    grouped_lines, status, _ = fetch_channel_lines_by_province(
        province,
        max_pages=max_pages,
        max_per_carrier=max_per_carrier,
    )
    if not grouped_lines:
        print(f"[-] [{province}] 频道提取失败: {status}")
        return

    # 3. 按运营商分组分别生成 txt/m3u（如：湖北电信、湖北联通、湖北移动）
    total_channels = 0
    exported = 0
    for group_title, channel_lines in grouped_lines.items():
        if not channel_lines:
            continue
        out_txt = os.path.join(txt_output_dir, f"{group_title}.txt")
        out_m3u = os.path.join(m3u_output_dir, f"{group_title}.m3u")
        txt_content = "\n".join(channel_lines)
        with open(out_txt, 'w', encoding='utf-8') as f_txt, open(out_m3u, 'w', encoding='utf-8') as f_m3u:
            f_txt.write(txt_content + "\n")
            f_m3u.write(f'#EXTM3U x-tvg-url="{EPG_URL}"\n')
            f_m3u.write(txt_to_m3u_format(txt_content, group_title) + "\n")
        exported += 1
        total_channels += len(channel_lines)
    if exported == 0:
        print(f"[-] [{province}] 频道提取失败: channel_lines_empty")
        return
    print(f"[+] 完美！[{province}] 更新完成，导出 {total_channels} 条频道，生成 {exported} 个运营商文件。")

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
    return ap.parse_args()


def main():
    args = parse_args()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    txt_output_dir = os.path.join(repo_root, "txt")
    m3u_output_dir = os.path.join(repo_root, "m3u")

    if args.test_region:
        grouped_lines, status, group_title = fetch_channel_lines_by_province(
            args.test_region,
            max_pages=args.max_pages,
            max_per_carrier=args.max_per_carrier,
        )
        total = sum(len(v) for v in grouped_lines.values()) if grouped_lines else 0
        print(f"\n[*] 测试结果: 地区={args.test_region}，分组={group_title}，状态={status}，频道数={total}")
        for k, v in grouped_lines.items():
            print(f"  - {k}: {len(v)} 条")
        return

    os.makedirs(txt_output_dir, exist_ok=True)
    os.makedirs(m3u_output_dir, exist_ok=True)
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