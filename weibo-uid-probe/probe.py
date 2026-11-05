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


DEFAULT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
    "Mobile/15E148 Safari/604.1"
)
TIMELINE_URL = "https://m.weibo.cn/api/container/getIndex"
PROFILE_URL = "https://api.weibo.cn/2/profile"
GUEST_CARDLIST_URL = "https://api.weibo.cn/2/guest/cardlist"
TIMELINE_CONTAINER_PREFIX = "107603"


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
        help="可选 Cookie（也可用环境变量 WEIBO_COOKIE）。未登录通常会撞登录墙。",
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


def build_opener(cookie: str) -> urllib.request.OpenerDirector:
    handlers = [urllib.request.HTTPRedirectHandler()]
    opener = urllib.request.build_opener(*handlers)
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://m.weibo.cn/",
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookie:
        headers["Cookie"] = cookie
    opener.addheaders = list(headers.items())
    return opener


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
            text = body.decode("utf-8", errors="replace")
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
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        err = f"http_{e.code}"
        if e.code in (403, 418, 429, 432):
            err = f"rate_or_forbidden_{e.code}"
        if "passport" in body.lower() or "login" in body.lower():
            err = "auth_wall_http"
        return e.code, latency, None, err, e.geturl() if hasattr(e, "geturl") else None
    except Exception as e:  # noqa: BLE001 - probe should never crash mid-run
        latency = (time.perf_counter() - started) * 1000
        return None, latency, None, f"{type(e).__name__}:{e}", None


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

    if args.dry_run:
        est = estimate(
            args.target_users,
            args.assume_posts_per_user,
            args.page_size,
            args.delay,
            0.0,
            None,
        )
        print(json.dumps({"uids": uids, "estimate": est}, ensure_ascii=False, indent=2))
        return 0

    opener = build_opener(args.cookie)
    all_results: list[PageResult] = []
    timeline_results: list[PageResult] = []
    summaries: dict[str, UidSummary] = {u: UidSummary(uid=u) for u in uids}

    for i, uid in enumerate(uids):
        # 1) 资料接口（常可匿名）
        profile = probe_profile(opener, uid, args.timeout)
        all_results.append(profile)
        s = summaries[uid]
        s.latencies_ms.append(profile.latency_ms)
        if profile.ok:
            s.profile_ok = True
            s.screen_name = profile.screen_name
        if profile.auth_wall:
            s.auth_wall = True
        if profile.error:
            s.errors.append(f"profile:{profile.error}")
        flag = "OK" if profile.ok else "FAIL"
        name = f" name={profile.screen_name}" if profile.screen_name else ""
        err = f" err={profile.error}" if profile.error else ""
        print(
            f"[{flag}] uid={uid} profile status={profile.http_status} "
            f"{profile.latency_ms:.0f}ms{name}{err}"
        )
        time.sleep(args.delay)

        # 2) m.weibo.cn 时间线分页
        for page in range(1, args.max_pages + 1):
            result = probe_page(opener, uid, page, args.timeout)
            all_results.append(result)
            timeline_results.append(result)
            s.pages_tried += 1
            s.latencies_ms.append(result.latency_ms)
            if result.ok:
                s.pages_ok += 1
                s.posts_fetched += result.posts
                if result.total_hint is not None:
                    s.total_hint = result.total_hint
            if result.auth_wall:
                s.auth_wall = True
            if result.rate_limited:
                s.rate_limited = True
            if result.error:
                s.errors.append(f"timeline_p{page}:{result.error}")
            flag = "OK" if result.ok else "FAIL"
            extra = (
                f" posts={result.posts}"
                if result.ok
                else f" err={result.error} raw_ok={result.raw_ok}"
            )
            print(
                f"[{flag}] uid={uid} timeline page={page} status={result.http_status} "
                f"{result.latency_ms:.0f}ms{extra}"
            )
            time.sleep(args.delay)
            if result.auth_wall or result.rate_limited:
                break

        # 3) guest/cardlist 对照
        guest = probe_guest_cardlist(opener, uid, args.timeout)
        all_results.append(guest)
        timeline_results.append(guest)
        s.pages_tried += 1
        s.latencies_ms.append(guest.latency_ms)
        if guest.ok:
            s.pages_ok += 1
            s.posts_fetched += guest.posts
        if guest.auth_wall:
            s.auth_wall = True
        if guest.error:
            s.errors.append(f"guest:{guest.error}")
        flag = "OK" if guest.ok else "FAIL"
        extra = f" posts={guest.posts}" if guest.ok else f" err={guest.error}"
        print(
            f"[{flag}] uid={uid} guest/cardlist status={guest.http_status} "
            f"{guest.latency_ms:.0f}ms{extra}"
        )
        if i < len(uids) - 1:
            time.sleep(args.delay)

    latencies = [r.latency_ms for r in all_results]
    pages_tried = len(timeline_results)
    pages_ok = sum(1 for r in timeline_results if r.ok)
    success_rate = (pages_ok / pages_tried) if pages_tried else 0.0
    posts_fetched = sum(r.posts for r in timeline_results if r.ok)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "uid_count": len(uids),
        "uids": uids,
        "max_pages": args.max_pages,
        "delay_s": args.delay,
        "has_cookie": bool(args.cookie),
        "profile_ok_count": sum(1 for u in uids if summaries[u].profile_ok),
        "pages_tried": pages_tried,
        "pages_ok": pages_ok,
        "page_success_rate": success_rate,
        "posts_fetched": posts_fetched,
        "auth_wall_hits": sum(1 for r in all_results if r.auth_wall),
        "rate_limit_hits": sum(1 for r in all_results if r.rate_limited),
        "latencies_ms": latencies,
        "per_uid": {u: asdict(summaries[u]) for u in uids},
        "results": [asdict(r) for r in all_results],
        "estimate_90k": estimate(
            args.target_users,
            args.assume_posts_per_user,
            args.page_size,
            args.delay,
            success_rate,
            statistics.median(latencies) if latencies else None,
        ),
        "channels": [
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
