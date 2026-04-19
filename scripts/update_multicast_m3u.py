#!/usr/bin/env python3
"""
Fetch multicast IPTV lists from iptv.cqshushu.com per province, validate streams,
write region M3U files (e.g. hubei4K.m3u) into the repo root or OUTPUT_DIR.

Requires Playwright browsers:  playwright install chromium
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

BASE = "https://blog.cqshushu.com/"
MULTICAST_ENTRY = "https://blog.cqshushu.com/multicast-iptv"
IPTV_MULTICAST_ENTRY = "https://iptv.cqshushu.com/index.php?t=multicast"

# 站点 ancr.js 会检测 navigator.webdriver 等；无头 Chromium 默认 true 会被拦截，页面无 #provinceSelect
_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""

_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
]

class SiteBlockedError(RuntimeError):
    """Raised when anti-bot blocks list interactions."""


def _route_filter(route):
    req = route.request
    # 只拦截大体积静态资源，避免触发目标站点的反广告拦截逻辑
    if req.resource_type in {"image", "media", "font"}:
        return route.abort()
    return route.continue_()

# 站点里明确境外地区，默认不跑（与「国内」批处理一致）
OVERSEAS_REGION_CODES = frozenset({"vn", "ru"})

# province code, Chinese name, output slug (file: {slug}4K.m3u)
REGIONS: list[tuple[str, str, str]] = [
    ("hb", "湖北", "hubei"),
    ("nm", "内蒙古", "neimenggu"),
    ("sc", "四川", "sichuan"),
    ("bj", "北京", "beijing"),
    ("sd", "山东", "shandong"),
    ("he", "河北", "hebei"),
    ("tj", "天津", "tianjin"),
    ("js", "江苏", "jiangsu"),
    ("ah", "安徽", "anhui"),
    ("sn", "陕西", "shaanxi"),
    ("ha", "河南", "henan"),
    ("sh", "上海", "shanghai"),
    ("jl", "吉林", "jilin"),
    ("zj", "浙江", "zhejiang"),
    ("gd", "广东", "guangdong"),
    ("hi", "海南", "hainan"),
    ("hl", "黑龙江", "heilongjiang"),
    ("yn", "云南", "yunnan"),
    ("fj", "福建", "fujian"),
    ("cq", "重庆", "chongqing"),
    ("hn", "湖南", "hunan"),
    ("gz", "贵州", "guizhou"),
    ("tw", "台湾", "taiwan"),
    ("qh", "青海", "qinghai"),
    ("sx", "山西", "shanxi"),
    ("xj", "新疆", "xinjiang"),
    ("gx", "广西", "guangxi"),
    ("gs", "甘肃", "gansu"),
    ("jx", "江西", "jiangxi"),
    ("ln", "辽宁", "liaoning"),
    ("nx", "宁夏", "ningxia"),
    ("vn", "越南", "yuenan"),
    ("ru", "俄罗斯", "eluosi"),
]


@dataclass
class MulticastRow:
    token: str
    ip: str
    type_label: str
    online_at: str


def _parse_time(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.min


def _sub_group(region_zh: str, channel_name: str) -> str:
    n = channel_name.upper()
    if "CCTV" in n or "央视" in channel_name or channel_name.startswith("中央"):
        return "央视"
    if "卫视" in channel_name:
        return "卫视"
    return "其他"


def _build_extinf(region_zh: str, channel: str) -> str:
    sub = _sub_group(region_zh, channel)
    return f'#EXTINF:-1 group-title="{region_zh}地区/{sub}",{channel}'


def _parse_m3u_text(text: str) -> list[tuple[str, str]]:
    """Return list of (channel_name, url) from raw M3U."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: list[tuple[str, str]] = []
    pending_name: str | None = None
    for ln in lines:
        if ln.startswith("#EXTINF"):
            pending_name = ln.rsplit(",", 1)[-1].strip()
        elif not ln.startswith("#") and pending_name:
            if ln.startswith("http://") or ln.startswith("https://"):
                out.append((pending_name, ln))
            pending_name = None
    return out


def _parse_plain_channel_lines(text: str) -> list[tuple[str, str]]:
    """Parse loose 'name,url' lines when not a strict M3U file."""
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        if "," in ln:
            name, maybe_url = ln.split(",", 1)
            name = name.strip()
            maybe_url = maybe_url.strip()
            if name and (maybe_url.startswith("http://") or maybe_url.startswith("https://")):
                out.append((name, maybe_url))
        elif "http://" in ln or "https://" in ln:
            m = re.search(r"(https?://\S+)", ln)
            if m:
                url = m.group(1).strip()
                head = ln.replace(url, "").strip(" ,-|\t")
                out.append((head or "live", url))
    return out


def _extract_m3u_urls_from_text(text: str, base_url: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"https?://[^\s\"'<>]+\.m3u(?:8)?[^\s\"'<>]*", text, flags=re.I):
        u = m.group(0).replace("&amp;", "&")
        if u not in seen:
            seen.add(u)
            out.append(u)
    for m in re.finditer(r'href\s*=\s*"([^"]+\.m3u(?:8)?[^"]*)"', text, flags=re.I):
        h = m.group(1).replace("&amp;", "&")
        u = h if h.startswith("http") else urljoin(base_url, h)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _extract_pairs_from_page_text(page) -> list[tuple[str, str]]:
    """Read textarea/body text and parse loose channel,url lines."""
    buf: list[str] = []
    try:
        tas = page.locator("textarea")
        for i in range(tas.count()):
            try:
                v = tas.nth(i).input_value(timeout=500)
                if v:
                    buf.append(v)
            except Exception:
                continue
    except Exception:
        pass
    try:
        body_txt = page.locator("body").inner_text(timeout=1200)
        if body_txt:
            buf.append(body_txt)
    except Exception:
        pass

    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in buf:
        for name, url in _parse_plain_channel_lines(chunk):
            if url in seen:
                continue
            seen.add(url)
            pairs.append((name, url))
    return pairs


def _rewrite_m3u(region_zh: str, pairs: Iterable[tuple[str, str]]) -> str:
    lines = ["#EXTM3U"]
    for name, url in pairs:
        lines.append(_build_extinf(region_zh, name))
        lines.append(url)
    return "\n".join(lines) + "\n"


def _extract_channel_pairs_from_html(html: str, base_url: str) -> list[tuple[str, str]]:
    """Parse <a href=....m3u8>Title</a> pairs from list HTML."""
    pat = re.compile(
        r'<a[^>]+href=["\'](?P<href>[^"\']+\.m3u8[^"\']*)["\'][^>]*>(?P<title>[^<]+)</a>',
        re.I,
    )
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in pat.finditer(html):
        title = re.sub(r"\s+", " ", m.group("title")).strip()
        href = m.group("href").replace("&amp;", "&")
        if not title or "javascript:" in href.lower():
            continue
        url = href if href.startswith("http") else urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        pairs.append((title, url))
    return pairs


def _collect_m3u8_hrefs(html: str, base_url: str) -> list[str]:
    hrefs = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html, flags=re.I)
    hrefs += re.findall(r'href\s*=\s*"([^"]+\.m3u8[^"]*)"', html, flags=re.I)
    out: list[str] = []
    seen = set()
    for h in hrefs:
        u = h if h.startswith("http") else urljoin(base_url, h)
        u = u.replace("&amp;", "&")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _find_m3u_download_url(html: str, base_url: str) -> str | None:
    for m in re.finditer(r'href\s*=\s*"([^"]+)"', html, flags=re.I):
        href = m.group(1).replace("&amp;", "&")
        if ".m3u" in href.lower() and "javascript:" not in href.lower():
            return urljoin(base_url, href)
    for m in re.finditer(r"https?://[^\s\"'<>]+\.m3u(?:\?[^\s\"'<>]*)?", html, flags=re.I):
        return m.group(0)
    return None


def _probe_m3u8(context, url: str, timeout_ms: int) -> bool:
    try:
        r = context.request.head(url, timeout=timeout_ms)
        if r and r.ok:
            return True
    except Exception:
        pass
    try:
        r = context.request.get(
            url,
            timeout=timeout_ms,
            headers={"Range": "bytes=0-8191"},
        )
        if not r:
            return False
        if r.status >= 400:
            return False
        ct = (r.headers.get("content-type") or "").lower()
        body = r.body() or b""
        if "mpegurl" in ct or "m3u8" in ct or body.lstrip().startswith(b"#EXTM3U"):
            return True
    except Exception:
        return False
    return False


def _extract_rows(page) -> list[MulticastRow]:
    section = page.locator('section[aria-label="组播源列表"]')
    rows = section.locator("tbody tr")
    n = rows.count()
    result: list[MulticastRow] = []
    for i in range(n):
        tr = rows.nth(i)
        try:
            onclick = tr.locator("a.ip-link").get_attribute("onclick") or ""
            m = re.search(r"gotoIP\('([^']+)'\s*,\s*'multicast'\)", onclick)
            if not m:
                continue
            token = m.group(1)
            ip = tr.locator("a.ip-link").inner_text().strip()
            tds = tr.locator("td")
            type_label = ""
            online_at = ""
            for j in range(tds.count()):
                cell = tds.nth(j)
                label = (cell.get_attribute("data-label") or "").strip()
                txt = cell.inner_text().strip()
                if label.startswith("类型"):
                    type_label = txt
                if label.startswith("上线时间"):
                    online_at = txt
            result.append(MulticastRow(token=token, ip=ip, type_label=type_label, online_at=online_at))
        except Exception:
            continue
    return result


def _is_multicast_list_page(url: str) -> bool:
    u = (url or "").lower()
    return "blog.cqshushu.com" in u and "multicast-iptv" in u


def _ensure_multicast_list(page, args) -> bool:
    """进入 blog 组播列表页；若已在列表页则跳过整页 goto。"""
    try:
        if _is_multicast_list_page(page.url) and page.locator('select[name="region"]').count() > 0:
            return True
    except Exception:
        pass
    page.goto(MULTICAST_ENTRY, wait_until="domcontentloaded", timeout=args.timeout_ms)
    try:
        page.wait_for_selector('select[name="region"]', state="visible", timeout=min(30000, args.timeout_ms))
    except Exception as e:
        print(
            f"[skip] blog multicast page missing region select ({e!s}); url={page.url!r}",
            file=sys.stderr,
        )
        raise SiteBlockedError("multicast list blocked")
    page.wait_for_timeout(400)
    return True


def _ensure_iptv_verified(page, args) -> None:
    """Warm up iptv.cqshushu.com JS challenge to set verification cookie."""
    page.goto(IPTV_MULTICAST_ENTRY, wait_until="domcontentloaded", timeout=args.timeout_ms)
    # 页面脚本会写 list_js_verified 并跳转 _js=1；给它一点执行时间
    page.wait_for_timeout(1500)


def _available_region_codes(page) -> set[str]:
    vals: set[str] = set()
    opts = page.locator('select[name="region"] option')
    for i in range(opts.count()):
        v = (opts.nth(i).get_attribute("value") or "").strip().lower()
        if v:
            vals.add(v)
    return vals


def _pick_row(rows: list[MulticastRow], region_zh: str) -> MulticastRow | None:
    if not rows:
        return None
    # Prefer rows whose 类型 mentions the province name
    tagged = [r for r in rows if region_zh in r.type_label]
    pool = tagged if tagged else rows
    # Newest 上线时间 first
    pool.sort(key=lambda r: _parse_time(r.online_at), reverse=True)
    # Prefer 新上线 if status column exists (optional)
    return pool[0]


def process_region(
    page,
    context,
    code: str,
    region_zh: str,
    slug: str,
    args,
    *,
    set_limit: bool,
) -> str | None:
    debug_dir = Path(args.output_dir).resolve() / "_debug"
    if args.debug_dump:
        debug_dir.mkdir(parents=True, exist_ok=True)
    _ensure_multicast_list(page, args)
    page.wait_for_timeout(150)
    if set_limit:
        try:
            page.locator('select[name="limit"]').select_option(str(args.per_page), timeout=8000)
            page.wait_for_timeout(250)
        except Exception:
            pass
    try:
        page.locator('select[name="region"]').select_option(code, timeout=8000)
        page.locator(".btn-search").click(timeout=5000)
    except Exception as e:
        print(f"[skip] {region_zh}: province select failed: {e!s}", file=sys.stderr)
        return None
    page.wait_for_timeout(500)
    try:
        page.wait_for_selector("table.hotel-iptv-table tbody tr", state="visible", timeout=10000)
    except Exception:
        pass

    target_idx = -1
    target_page = 1
    for page_no in range(1, max(1, args.max_region_pages) + 1):
        tr_list = page.locator("table.hotel-iptv-table tbody tr")
        n = tr_list.count()
        for i in range(n):
            tr = tr_list.nth(i)
            tds = tr.locator("td")
            if tds.count() < 6:
                continue
            status = tds.nth(5).inner_text().strip()
            typ = tds.nth(2).inner_text().strip()
            if region_zh not in typ:
                continue
            # 仅选“新上线”状态（按用户要求）
            if "新上线" in status:
                target_idx = i
                target_page = page_no
                break
        if target_idx >= 0:
            break
        # 翻到下一页继续找“新上线”
        if page_no < args.max_region_pages:
            pager = page.locator(f'a.page-link[data-page="{page_no + 1}"]')
            if pager.count() == 0:
                break
            try:
                pager.first.click(timeout=4000)
                page.wait_for_timeout(600)
            except Exception:
                break

    if target_idx < 0:
        print(f"[skip] {region_zh}: no multicast rows", file=sys.stderr)
        return None
    print(f"[info] {region_zh}: found 新上线 on page {target_page}", file=sys.stderr)
    tr_list = page.locator("table.hotel-iptv-table tbody tr")
    tr = tr_list.nth(target_idx)
    token = (tr.locator("a.ip-link").first.get_attribute("data-p") or "").strip()
    if not token:
        print(f"[skip] {region_zh}: missing token data-p", file=sys.stderr)
        return None
    detail_url = f"https://iptv.cqshushu.com/index.php?p={token}&t=multicast"
    page.goto(detail_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
    page.wait_for_timeout(800)
    html = page.content()
    if args.debug_dump:
        (debug_dir / f"{slug}_detail_before.html").write_text(html, encoding="utf-8", errors="ignore")
    # 若被 iptv 站点拦截，先做一次域名验证后重试详情页
    if ("验证失败" in html) or ("请求失败" in html):
        _ensure_iptv_verified(page, args)
        page.goto(detail_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
        page.wait_for_timeout(900)
        html = page.content()
        if args.debug_dump:
            (debug_dir / f"{slug}_detail_retry.html").write_text(html, encoding="utf-8", errors="ignore")

    # 捕获点击后的网络响应（有些页面把频道数据放在 XHR，不直接渲染到 DOM）
    net_payloads: list[str] = []
    net_urls: list[str] = []

    def _on_response(resp):
        try:
            u = resp.url.lower()
            net_urls.append(resp.url)
            if (
                "admin-ajax.php" in u
                or "iptv.cqshushu.com/index.php" in u
                or ".m3u" in u
                or ".m3u8" in u
            ):
                txt = resp.text()
                if txt and ("m3u" in txt.lower() or "http" in txt.lower()):
                    net_payloads.append(txt)
        except Exception:
            pass

    page.on("response", _on_response)

    # 查看频道列表
    for name in ("查看频道列表", "频道列表"):
        loc = page.get_by_text(name, exact=False)
        if loc.count() == 0:
            continue
        try:
            loc.first.click(timeout=5000)
            page.wait_for_timeout(1000)
            html = page.content()
            break
        except Exception:
            continue
    page.wait_for_timeout(500)
    page.remove_listener("response", _on_response)
    if args.debug_dump:
        (debug_dir / f"{slug}_channel_page.html").write_text(page.content(), encoding="utf-8", errors="ignore")
        if net_urls:
            (debug_dir / f"{slug}_net_urls.txt").write_text("\n".join(net_urls[:200]), encoding="utf-8")

    m3u_url = _find_m3u_download_url(html, page.url)
    pairs: list[tuple[str, str]] | None = None

    # 优先点“M3U下载”拿真实文件，避免页面不直接暴露 m3u8 链接
    for dl_text in ("M3U下载", "下载M3U", "下载 m3u", "m3u 下载"):
        dl = page.get_by_text(dl_text, exact=False)
        if dl.count() == 0:
            continue
        try:
            with page.expect_download(timeout=7000) as dlinfo:
                dl.first.click(timeout=4000)
            download = dlinfo.value
            tmp = Path(args.output_dir).resolve() / ".tmp_download.m3u"
            download.save_as(str(tmp))
            raw = tmp.read_text(encoding="utf-8", errors="ignore")
            tmp.unlink(missing_ok=True)
            pairs = _parse_m3u_text(raw) or _parse_plain_channel_lines(raw)
            if pairs:
                break
        except Exception:
            continue

    if (not pairs) and m3u_url:
        try:
            r = context.request.get(m3u_url, timeout=args.timeout_ms)
            if r.ok:
                raw = r.text()
                pairs = _parse_m3u_text(raw) or _parse_plain_channel_lines(raw)
        except Exception:
            pairs = None

    if not pairs:
        txt_pairs = _extract_pairs_from_page_text(page)
        if txt_pairs:
            pairs = txt_pairs

    if not pairs and net_payloads:
        # 先尝试把接口返回当作 M3U / 文本频道列表解析
        for payload in net_payloads:
            pairs = _parse_m3u_text(payload) or _parse_plain_channel_lines(payload)
            if pairs:
                break
        # 再兜底从接口返回里抽 URL
        if not pairs:
            all_urls: list[str] = []
            for payload in net_payloads:
                all_urls.extend(_extract_m3u_urls_from_text(payload, page.url))
            if all_urls:
                first = all_urls[0]
                nm = urlparse(first).path.split("/")[-1].split("?")[0] or "live"
                pairs = [(nm.replace(".m3u8", "").replace(".m3u", "") or "live", first)]

    if not pairs:
        # 取消 m3u8 可播校验：频道列表里有链接就直接落盘
        anchor_pairs = _extract_channel_pairs_from_html(html, page.url)
        if anchor_pairs:
            pairs = anchor_pairs

    if not pairs:
        m3u8s = _collect_m3u8_hrefs(html, page.url)
        if m3u8s:
            u = m3u8s[0]
        else:
            print(f"[skip] {region_zh}: no m3u8 links found", file=sys.stderr)
            return None
        path = urlparse(u).path.split("/")[-1].split("?")[0] or "live"
        ch = path.replace(".m3u8", "") or "live"
        pairs = [(ch, u)]

    return _rewrite_m3u(region_zh, pairs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--output-dir",
        default=os.environ.get("GITHUB_WORKSPACE", "."),
        help="Directory to write *4K.m3u files",
    )
    ap.add_argument("--timeout-ms", type=int, default=120000)
    ap.add_argument("--probe-timeout-ms", type=int, default=15000)
    ap.add_argument("--per-page", type=int, default=10, help="Rows per page (site select limit)")
    ap.add_argument("--test-top-n", type=int, default=8, help="How many m3u8 URLs to probe")
    ap.add_argument("--regions", default="", help="Comma province codes, e.g. hb,sc (default: all)")
    ap.add_argument("--stop-on-consecutive-fail", type=int, default=4)
    ap.add_argument("--max-region-pages", type=int, default=4, help="Max pages to scan per region for 新上线 rows")
    ap.add_argument("--debug-dump", action="store_true", help="Dump debug html/network files under _debug/")
    ap.add_argument(
        "--include-overseas",
        action="store_true",
        help="Also scrape vn/ru (Vietnam, Russia); default is domestic-only.",
    )
    args = ap.parse_args()

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    want = {x.strip().lower() for x in args.regions.split(",") if x.strip()}
    skip_overseas = OVERSEAS_REGION_CODES if not args.include_overseas else frozenset()
    base = [(c, z, s) for c, z, s in REGIONS if c not in skip_overseas]
    regions = [(c, z, s) for c, z, s in base if not want or c in want]

    with sync_playwright() as p:
        launch_kw = {"headless": True, "args": _CHROMIUM_ARGS}
        browser = None
        if os.environ.get("GITHUB_ACTIONS") == "true":
            try:
                browser = p.chromium.launch(channel="chrome", **launch_kw)
            except Exception:
                browser = None
        if browser is None:
            browser = p.chromium.launch(**launch_kw)
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
            if platform.system() == "Linux"
            else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        context = browser.new_context(
            locale="zh-CN",
            viewport={"width": 1365, "height": 900},
            user_agent=ua,
            accept_downloads=True,
        )
        context.add_init_script(_STEALTH_INIT)
        page = context.new_page()
        context.route("**/*", _route_filter)
        if not _ensure_multicast_list(page, args):
            print("[fatal] cannot open multicast list page", file=sys.stderr)
            browser.close()
            return 1
        # 先在 iptv 域名完成一次 JS 验证，后续详情页才能正常拿到频道按钮
        try:
            _ensure_iptv_verified(page, args)
            _ensure_multicast_list(page, args)
        except Exception as e:
            print(f"[warn] iptv verification warmup failed: {e!s}", file=sys.stderr)
        available_codes = _available_region_codes(page)
        consecutive_fail = 0
        for i, (code, zh, slug) in enumerate(regions):
            if code not in available_codes:
                print(f"[skip] {zh}: region code {code} not in page options", file=sys.stderr)
                continue
            path = out_dir / f"{slug}4K.m3u"
            try:
                text = process_region(
                    page,
                    context,
                    code,
                    zh,
                    slug,
                    args,
                    set_limit=(i == 0),
                )
                if text:
                    path.write_text(text, encoding="utf-8")
                    print(f"[ok] {path.name} ({zh})")
                # “无数据/无可播链接”不计入风控失败，避免误中断
                consecutive_fail = 0
                time.sleep(0.25)
            except SiteBlockedError as e:
                consecutive_fail += 1
                print(f"[warn] blocked signal: {e}", file=sys.stderr)
            except Exception as e:
                print(f"[err] {zh}: {e}", file=sys.stderr)
            if consecutive_fail >= args.stop_on_consecutive_fail:
                print(
                    f"[abort] consecutive failures reached {consecutive_fail}; stop early to avoid wasting CI time",
                    file=sys.stderr,
                )
                break
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
