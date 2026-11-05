#!/usr/bin/env python3
"""
微博 UID 博文拉取可行性探测工具（抽样验证，非全量爬虫）。

用途：
  - 用少量 UID 实测：能否按 uid 拉到时间线、是否触发登录墙/限流、耗时与成功率
  - 据此估算「9 万用户全量博文」所需请求量与不可行点

默认严格限流、限制 UID/分页数量，避免误用成批量采集器。
仅做可行性探测；生产采集请走微博开放平台授权接口。
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    import brotli as _brotli
except ImportError:  # pragma: no cover
    _brotli = None


DEFAULT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
    "Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
TIMELINE_URL = "https://m.weibo.cn/api/container/getIndex"
PROFILE_URL = "https://api.weibo.cn/2/profile"
GUEST_CARDLIST_URL = "https://api.weibo.cn/2/guest/cardlist"
PC_PROFILE_URL = "https://weibo.com/ajax/profile/info"
PC_TIMELINE_URL = "https://weibo.com/ajax/statuses/mymblog"
MP_CARDLIST_URL = "https://api.weibo.cn/2/cardlist"
MP_PROFILE_STATUSES_URL = "https://api.weibo.cn/2/profile/statuses"
MP_USERS_SHOW_URL = "https://api.weibo.cn/2/users/show"
MP_UNREAD_URL = "https://api.weibo.cn/2/remind/unread_count"
TIMELINE_CONTAINER_PREFIX = "107603"
# 微信小程序客户端标识（非用户密钥；用户密钥是 gsid）
MP_DEFAULT_FROM = "1885396040"
MP_DEFAULT_WM = "90163_90001"
MP_DEFAULT_S = "5f858072"
MP_REFERER = "https://servicewechat.com/wx9074de28009e1111/296/page-frame.html"
MP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF "
    "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541f38) XWEB/25819"
)


@dataclass
class PageResult:
    uid: str
    endpoint: str
    page: int
    ok: bool
    http_status: int | None
    latency_ms: float
    posts: int = 0
    screen_name: str | None = None
    total_hint: int | None = None
    error: str | None = None
    auth_wall: bool = False
    rate_limited: bool = False
    final_url: str | None = None
    raw_ok: Any = None


@dataclass
class UidSummary:
    uid: str
    screen_name: str | None = None
    profile_ok: bool = False
    pages_ok: int = 0
    pages_tried: int = 0
    posts_fetched: int = 0
    total_hint: int | None = None
    auth_wall: bool = False
    rate_limited: bool = False
    errors: list[str] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="通过 UID 抽样验证微博博文拉取可行性"
    )
    p.add_argument(
        "--uids",
        nargs="*",
        default=[],
        help="UID 列表，例如 --uids 2803301701 1669879400",
    )
    p.add_argument(
        "--uids-file",
        help="每行一个 UID 的文件路径",
    )
    p.add_argument(
        "--cookie",
        default=os.environ.get("WEIBO_COOKIE", ""),
        help="可选 Cookie 字符串（也可用环境变量 WEIBO_COOKIE）。勿把真实 Cookie 提交进仓库。",
    )
    p.add_argument(
        "--cookie-file",
        help="从文件读取 Cookie（推荐：一行完整 Cookie，避免出现在 shell 历史里）",
    )
    p.add_argument(
        "--gsid",
        default=os.environ.get("WEIBO_GSID", ""),
        help="小程序 gsid（也可用环境变量 WEIBO_GSID）。勿提交仓库/勿贴聊天。",
    )
    p.add_argument(
        "--gsid-file",
        help="从文件读取 gsid（一行）",
    )
    p.add_argument(
        "--mp-s",
        default=os.environ.get("WEIBO_MP_S", MP_DEFAULT_S),
        help="小程序参数 s（客户端标识，默认内置）",
    )
    p.add_argument(
        "--mp-from",
        default=os.environ.get("WEIBO_MP_FROM", MP_DEFAULT_FROM),
        help="小程序参数 from",
    )
    p.add_argument(
        "--mp-wm",
        default=os.environ.get("WEIBO_MP_WM", MP_DEFAULT_WM),
        help="小程序参数 wm",
    )
    p.add_argument(
        "--max-uids",
        type=int,
        default=5,
        help="最多探测多少个 UID（默认 5，上限 20）",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=2,
        help="每个 UID 最多翻页数（默认 2，上限 10）",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="请求间隔秒（默认 1.5）",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="单请求超时秒",
    )
    p.add_argument(
        "--target-users",
        type=int,
        default=90_000,
        help="估算规模用的目标用户数（默认 90000）",
    )
    p.add_argument(
        "--assume-posts-per-user",
        type=int,
        default=500,
        help="估算时假设人均博文数（默认 500）",
    )
    p.add_argument(
        "--page-size",
        type=int,
        default=20,
        help="估算时假设每页条数（默认 20）",
    )
    p.add_argument(
        "--out",
        default="probe_report.json",
        help="报告输出路径",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="不发真实请求，只打印将要探测的 UID 与估算公式",
    )
    args = p.parse_args()
    args.max_uids = max(1, min(args.max_uids, 20))
    args.max_pages = max(1, min(args.max_pages, 10))
    args.delay = max(0.5, args.delay)
    return args


def load_uids(args: argparse.Namespace) -> list[str]:
    uids: list[str] = []
    uids.extend(args.uids or [])
    if args.uids_file:
        with open(args.uids_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                uids.append(line.split()[0])
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for u in uids:
        if not u.isdigit():
            print(f"[warn] 跳过非数字 UID: {u}", file=sys.stderr)
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[: args.max_uids]


def _read_secret_line(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            return line
    return ""


def resolve_cookie(args: argparse.Namespace) -> str:
    cookie = (args.cookie or "").strip()
    if args.cookie_file:
        cookie = _read_secret_line(args.cookie_file) or cookie
    return cookie


def resolve_gsid(args: argparse.Namespace) -> str:
    gsid = (args.gsid or "").strip()
    if args.gsid_file:
        gsid = _read_secret_line(args.gsid_file) or gsid
    return gsid


def cookie_diagnostics(cookie: str) -> dict[str, Any]:
    """粗检 Cookie 是否像登录态（不打印任何 Cookie 值）。"""
    names = set()
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        names.add(part.split("=", 1)[0].strip())
    important = ["SUB", "SUBP", "SCF", "SUHB", "_T_WM", "XSRF-TOKEN", "MLOGIN", "SSOLoginState"]
    present = [n for n in important if n in names]
    return {
        "length": len(cookie),
        "key_count": len(names),
        "has_sub": "SUB" in names,
        "has_subp": "SUBP" in names,
        "important_keys_present": present,
        "looks_logged_in": "SUB" in names and "SUBP" in names,
    }


def build_opener(
    cookie: str,
    *,
    desktop: bool = False,
) -> urllib.request.OpenerDirector:
    handlers = [urllib.request.HTTPRedirectHandler()]
    opener = urllib.request.build_opener(*handlers)
    headers = {
        "User-Agent": DESKTOP_UA if desktop else DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://weibo.com/" if desktop else "https://m.weibo.cn/",
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookie:
        headers["Cookie"] = cookie
    opener.addheaders = list(headers.items())
    return opener


def _decode_http_body(raw: bytes, headers: Any) -> str:
    enc = ""
    try:
        enc = (headers.get("Content-Encoding") if headers else "") or ""
    except Exception:  # noqa: BLE001
        enc = ""
    enc = str(enc).lower()
    if enc == "br" or (raw and _brotli is not None and raw[:2] != b"\x1f\x8b"):
        if _brotli is not None:
            try:
                return _brotli.decompress(raw).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    return raw.decode("utf-8", errors="replace")


def fetch_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    timeout: float,
) -> tuple[int | None, float, Any, str | None, str | None]:
    started = time.perf_counter()
    try:
        with opener.open(url, timeout=timeout) as resp:
            body = resp.read()
            latency = (time.perf_counter() - started) * 1000
            final_url = resp.geturl()
            status = getattr(resp, "status", None) or resp.getcode()
            text = _decode_http_body(body, resp.headers)
            if "passport.weibo" in final_url or "visitor.passport" in final_url:
                return status, latency, None, "auth_wall_redirect", final_url
            if not text.strip():
                return status, latency, None, "empty_body", final_url
            try:
                return status, latency, json.loads(text), None, final_url
            except json.JSONDecodeError:
                snippet = text[:200].replace("\n", " ")
                if "login" in text.lower() or "passport" in text.lower():
                    return status, latency, None, "auth_wall_html", final_url
                return status, latency, None, f"non_json:{snippet}", final_url
    except urllib.error.HTTPError as e:
        latency = (time.perf_counter() - started) * 1000
        raw = e.read() if e.fp else b""
        body = _decode_http_body(raw, getattr(e, "headers", None)) if raw else ""
        err = f"http_{e.code}"
        if e.code in (403, 418, 429, 432):
            err = f"rate_or_forbidden_{e.code}"
        if "passport" in body.lower() or "login" in body.lower():
            err = "auth_wall_http"
        return e.code, latency, None, err, e.geturl() if hasattr(e, "geturl") else None
    except Exception as e:  # noqa: BLE001 - probe should never crash mid-run
        latency = (time.perf_counter() - started) * 1000
        return None, latency, None, f"{type(e).__name__}:{e}", None


def build_mp_opener(gsid: str, mp_s: str, mp_from: str, mp_wm: str) -> tuple[urllib.request.OpenerDirector, dict[str, str]]:
    """返回 opener + 公共 query 参数（不含业务 uid/page）。"""
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    opener.addheaders = [
        ("User-Agent", MP_UA),
        ("Accept", "*/*"),
        ("Content-Type", "application/json"),
        ("Referer", MP_REFERER),
        ("xweb_xhr", "1"),
        ("Accept-Encoding", "gzip, deflate, br"),
        ("Accept-Language", "zh-CN,zh;q=0.9"),
    ]
    common = {
        "c": "weixinminiprogram",
        "s": mp_s,
        "from": mp_from,
        "wm": mp_wm,
        "lang": "zh_CN",
        "gsid": gsid,
    }
    return opener, common


def mp_url(path: str, common: dict[str, str], extra: dict[str, str]) -> str:
    q = dict(common)
    q.update(extra)
    return f"https://api.weibo.cn{path}?{urllib.parse.urlencode(q)}"


_SENSITIVE_QUERY_KEYS = {
    "gsid",
    "cookie",
    "sub",
    "subp",
    "scf",
    "wbpsess",
    "access_token",
    "aid",
}


def redact_url(url: str | None) -> str | None:
    """报告里去掉 gsid 等敏感 query，避免密钥落盘。"""
    if not url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        redacted = []
        for k, v in q:
            if k.lower() in _SENSITIVE_QUERY_KEYS:
                redacted.append((k, "[REDACTED]"))
            else:
                redacted.append((k, v))
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(redacted), parts.fragment)
        )
    except Exception:  # noqa: BLE001
        return "[redact_failed]"


def probe_mp_users_show(
    opener: urllib.request.OpenerDirector,
    common: dict[str, str],
    uid: str,
    timeout: float,
) -> PageResult:
    url = mp_url("/2/users/show", common, {"uid": uid})
    status, latency, data, err, final_url = fetch_json(opener, url, timeout)
    screen_name = None
    total_hint = None
    posts = 0
    ok = False
    if isinstance(data, dict) and data.get("id") is not None and not data.get("errno"):
        screen_name = data.get("screen_name")
        if isinstance(data.get("statuses_count"), int):
            total_hint = data["statuses_count"]
        if isinstance(data.get("status"), dict):
            posts = 1
        ok = True
    elif isinstance(data, dict) and data.get("errno"):
        err = err or f"errno:{data.get('errno')}:{data.get('errmsg')}"
    return PageResult(
        uid=uid,
        endpoint="api.weibo.cn/2/users/show",
        page=0,
        ok=ok,
        http_status=status,
        latency_ms=round(latency, 1),
        posts=posts,
        screen_name=screen_name,
        total_hint=total_hint,
        error=err,
        auth_wall=bool(err and "auth" in err),
        rate_limited=bool(err and "rate_or_forbidden" in (err or "")),
        final_url=final_url,
        raw_ok=None if not isinstance(data, dict) else data.get("errno"),
    )


def probe_mp_cardlist(
    opener: urllib.request.OpenerDirector,
    common: dict[str, str],
    uid: str,
    page: int,
    timeout: float,
) -> PageResult:
    containerid = f"{TIMELINE_CONTAINER_PREFIX}{uid}"
    url = mp_url(
        "/2/cardlist",
        common,
        {"containerid": containerid, "page": str(page), "count": "20"},
    )
    status, latency, data, err, final_url = fetch_json(opener, url, timeout)
    posts = 0
    total_hint = None
    auth_wall = False
    if isinstance(data, dict):
        if data.get("errno"):
            err = err or f"errno:{data.get('errno')}:{data.get('errmsg')}"
        posts, total_hint = _count_mblogs(data)
        info = data.get("cardlistInfo")
        if isinstance(info, dict) and isinstance(info.get("total"), int):
            total_hint = info["total"]
        # 登录引导
        aw, err2 = detect_auth_wall(data, err, final_url)
        auth_wall, err = aw, err2
    ok = status == 200 and err is None and not auth_wall and isinstance(data, dict) and "cards" in data
    return PageResult(
        uid=uid,
        endpoint="api.weibo.cn/2/cardlist",
        page=page,
        ok=ok,
        http_status=status,
        latency_ms=round(latency, 1),
        posts=posts,
        total_hint=total_hint,
        error=err,
        auth_wall=auth_wall,
        rate_limited=bool(err and "rate_or_forbidden" in (err or "")),
        final_url=final_url,
        raw_ok=None if not isinstance(data, dict) else data.get("errno"),
    )


def probe_mp_profile_statuses(
    opener: urllib.request.OpenerDirector,
    common: dict[str, str],
    uid: str,
    page: int,
    timeout: float,
) -> PageResult:
    containerid = f"{TIMELINE_CONTAINER_PREFIX}{uid}"
    url = mp_url(
        "/2/profile/statuses",
        common,
        {"uid": uid, "containerid": containerid, "page": str(page), "count": "20"},
    )
    status, latency, data, err, final_url = fetch_json(opener, url, timeout)
    posts = 0
    total_hint = None
    auth_wall = False
    if isinstance(data, dict):
        if data.get("errno"):
            err = err or f"errno:{data.get('errno')}:{data.get('errmsg')}"
        posts, total_hint = _count_mblogs(data)
        info = data.get("cardlistInfo")
        if isinstance(info, dict) and isinstance(info.get("total"), int):
            total_hint = info["total"]
        aw, err2 = detect_auth_wall(data, err, final_url)
        auth_wall, err = aw, err2
    ok = (
        status == 200
        and err is None
        and not auth_wall
        and isinstance(data, dict)
        and ("cards" in data or "statuses" in data)
    )
    return PageResult(
        uid=uid,
        endpoint="api.weibo.cn/2/profile/statuses",
        page=page,
        ok=ok,
        http_status=status,
        latency_ms=round(latency, 1),
        posts=posts,
        total_hint=total_hint,
        error=err,
        auth_wall=auth_wall,
        rate_limited=bool(err and "rate_or_forbidden" in (err or "")),
        final_url=final_url,
        raw_ok=None if not isinstance(data, dict) else data.get("errno"),
    )


def _count_mblogs(payload: dict[str, Any]) -> tuple[int, int | None]:
    posts = 0
    total_hint = None
    cards = payload.get("cards")
    if isinstance(cards, list):
        for card in cards:
            if not isinstance(card, dict):
                continue
            if card.get("mblog"):
                posts += 1
            card_group = card.get("card_group")
            if isinstance(card_group, list):
                posts += sum(1 for c in card_group if isinstance(c, dict) and c.get("mblog"))
            # 登录引导卡
            desc = str(card.get("desc") or "")
            if "登录" in desc:
                pass
    info = payload.get("cardlistInfo")
    if isinstance(info, dict) and isinstance(info.get("total"), int):
        total_hint = info["total"]
    statuses = payload.get("statuses")
    if isinstance(statuses, list):
        posts += len(statuses)
    return posts, total_hint


def detect_auth_wall(data: Any, err: str | None, final_url: str | None) -> tuple[bool, str | None]:
    if err and "auth_wall" in err:
        return True, err
    if final_url and ("passport.weibo" in final_url or "visitor.passport" in final_url):
        return True, err or "auth_wall_redirect"
    if not isinstance(data, dict):
        return False, err
    ok_val = data.get("ok")
    # m.weibo.cn: ok=-100 表示需登录
    if ok_val in (-100, "-100"):
        return True, "auth_wall_ok_-100"
    msg = str(data.get("msg") or data.get("errmsg") or "")
    if "登录" in msg:
        return True, err or f"auth_wall_msg:{msg}"
    cards = None
    if isinstance(data.get("cards"), list):
        cards = data["cards"]
    elif isinstance(data.get("data"), dict) and isinstance(data["data"].get("cards"), list):
        cards = data["data"]["cards"]
    if isinstance(cards, list):
        for card in cards:
            if isinstance(card, dict) and "登录" in str(card.get("desc") or ""):
                return True, err or "auth_wall_login_card"
        title = None
        info = data.get("cardlistInfo") or (
            data.get("data", {}).get("cardlistInfo") if isinstance(data.get("data"), dict) else None
        )
        if isinstance(info, dict):
            title = info.get("title")
        if title == "登录注册":
            return True, err or "auth_wall_cardlist_title"
    return False, err


def probe_profile(
    opener: urllib.request.OpenerDirector,
    uid: str,
    timeout: float,
) -> PageResult:
    url = f"{PROFILE_URL}?{urllib.parse.urlencode({'uid': uid})}"
    status, latency, data, err, final_url = fetch_json(opener, url, timeout)
    auth_wall, err = detect_auth_wall(data, err, final_url)
    screen_name = None
    ok = False
    if isinstance(data, dict) and isinstance(data.get("userInfo"), dict):
        screen_name = data["userInfo"].get("screen_name")
        ok = status == 200 and screen_name is not None and not auth_wall
    elif isinstance(data, dict) and data.get("errno"):
        err = err or f"errno:{data.get('errno')}:{data.get('errmsg')}"
    return PageResult(
        uid=uid,
        endpoint="api.weibo.cn/2/profile",
        page=0,
        ok=ok,
        http_status=status,
        latency_ms=round(latency, 1),
        screen_name=screen_name,
        error=err,
        auth_wall=auth_wall,
        rate_limited=bool(err and "rate_or_forbidden" in err),
        final_url=final_url,
        raw_ok=data.get("ok") if isinstance(data, dict) else None,
    )


def probe_page(
    opener: urllib.request.OpenerDirector,
    uid: str,
    page: int,
    timeout: float,
) -> PageResult:
    query = {
        "type": "uid",
        "value": uid,
        "containerid": f"{TIMELINE_CONTAINER_PREFIX}{uid}",
    }
    if page > 1:
        query["page"] = str(page)
    url = f"{TIMELINE_URL}?{urllib.parse.urlencode(query)}"
    status, latency, data, err, final_url = fetch_json(opener, url, timeout)
    auth_wall, err = detect_auth_wall(data, err, final_url)
    rate_limited = bool(err and ("rate_or_forbidden" in err or status in (418, 429, 432)))

    posts = 0
    total_hint = None
    raw_ok = None
    if isinstance(data, dict):
        raw_ok = data.get("ok")
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        if isinstance(payload, dict):
            posts, total_hint = _count_mblogs(payload)
        if err is None and not auth_wall and raw_ok not in (1, "1", None):
            if raw_ok not in (1, "1") and data.get("msg"):
                err = f"api_msg:{data.get('msg')}"

    # 必须真正读到博文，或 ok==1 且无登录墙，才算时间线成功
    ok = (
        err is None
        and status == 200
        and not auth_wall
        and (posts > 0 or raw_ok in (1, "1"))
    )
    return PageResult(
        uid=uid,
        endpoint="m.weibo.cn/api/container/getIndex",
        page=page,
        ok=ok,
        http_status=status,
        latency_ms=round(latency, 1),
        posts=posts,
        total_hint=total_hint,
        error=err,
        auth_wall=auth_wall,
        rate_limited=rate_limited,
        final_url=final_url,
        raw_ok=raw_ok,
    )


def probe_pc_profile(
    opener: urllib.request.OpenerDirector,
    uid: str,
    timeout: float,
) -> PageResult:
    url = f"{PC_PROFILE_URL}?{urllib.parse.urlencode({'uid': uid})}"
    status, latency, data, err, final_url = fetch_json(opener, url, timeout)
    auth_wall, err = detect_auth_wall(data, err, final_url)
    screen_name = None
    total_hint = None
    ok = False
    if isinstance(data, dict):
        user = (data.get("data") or {}).get("user") if isinstance(data.get("data"), dict) else None
        if isinstance(user, dict):
            screen_name = user.get("screen_name")
            if isinstance(user.get("statuses_count"), int):
                total_hint = user["statuses_count"]
            ok = data.get("ok") == 1 and user.get("id") is not None and not auth_wall
        elif data.get("ok") == -100 or data.get("ok") == "-100":
            auth_wall = True
            err = err or "auth_wall_ok_-100"
    return PageResult(
        uid=uid,
        endpoint="weibo.com/ajax/profile/info",
        page=0,
        ok=ok,
        http_status=status,
        latency_ms=round(latency, 1),
        screen_name=screen_name,
        total_hint=total_hint,
        error=err,
        auth_wall=auth_wall,
        rate_limited=bool(err and "rate_or_forbidden" in (err or "")),
        final_url=final_url,
        raw_ok=data.get("ok") if isinstance(data, dict) else None,
    )


def probe_pc_timeline(
    opener: urllib.request.OpenerDirector,
    uid: str,
    page: int,
    timeout: float,
) -> PageResult:
    query = {"uid": uid, "page": str(page), "feature": "0"}
    url = f"{PC_TIMELINE_URL}?{urllib.parse.urlencode(query)}"
    status, latency, data, err, final_url = fetch_json(opener, url, timeout)
    auth_wall, err = detect_auth_wall(data, err, final_url)
    posts = 0
    total_hint = None
    raw_ok = None
    if isinstance(data, dict):
        raw_ok = data.get("ok")
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        lst = payload.get("list") if isinstance(payload, dict) else None
        if isinstance(lst, list):
            posts = len(lst)
        if isinstance(payload, dict) and isinstance(payload.get("total"), int):
            total_hint = payload["total"]
        if raw_ok in (-100, "-100"):
            auth_wall = True
            err = err or "auth_wall_ok_-100"
    ok = status == 200 and not auth_wall and err is None and raw_ok == 1 and posts >= 0
    # 空博文用户也算接口成功（ok=1 且 list=[]）
    if ok and posts == 0 and raw_ok != 1:
        ok = False
    if status == 200 and raw_ok == 1 and not auth_wall and err is None:
        ok = True
    return PageResult(
        uid=uid,
        endpoint="weibo.com/ajax/statuses/mymblog",
        page=page,
        ok=ok,
        http_status=status,
        latency_ms=round(latency, 1),
        posts=posts,
        total_hint=total_hint,
        error=err,
        auth_wall=auth_wall,
        rate_limited=bool(err and "rate_or_forbidden" in (err or "")),
        final_url=final_url,
        raw_ok=raw_ok,
    )


def probe_guest_cardlist(
    opener: urllib.request.OpenerDirector,
    uid: str,
    timeout: float,
) -> PageResult:
    query = {"containerid": f"{TIMELINE_CONTAINER_PREFIX}{uid}"}
    url = f"{GUEST_CARDLIST_URL}?{urllib.parse.urlencode(query)}"
    status, latency, data, err, final_url = fetch_json(opener, url, timeout)
    auth_wall, err = detect_auth_wall(data, err, final_url)
    posts = 0
    total_hint = None
    if isinstance(data, dict):
        posts, total_hint = _count_mblogs(data)
    ok = status == 200 and not auth_wall and posts > 0 and err is None
    if not ok and err is None and auth_wall:
        err = err or "auth_wall"
    return PageResult(
        uid=uid,
        endpoint="api.weibo.cn/2/guest/cardlist",
        page=1,
        ok=ok,
        http_status=status,
        latency_ms=round(latency, 1),
        posts=posts,
        total_hint=total_hint,
        error=err,
        auth_wall=auth_wall,
        rate_limited=bool(status in (418, 429, 432)),
        final_url=final_url,
        raw_ok=data.get("ok") if isinstance(data, dict) else None,
    )


def estimate(
    target_users: int,
    posts_per_user: int,
    page_size: int,
    delay_s: float,
    observed_success_rate: float,
    median_latency_ms: float | None,
) -> dict[str, Any]:
    pages_per_user = max(1, (posts_per_user + page_size - 1) // page_size)
    total_requests = target_users * pages_per_user
    # 成功路径按 delay 串行估算；另给出「即便无限流」的理论下界
    serial_hours = (total_requests * delay_s) / 3600.0
    if median_latency_ms is not None:
        serial_hours_lat = (total_requests * (median_latency_ms / 1000.0 + delay_s)) / 3600.0
    else:
        serial_hours_lat = serial_hours
    effective = max(observed_success_rate, 0.0)
    return {
        "target_users": target_users,
        "assume_posts_per_user": posts_per_user,
        "assume_page_size": page_size,
        "pages_per_user": pages_per_user,
        "total_requests": total_requests,
        "observed_page_success_rate": round(effective, 4),
        "estimated_serial_hours_at_delay": round(serial_hours_lat, 2),
        "note": (
            "以上为理想串行估算。未计入登录墙、验证码、封禁、IP 风控；"
            "实测成功率接近 0 时，全量方案在非官方通道下不可行。"
        ),
    }


def print_human_report(report: dict[str, Any]) -> None:
    print("\n========== 可行性探测报告 ==========")
    print(f"时间: {report['started_at']}")
    print(f"UID 数: {report['uid_count']}  页/UID: {report['max_pages']}  间隔: {report['delay_s']}s")
    print(f"带 Cookie: {report['has_cookie']}")
    print(
        f"资料接口成功: {report['profile_ok_count']}/{report['uid_count']}  "
        f"时间线成功页: {report['pages_ok']}/{report['pages_tried']}  "
        f"成功率: {report['page_success_rate']:.1%}"
    )
    print(f"拉到博文条数(抽样): {report['posts_fetched']}")
    print(f"登录墙命中: {report['auth_wall_hits']}  限流/拒绝: {report['rate_limit_hits']}")
    if report["latencies_ms"]:
        print(
            "延迟 ms: "
            f"p50={statistics.median(report['latencies_ms']):.0f} "
            f"avg={statistics.mean(report['latencies_ms']):.0f} "
            f"max={max(report['latencies_ms']):.0f}"
        )
    est = report["estimate_90k"]
    print("\n--- 对目标规模的外推 ---")
    print(
        f"目标用户 {est['target_users']} × 人均 {est['assume_posts_per_user']} 条 "
        f"≈ {est['total_requests']} 次分页请求"
    )
    print(f"按当前间隔串行约 {est['estimated_serial_hours_at_delay']} 小时（理想情况）")
    print(f"结论: {report['verdict']}")
    print("====================================\n")


def verdict_from(report: dict[str, Any]) -> str:
    if report["pages_tried"] == 0:
        return "未发起请求（dry-run 或无 UID）"
    profile_ok = report.get("profile_ok_count", 0)
    pc_posts = report.get("pc_posts_fetched", 0)
    mp_posts = report.get("mp_posts_fetched", 0)
    if mp_posts > 0:
        return (
            "有小程序 gsid 时，api.weibo.cn/2/cardlist（containerid=107603{uid}）"
            "可按 UID 抽样读到博文。"
            "你贴的 unread_count 只用于校验登录态，不是博文列表接口。"
            "小样本可读 ≠ 9 万全量可行（限流/风控/账号封禁/合规仍卡死全量）。"
        )
    if pc_posts > 0:
        return (
            "有登录 Cookie 时，weibo.com/ajax 可按 UID 抽样读到博文；"
            "m.weibo.cn 仍常登录墙。"
            "这只证明小样本可读，不证明 9 万用户全量可行（限流/风控/合规仍卡死全量）。"
        )
    if report["auth_wall_hits"] > 0 and report["posts_fetched"] == 0:
        extra = (
            f"资料接口可通 ({profile_ok}/{report['uid_count']})，但博文时间线全部登录墙。"
            if profile_ok
            else "资料与时间线均不可稳定读取。"
        )
        return (
            f"按 UID 抓取博文：不可行。{extra}"
            "未授权通道无法支撑 9 万用户全量博文采集。"
        )
    if report["rate_limit_hits"] > 0 and report["page_success_rate"] < 0.3:
        return "不可行（强风控/限流）。即使单个 UID 偶发成功，9 万用户全量也无法靠该通道完成。"
    if report["page_success_rate"] >= 0.8 and report["posts_fetched"] > 0:
        return (
            "抽样通道可读到博文，但全量仍高风险：请用官方开放平台 + 授权/配额；"
            "本工具仅证明「单 UID 可读」，不证明「9 万全量可行」。"
        )
    if report["posts_fetched"] == 0:
        return "不可行（0 条博文）。当前仅可能读到资料，读不到时间线。"
    return "部分可用但不稳定，不建议作为 9 万用户全量采集方案。"


def main() -> int:
    args = parse_args()
    uids = load_uids(args)
    if not uids:
        # 内置几个公开大 V / 官方号 UID，便于开箱探测
        uids = ["2803301701", "1669879400", "1934184475"][: args.max_uids]
        print("[info] 未提供 UID，使用内置样例 UID 做探测", file=sys.stderr)

    cookie = resolve_cookie(args)
    cookie_meta = cookie_diagnostics(cookie) if cookie else {"provided": False}
    gsid = resolve_gsid(args)

    if args.dry_run:
        est = estimate(
            args.target_users,
            args.assume_posts_per_user,
            args.page_size,
            args.delay,
            0.0,
            None,
        )
        print(
            json.dumps(
                {
                    "uids": uids,
                    "cookie": {"provided": bool(cookie), **(cookie_meta if cookie else {})},
                    "gsid": {"provided": bool(gsid), "length": len(gsid) if gsid else 0},
                    "estimate": est,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if cookie:
        print(
            "[info] 已加载 Cookie: "
            f"keys≈{cookie_meta.get('key_count')} "
            f"SUB={cookie_meta.get('has_sub')} "
            f"SUBP={cookie_meta.get('has_subp')} "
            f"looks_logged_in={cookie_meta.get('looks_logged_in')}",
            file=sys.stderr,
        )
        if not cookie_meta.get("looks_logged_in"):
            print(
                "[warn] Cookie 里未见 SUB/SUBP，m.weibo.cn 时间线仍可能 ok=-100",
                file=sys.stderr,
            )
    else:
        print(
            "[info] 未提供 Cookie。可用 --cookie-file / WEIBO_COOKIE / --cookie 注入登录态后再测",
            file=sys.stderr,
        )
    if gsid:
        print(f"[info] 已加载小程序 gsid（len={len(gsid)}，值不打印）", file=sys.stderr)
    else:
        print(
            "[info] 未提供 gsid。小程序通道可用 --gsid-file / WEIBO_GSID",
            file=sys.stderr,
        )

    opener = build_opener(cookie)
    pc_opener = build_opener(cookie, desktop=True) if cookie else None
    mp_opener = None
    mp_common: dict[str, str] | None = None
    if gsid:
        mp_opener, mp_common = build_mp_opener(gsid, args.mp_s, args.mp_from, args.mp_wm)
    all_results: list[PageResult] = []
    timeline_results: list[PageResult] = []
    summaries: dict[str, UidSummary] = {u: UidSummary(uid=u) for u in uids}

    def record(result: PageResult, *, timeline: bool, label: str) -> None:
        all_results.append(result)
        s = summaries[result.uid]
        s.latencies_ms.append(result.latency_ms)
        if timeline:
            timeline_results.append(result)
            s.pages_tried += 1
            if result.ok:
                s.pages_ok += 1
                s.posts_fetched += result.posts
                if result.total_hint is not None:
                    s.total_hint = result.total_hint
        elif result.ok:
            s.profile_ok = True
            if result.screen_name:
                s.screen_name = result.screen_name
            if result.total_hint is not None and s.total_hint is None:
                s.total_hint = result.total_hint
        if result.auth_wall:
            s.auth_wall = True
        if result.rate_limited:
            s.rate_limited = True
        if result.error:
            s.errors.append(f"{label}:{result.error}")
        flag = "OK" if result.ok else "FAIL"
        extra = ""
        if result.screen_name:
            extra += f" name={result.screen_name}"
        if result.ok:
            extra += f" posts={result.posts}"
            if result.total_hint is not None:
                extra += f" statuses_count≈{result.total_hint}"
        elif result.error:
            extra += f" err={result.error}"
            if result.raw_ok is not None:
                extra += f" raw_ok={result.raw_ok}"
        page_bit = f" page={result.page}" if result.page else ""
        print(
            f"[{flag}] uid={result.uid} {label}{page_bit} "
            f"status={result.http_status} {result.latency_ms:.0f}ms{extra}"
        )

    for i, uid in enumerate(uids):
        # -1) 微信小程序 gsid 通道
        if mp_opener is not None and mp_common is not None:
            mp_user = probe_mp_users_show(mp_opener, mp_common, uid, args.timeout)
            record(mp_user, timeline=False, label="mp/users.show")
            time.sleep(args.delay)
            for page in range(1, args.max_pages + 1):
                mp_cl = probe_mp_cardlist(mp_opener, mp_common, uid, page, args.timeout)
                record(mp_cl, timeline=True, label="mp/cardlist")
                time.sleep(args.delay)
                if mp_cl.auth_wall or mp_cl.rate_limited:
                    break
            mp_ps = probe_mp_profile_statuses(mp_opener, mp_common, uid, 1, args.timeout)
            record(mp_ps, timeline=True, label="mp/profile.statuses")
            time.sleep(args.delay)

        # 0) PC Cookie 通道（weibo.com/ajax）——与你从网页复制的 Cookie 匹配
        if pc_opener is not None:
            pc_profile = probe_pc_profile(pc_opener, uid, args.timeout)
            record(pc_profile, timeline=False, label="pc/profile")
            time.sleep(args.delay)
            for page in range(1, args.max_pages + 1):
                pc_tl = probe_pc_timeline(pc_opener, uid, page, args.timeout)
                record(pc_tl, timeline=True, label="pc/mymblog")
                time.sleep(args.delay)
                if pc_tl.auth_wall or pc_tl.rate_limited:
                    break

        # 1) 资料接口（常可匿名；有 Cookie 时也可能校验失败，属对照）
        profile = probe_profile(opener, uid, args.timeout)
        record(profile, timeline=False, label="mobile/profile")
        time.sleep(args.delay)

        # 2) m.weibo.cn 时间线分页
        for page in range(1, args.max_pages + 1):
            result = probe_page(opener, uid, page, args.timeout)
            record(result, timeline=True, label="m.weibo/timeline")
            time.sleep(args.delay)
            if result.auth_wall or result.rate_limited:
                break

        # 3) guest/cardlist 对照
        guest = probe_guest_cardlist(opener, uid, args.timeout)
        record(guest, timeline=True, label="guest/cardlist")
        if i < len(uids) - 1:
            time.sleep(args.delay)

    latencies = [r.latency_ms for r in all_results]
    pages_tried = len(timeline_results)
    pages_ok = sum(1 for r in timeline_results if r.ok)
    success_rate = (pages_ok / pages_tried) if pages_tried else 0.0
    posts_fetched = sum(r.posts for r in timeline_results if r.ok)
    pc_posts = sum(
        r.posts
        for r in timeline_results
        if r.ok and r.endpoint.endswith("mymblog")
    )
    mp_posts = sum(
        r.posts
        for r in timeline_results
        if r.ok and ("cardlist" in r.endpoint or "profile/statuses" in r.endpoint)
    )
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "uid_count": len(uids),
        "uids": uids,
        "max_pages": args.max_pages,
        "delay_s": args.delay,
        "has_cookie": bool(cookie),
        "has_gsid": bool(gsid),
        "cookie_meta": cookie_meta if cookie else {"provided": False},
        "profile_ok_count": sum(1 for u in uids if summaries[u].profile_ok),
        "pages_tried": pages_tried,
        "pages_ok": pages_ok,
        "page_success_rate": success_rate,
        "posts_fetched": posts_fetched,
        "pc_posts_fetched": pc_posts,
        "mp_posts_fetched": mp_posts,
        "auth_wall_hits": sum(1 for r in all_results if r.auth_wall),
        "rate_limit_hits": sum(1 for r in all_results if r.rate_limited),
        "latencies_ms": latencies,
        "per_uid": {u: asdict(summaries[u]) for u in uids},
        "results": [
            {**asdict(r), "final_url": redact_url(r.final_url)} for r in all_results
        ],
        "estimate_90k": estimate(
            args.target_users,
            args.assume_posts_per_user,
            args.page_size,
            args.delay,
            success_rate,
            statistics.median(latencies) if latencies else None,
        ),
        "channels": [
            "api.weibo.cn/2/users/show",
            "api.weibo.cn/2/cardlist",
            "api.weibo.cn/2/profile/statuses",
            "api.weibo.cn/2/remind/unread_count",
            "weibo.com/ajax/profile/info",
            "weibo.com/ajax/statuses/mymblog",
            "api.weibo.cn/2/profile",
            "m.weibo.cn/api/container/getIndex",
            "api.weibo.cn/2/guest/cardlist",
        ],
        "disclaimer": (
            "本程序仅用于小样本可行性验证；请遵守法律法规与微博服务条款。"
            "全量/商业分析请使用微博开放平台正式授权接口。"
        ),
    }
    report["verdict"] = verdict_from(report)

    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print_human_report(report)
    print(f"详细 JSON 已写入: {out_path}")
    return 0 if posts_fetched > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
