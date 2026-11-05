# 微博 UID 博文拉取可行性探测

用少量 UID 实测：按 uid 能否读到时间线、是否登录墙/限流，并外推到约 9 万用户全量的请求规模。

**这不是全量爬虫。** 默认最多 20 个 UID、每 UID 10 页，且强制请求间隔。

## 运行

```bash
# 用内置样例 UID
python3 weibo-uid-probe/probe.py

# 指定 UID
python3 weibo-uid-probe/probe.py --uids 2803301701 1669879400

# 从文件读 UID
python3 weibo-uid-probe/probe.py --uids-file weibo-uid-probe/uids.sample.txt --max-uids 3 --max-pages 2

# 若你有自己的登录 Cookie（仅测「有登录态」差异；勿提交 Cookie）
export WEIBO_COOKIE='SUB=...; SUBP=...'
python3 weibo-uid-probe/probe.py --uids-file weibo-uid-probe/uids.sample.txt

# 只看估算、不发请求
python3 weibo-uid-probe/probe.py --dry-run --uids-file weibo-uid-probe/uids.sample.txt
```

报告默认写到当前目录 `probe_report.json`。

## 探测的接口

| 接口 | 作用 |
|------|------|
| `GET https://api.weibo.cn/2/profile?uid=` | 用户资料（常可匿名） |
| `GET https://m.weibo.cn/api/container/getIndex?type=uid&value=&containerid=107603{uid}` | 博文时间线 |
| `GET https://api.weibo.cn/2/guest/cardlist?containerid=107603{uid}` | 访客时间线对照 |

## 如何解读

| 现象 | 含义 |
|------|------|
| 资料 OK、时间线 `ok=-100` / 登录卡 | 能解析 UID，但博文需登录，抓取不可行 |
| `auth_wall` / 跳转 passport | 无有效登录态，公开接口不可批量用 |
| `403/418/429` | 风控或限流，全量更不可行 |
| 抽样成功但成功率不稳 | 单 UID 可读 ≠ 9 万全量可行 |
| 外推小时数 | 仅理想串行下界，未含封禁与验证码 |

## 合规

仅做小样本可行性验证。批量采集用户博文需微博开放平台授权与合规评估；请勿用于未授权大规模抓取。
