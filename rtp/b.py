import requests
import os
import re
import time
import subprocess
import argparse
import json
import base64
from html import unescape
from urllib.parse import quote
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# ================= 配置区域 =================
# 1. 组播源网站配置
TEMPLATE_DIR = "rtp"                                  # 母版文件夹名称
MULTICAST_SOURCE_URL = "https://blog.cqshushu.com/multicast-iptv"

# 2. GitHub 推送配置
# 提交说明前缀；为空时使用默认文案
GITHUB_COMMIT_PREFIX = "Auto update"
# ============================================
EPG_URL = "http://epg.51zmt.top:8000/e.xml.gz"
TVG_LOGO_BASE_URL = "https://gcore.jsdelivr.net/gh/taksssss/tv/icon/"

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

def extract_province(filename):
    """智能识别省份"""
    for p in PROVINCES:
        if p in filename: return p
    return None

def check_and_clear_existing(txt_file, m3u_file):
    """不做测流，直接清空旧文件并重新导出。"""
    if not os.path.exists(txt_file):
        return False
    print(f"[*] 不做测流，清空旧文件后重新导出...")
    for file in [txt_file, m3u_file]:
        with open(file, 'w', encoding='utf-8') as f: f.write("")
    return False

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
        ip_match = re.search(r"<a[^>]*>\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+)\s*</a>", row_html, flags=re.IGNORECASE)
        if not ip_match:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.IGNORECASE | re.DOTALL)
        if len(tds) < 6:
            continue
        result.append({
            "host": ip_match.group(1).strip(),
            "type": _strip_html(tds[2]),
            "status": _strip_html(tds[5]),
        })
    return result


def fetch_region_rows_by_ajax(province, limit=20):
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

    payload = {
        "action": "multicast_iptv_ajax",
        "action_type": "list",
        "page_num": 1,
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
        print(f"[-] Ajax 请求省份 [{province}] 失败: {e}")
        return []
    if not data.get("success"):
        msg = data.get("data", {}).get("message", "unknown error")
        print(f"[-] Ajax 返回失败: {msg}")
        return []
    fragment = data.get("data", {}).get("html", "")
    rows = _parse_rows_from_html_fragment(fragment)
    print(f"[*] [{province}] Ajax 返回 {len(rows)} 条服务器。")
    return rows


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

def process_province(template_filename, template_dir, txt_output_dir, m3u_output_dir, source_rows=None):
    """单一省份核心流水线"""
    province = extract_province(template_filename)
    if not province: return

    template_path = os.path.join(template_dir, template_filename)
    out_txt = os.path.join(txt_output_dir, template_filename)
    out_m3u = os.path.join(m3u_output_dir, template_filename.replace('.txt', '.m3u'))
    group_title = os.path.splitext(template_filename)[0]

    # 1. 检测已有文件
    if check_and_clear_existing(out_txt, out_m3u): return

    # 2. 读取母版内容
    with open(template_path, 'r', encoding='utf-8') as f:
        template_content = f.read()
    
    # 动态提取前5个组播靶标，避免单个频道停播导致误判
    targets = extract_test_targets(template_content, max_targets=5)
    if not targets:
        return
    print(f"[*] 成功提取 [{province}] 测试靶标数量: {len(targets)}")

    # 3. 获取网站资产并按状态筛选
    region_all, assets = get_region_assets(province, source_rows)
    print(f"[*] [{province}] 地区共提取到 {len(region_all)} 条服务器。")
    if not assets:
        return
    # 候选服务器去重（最多5条），不做测流，直接用于导出
    candidate_hosts, seen_root_domains = [], set()
    for item in assets:
        host = item.get("host")
        if not host:
            continue
        pure_domain = host.split(':')[0]
        root_domain = get_root_domain(pure_domain)
        if root_domain in seen_root_domains:
            continue
        seen_root_domains.add(root_domain)
        candidate_hosts.append(host)
        if len(candidate_hosts) >= 5:
            break

    if not candidate_hosts:
        print(f"[-] [{province}] 没有可导出的候选服务器。")
        return
    print(f"[*] [{province}] 已选候选服务器数量: {len(candidate_hosts)}（跳过测流）")

    # 5. 克隆母版生成纯净文件
    if candidate_hosts:
        pattern = re.compile(r'(?:https?://[^/,]+/)?(udp|rtp|igmp)(?:/|://)(\d+\.\d+\.\d+\.\d+:\d+)', re.IGNORECASE)
        with open(out_txt, 'w', encoding='utf-8') as f_txt, open(out_m3u, 'w', encoding='utf-8') as f_m3u:
            f_m3u.write(f'#EXTM3U x-tvg-url="{EPG_URL}"\n')
            for host in candidate_hosts:
                new_txt_block = pattern.sub(f'http://{host}/\\1/\\2', template_content)
                f_txt.write(new_txt_block + "\n\n")
                f_m3u.write(txt_to_m3u_format(new_txt_block, group_title) + "\n\n")
        print(f"[+] 完美！[{province}] 更新完成，导出 {len(candidate_hosts)} 个规则节点。")
    else:
        print(f"[-] [{province}] 本次无可导出节点。")

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
    ap = argparse.ArgumentParser(description="RTP 模板搜源并生成省份 txt/m3u。")
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
        help="仅处理文件名中包含该省份的模板。例如：--only-province 湖北",
    )
    return ap.parse_args()


def main():
    args = parse_args()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    template_dir = os.path.join(script_dir, TEMPLATE_DIR)
    txt_output_dir = os.path.join(repo_root, "txt")
    m3u_output_dir = os.path.join(repo_root, "m3u")

    if args.test_region:
        region_all, preferred = get_region_assets(args.test_region)
        print(f"\n[*] 测试结果: 地区={args.test_region}，提取总数={len(region_all)}，可用候选数={len(preferred)}")
        for row in region_all:
            print(f"  - {row['host']} | {row['type']} | {row['status']}")
        if preferred:
            chosen = preferred[0]
            print(f"[*] 最终选中: {chosen['host']} | {chosen['status']}")
        return

    if not os.path.exists(template_dir):
        os.makedirs(template_dir)
        print(f"[!] 没有找到 '{template_dir}' 目录，已自动创建。请放入模板后重新运行！")
        return

    os.makedirs(txt_output_dir, exist_ok=True)
    os.makedirs(m3u_output_dir, exist_ok=True)

    template_files = [f for f in os.listdir(template_dir) if f.endswith('.txt')]
    if not template_files:
        print(f"[!] '{template_dir}' 目录中空空如也，请放入各省市的模板文件。")
        return

    # 流水线处理各省份
    for filename in template_files:
        if args.only_province and args.only_province not in filename:
            continue
        print(f"\n" + "="*50)
        print(f" 正在处理兵工厂任务: {filename}")
        print("="*50)
        process_province(filename, template_dir, txt_output_dir, m3u_output_dir)

    generated_files = []
    generated_files.extend(
        [os.path.join("txt", f) for f in os.listdir(txt_output_dir) if f.endswith('.txt')]
    )
    generated_files.extend(
        [os.path.join("m3u", f) for f in os.listdir(m3u_output_dir) if f.endswith('.m3u')]
    )
    if args.push:
        print("\n[] 流水线本地文件生成完毕，准备执行 GitHub 同步...")
        push_to_github(generated_files)
        print("\n[] 史诗级闭环！全网搜源 -> 深度测流 -> 覆盖生成 -> GitHub 发布，全部完成！")
    else:
        print("\n[] 流水线本地文件生成完毕（未启用 --push，跳过 git 推送）。")
        print(f"[] 本次生成文件数量: {len(generated_files)}")

if __name__ == '__main__':
    main()