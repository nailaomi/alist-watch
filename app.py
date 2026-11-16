#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alist-watch —— NAS 网盘凭证巡检（容器版）
纯标准库实现（无 pip 依赖）：Web UI + 定时调度 + 历史记录 + Bark 推送

设计要点：
  1. 只读打开 alist 的 data.db，不需要 docker.sock（不需要容器控制权）
  2. 挂载路径自动发现（从 x_storages 读 driver + mount_path），不再硬编码
  3. 配置全部在 Web UI 里改，不用 SSH
  4. 历史落 sqlite，可回溯
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import icon

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
CONF_PATH = os.path.join(DATA_DIR, "config.json")
HIST_PATH = os.path.join(DATA_DIR, "history.db")
RUN_PATH = os.path.join(DATA_DIR, "runstate.json")
PORT = int(os.environ.get("PORT", "8088"))

DEFAULT_CONF = {
    "alist_base": "http://host.docker.internal:5678",
    "gbox_base": "",
    "gbox_user": "",
    "gbox_pass": "",
    "alist_db": "/alist/data/data.db",
    "bark": {"url": "", "key": "", "group": "NAS巡检", "sound": "minuet",
             "title_ok": "✅ 网盘正常 {date} {time}",
             "title_bad": "⛔ {bad} 需要处理",
             "level_ok": "active",
             "level_bad": "timeSensitive", "with_hint": True},
    # 每天在这些时刻巡检并推送（早/中/晚）。某个格子留空 = 那一次不发。
    "schedule": {"times": ["08:30", "12:30", "19:30"]},
    # 实测鉴权（fs/list 真打上游）的频率控制——防封控
    # 8 小时 ≈ 每天早、晚各实测一次，中午那轮复用结论
    "probe": {"enabled": True, "min_interval_h": 8, "gap_s": 1.2},
    # 外部心跳：每轮巡检结束 GET 一次；外部收不到 = NAS/容器/程序本身挂了
    # 填 healthchecks.io 的 ping 地址或 Uptime Kuma 的 Push Monitor 地址
    "heartbeat": {"url": ""},
    "notify_when_ok": True,
    "notify": {"policy": "always", "fail_threshold": 1, "repeat_bad": True},
    "public_base": "",
    "ui_password": "changeme",
    "disabled": [],
}


def log(*a):
    print("[aw %s]" % datetime.now(CST).strftime("%m-%d %H:%M:%S"), *a, flush=True)

# 驱动 → (分组名, 类型)
DRIVER_MAP = {
    "115 Cloud": ("115", "personal"), "115 CloudShare": ("115", "share"),
    "115 Share": ("115", "share"),
    "Quark": ("夸克", "personal"), "QuarkShare": ("夸克", "share"),
    "GuangYaPan": ("光鸭", "personal"), "GuangYaShare": ("光鸭", "share"),
    "AliyundriveOpen": ("阿里", "personal"), "AliyundriveShare2Open": ("阿里", "share"),
    "189CloudPC": ("天翼", "personal"), "189Cloud": ("天翼", "personal"),
    "PikPak": ("PikPak", "personal"), "PikPakShare": ("PikPak", "share"),
    "Thunder": ("迅雷", "personal"), "ThunderShare": ("迅雷", "share"),
    "BaiduNetdisk": ("百度", "personal"),
}

# 分组 → 图标标识
#   emoji : 推送正文里的彩色小图标（纯文本兜底）
#   slug  : 容器内手绘品牌图标 /logo/<slug>.png，网页和 Bark 共用
#   en    : 状态图上的 ASCII 名称（点阵字库没有中文）
PROVIDER_META = {
    "115":     {"emoji": "📦", "color": "#2e6be6", "short": "115",
                "slug": "115", "en": "115"},
    "夸克":     {"emoji": "⚛️", "color": "#5b57f0", "short": "夸",
                "slug": "quark", "en": "QUARK"},
    "光鸭":     {"emoji": "🦆", "color": "#f0a020", "short": "鸭",
                "slug": "guangya", "en": "GUANGYA"},
    "阿里":     {"emoji": "☁️", "color": "#3c7df7", "short": "阿",
                "slug": "aliyun", "en": "ALIYUN"},
    "天翼":     {"emoji": "📡", "color": "#1e7bef", "short": "天",
                "slug": "tianyi", "en": "TIANYI"},
    "迅雷":     {"emoji": "⚡", "color": "#2ba8f2", "short": "迅",
                "slug": "xunlei", "en": "XUNLEI"},
    "PikPak":  {"emoji": "🗂️", "color": "#00b39a", "short": "P",
                "slug": "pikpak", "en": "PIKPAK"},
    "百度":     {"emoji": "🐟", "color": "#2a32e0", "short": "度",
                "slug": "baidu", "en": "BAIDU"},
}


def pmeta(group):
    return PROVIDER_META.get(group) or {"emoji": "☁️", "color": "#6b7280",
                                        "short": (group or "?")[:1],
                                        "slug": "115", "en": (group or "?")[:6].upper()}


def pslug(group):
    return pmeta(group)["slug"]


def pen(group):
    return pmeta(group)["en"]


_state_lock = threading.Lock()
STATE = {"last": None, "running": False, "last_error": "", "phase": ""}


def set_phase(txt):
    with _state_lock:
        STATE["phase"] = txt


# ---------------------------------------------------------------- 配置
def load_conf():
    c = json.loads(json.dumps(DEFAULT_CONF))
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            d = json.load(f)
        for k, v in d.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
    except Exception:
        pass
    return c


def save_conf(c):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONF_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONF_PATH)
    try:
        os.chmod(CONF_PATH, 0o600)
    except Exception:
        pass


def public_conf(conf):
    """给前端看的配置：密码类字段一律不回传"""
    c = json.loads(json.dumps(conf))
    c["gbox_pass"] = ""
    c["ui_password"] = ""
    c["gbox_pass_set"] = bool(conf.get("gbox_pass"))
    c["ui_password_set"] = bool(conf.get("ui_password"))
    return c


def load_runstate():
    try:
        with open(RUN_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_runstate(d):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = RUN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, RUN_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------- HTTP
def http(method, url, body=None, headers=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------- 历史
def hist_init():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(HIST_PATH)
    con.execute("""create table if not exists checks(
        id integer primary key autoincrement,
        ts text, verdict text, summary text, detail text)""")
    con.commit()
    con.close()


def hist_add(ts, verdict, summary, detail):
    """返回这条记录的 id（状态图按 id 回查 detail 现画）"""
    try:
        con = sqlite3.connect(HIST_PATH, timeout=10)
        cur = con.execute("insert into checks(ts,verdict,summary,detail) values(?,?,?,?)",
                          (ts, verdict, summary, json.dumps(detail, ensure_ascii=False)))
        rid = cur.lastrowid
        con.execute("delete from checks where id not in "
                    "(select id from checks order by id desc limit 500)")
        con.commit()
        con.close()
        return rid
    except Exception:
        return None


def hist_detail(rid):
    """按 history id 取回当轮 detail（状态图现画用）"""
    try:
        con = sqlite3.connect(HIST_PATH, timeout=10)
        row = con.execute("select detail from checks where id=?", (int(rid),)).fetchone()
        con.close()
        return (json.loads(row[0]) or {}) if row else None
    except Exception:
        return None


def hist_list(limit=30):
    try:
        con = sqlite3.connect(HIST_PATH, timeout=10)
        rows = con.execute("select id,ts,verdict,summary from checks "
                           "order by id desc limit ?", (limit,)).fetchall()
        con.close()
        return [{"id": r[0], "ts": r[1], "verdict": r[2], "summary": r[3]} for r in rows]
    except Exception:
        return []


def hist_stats(rounds=21, days=30):
    """聚合历史库：可用率、本周异常、实测通过率、连续在线天数、
    每个网盘近 N 轮的状态条（画热力条用）、持续失效起点。
    rows 按 id 倒序（最新在前）。detail JSON 只解析最近 ~40 条，别的不碰。"""
    empty = {"uptime30": None, "total30": 0, "week_bad": 0, "week_total": 0, "probe_ok": 0,
             "probe_tot": 0, "streak": 0, "strips": {}, "recent": [],
             "bad_since": {}, "flaky": {}}
    try:
        con = sqlite3.connect(HIST_PATH, timeout=10)
        rows = con.execute("select ts,verdict,detail from checks "
                           "order by id desc limit 1200").fetchall()
        con.close()
    except Exception:
        return empty
    if not rows:
        return empty

    now = datetime.now(CST)
    cut30 = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cut7 = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    in30 = [(ts, v) for ts, v, _ in rows if ts >= cut30]
    total30 = len(in30)

    week_bad = len([1 for ts, v in in30 if ts >= cut7 and v != "OK"])
    week_total = len([1 for ts, v in in30 if ts >= cut7])

    # 可用率按「网盘 × 轮次」加权：一个网盘挂了不该把全局打成 0%
    prov_ok = prov_tot = 0
    for ts, _v, det in rows:
        if ts < cut30:
            continue
        try:
            ps = (json.loads(det) or {}).get("providers", [])
        except Exception:
            continue
        for p in ps:
            if p.get("status") == "off":
                continue
            prov_tot += 1
            if p.get("status") == "ok":
                prov_ok += 1

    # 连续全绿天数：从今天往回数，碰到有 FAIL 的天就停
    bad_days = {ts[:10] for ts, v, _ in rows if v == "FAIL"}
    streak, d = 0, now.date()
    while streak <= 3660:
        if d.strftime("%Y-%m-%d") in bad_days:
            break
        streak += 1
        d -= timedelta(days=1)

    # 最近 N 轮：热力条 + 持续失效起点。
    # 某轮没出现的网盘补 None（画成灰格），保证每条的格子数一致、时间对齐。
    recent = list(reversed(rows[:rounds]))            # 旧 → 新
    providers_now = []
    try:
        providers_now = [(p.get("group"), p.get("status"))
                         for p in (json.loads(rows[0][2]) or {}).get("providers", [])]
    except Exception:
        pass
    groups, seen, round_ps = [], set(), []
    for _ts, _v, det in recent:
        try:
            ps = (json.loads(det) or {}).get("providers", [])
        except Exception:
            ps = []
        round_ps.append(ps)
        for p in ps:
            g = p.get("group") or "?"
            if g not in seen:
                seen.add(g)
                groups.append(g)
    strips = {g: [] for g in groups}
    for ps in round_ps:
        cur = {}
        for p in ps:
            cur[p.get("group") or "?"] = p.get("status") or "ok"
        for g in groups:
            strips[g].append(cur.get(g))              # None = 该轮无此网盘

    bad_since = {}
    for g, st in providers_now:
        if st != "bad":
            continue
        run = strips.get(g) or []
        n = 0
        for s in reversed(run):
            if s == "bad":
                n += 1
            else:
                break
        idx = len(run) - n
        if 0 <= idx < len(recent):
            bad_since[g] = recent[idx][0][5:10]       # MM-DD

    # 7 天实测通过率（只算真打上游的轮次，缓存轮不计）
    probe_ok = probe_tot = 0
    for ts, _v, det in rows[:60]:
        if ts < cut7:
            continue
        try:
            ps = (json.loads(det) or {}).get("providers", [])
        except Exception:
            continue
        for p in ps:
            if p.get("probe") in ("ok", "bad", "unknown"):
                probe_tot += 1
                if p.get("probe") == "ok":
                    probe_ok += 1

    # 近 7 天抖动：某盘在 ok 与 bad 之间反复横跳，多半是凭证处在边缘。
    # 这类盘现在还没「坏」，但离坏不远——提前提醒比事后报警有用。
    seq, flaky = {}, {}
    for ts, _v, det in reversed(rows):
        if ts < cut7:
            continue
        try:
            ps = (json.loads(det) or {}).get("providers", [])
        except Exception:
            continue
        for p in ps:
            s = p.get("status") or "ok"
            if s == "off":
                continue
            seq.setdefault(p.get("group") or "?", []).append(s)
    for g, ss in seq.items():
        fail = len([1 for s in ss if s == "bad"])
        rec = len([1 for i in range(1, len(ss))
                   if ss[i - 1] == "bad" and ss[i] != "bad"])
        if rec >= 2 and fail >= 2:
            flaky[g] = {"fail": fail, "tot": len(ss), "recover": rec}

    return {"uptime30": round(prov_ok * 100.0 / prov_tot, 1) if prov_tot else None,
            "total30": total30, "week_bad": week_bad, "week_total": week_total,
            "probe_ok": probe_ok, "probe_tot": probe_tot, "streak": streak,
            "strips": strips, "flaky": flaky,
            "recent": [{"ts": ts, "verdict": v} for ts, v, _ in recent],
            "bad_since": bad_since}


def hist_incidents(days=30, limit=12):
    """把连续的非 OK 轮合并成「故障事件」，外加逐日视图。
    一次故障散成 5 条记录没有意义，用户要的是「什么时候坏的、坏了多久」。"""
    now = datetime.now(CST)
    cut = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        con = sqlite3.connect(HIST_PATH, timeout=10)
        rows = con.execute("select ts,verdict,detail from checks where ts>=? order by id asc",
                           (cut,)).fetchall()
        con.close()
    except Exception:
        return {"items": [], "total": 0, "days": []}
    if not rows:
        return {"items": [], "total": 0, "days": []}

    items, cur = [], None
    for ts, v, det in rows:
        if v == "OK":
            if cur:
                cur["end"], cur["ongoing"] = ts, False
                items.append(cur)
                cur = None
            continue
        groups = []
        try:
            for p in (json.loads(det) or {}).get("providers", []):
                if p.get("status") == "bad":
                    groups.append(p.get("group") or "?")
        except Exception:
            pass
        if cur is None:
            cur = {"start": ts, "end": None, "rounds": 0, "groups": [],
                   "worst": v, "ongoing": True}
        cur["rounds"] += 1
        for g in groups:
            if g not in cur["groups"]:
                cur["groups"].append(g)
        if v == "FAIL":
            cur["worst"] = "FAIL"
    if cur:
        items.append(cur)

    def fmt_h(h):
        if h < 1:
            return "%d 分钟" % max(1, int(round(h * 60)))
        if h < 24:
            return "%.1f 小时" % h
        return "%.1f 天" % (h / 24.0)

    for it in items:
        t0 = datetime.strptime(it["start"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        t1 = (datetime.strptime(it["end"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
              if it["end"] else now)
        it["hours"] = round(max(0.0, (t1 - t0).total_seconds() / 3600.0), 1)
        it["dur"] = fmt_h(it["hours"])
        it["kind"] = "FAIL" if it["worst"] == "FAIL" else "WARN"
    items.reverse()                                  # 最新在前

    day = {}
    for ts, v, _det in rows:
        r = day.setdefault(ts[:10], {"d": ts[:10], "v": "OK", "n": 0})
        r["n"] += 1
        if v == "FAIL":
            r["v"] = "FAIL"
        elif v == "WARN" and r["v"] != "FAIL":
            r["v"] = "WARN"
    days_out = []
    d0 = now.date() - timedelta(days=days - 1)
    for i in range(days):                            # 补齐没巡检的日子，时间轴才不断格
        d = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        days_out.append(day.get(d) or {"d": d, "v": "", "n": 0})
    return {"items": items[:limit], "total": len(items), "days": days_out}


# ---------------------------------------------------------------- 读存储
def read_storages(conf):
    """只读打开 alist 的 data.db，返回 ({driver,path,status,disabled}, 备注)

    两种打开方式：正常 WAL 只读；WAL 打不开时退回 immutable=1
    （能看到已提交数据，但可能不是最新——会在备注里标出来）。
    """
    path = conf["alist_db"]
    last_err = ""
    for suffix, note in (("?mode=ro", ""),
                         ("?mode=ro&immutable=1", "只读快照模式（数据可能滞后）")):
        try:
            con = sqlite3.connect("file:%s%s" % (path, suffix), uri=True, timeout=8)
            try:
                con.execute("PRAGMA busy_timeout=8000")
                rows = con.execute(
                    "select driver, mount_path, status, disabled from x_storages"
                ).fetchall()
            finally:
                con.close()
            return ([{"driver": r[0], "path": r[1], "status": str(r[2] or ""),
                      "disabled": bool(r[3])} for r in rows], note)
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
    raise RuntimeError(last_err)


# 非网盘账号类驱动，直接忽略（Alias 是路径别名、StrmTxt 是虚拟目录）
SKIP_DRIVERS = {"Alias", "StrmTxt", "UrlTree", "Virtual", "Crypt"}


def group_storages(rows):
    """按分组聚合：{group: {'personal': [...], 'share': [...], 'unknown': [...]}}"""
    out = {}
    for s in rows:
        if s["driver"] in SKIP_DRIVERS:
            continue
        g, kind = DRIVER_MAP.get(s["driver"], (s["driver"], "unknown"))
        out.setdefault(g, {"personal": [], "share": [], "unknown": []})[kind].append(s)
    return out


AUTH_HINT = ("unauthor", "401", "403", "token", "cookie", "passport", "登录",
             "失效", "expired", "invalid", "凭证", "未授权")


def probe_auth(base, path):
    """实时鉴权探测：列一个必定不存在的子目录
    返回 ('ok'|'bad'|'unknown', 说明)
    'object not found' 说明鉴权通过、只是路径不存在；出现鉴权类关键字则判定失效。
    用随机子路径是为了绕开 alist 的目录缓存（缓存按路径命中）。"""
    probe = path.rstrip("/") + "/__aw_%s" % secrets.token_hex(4)
    code, txt = http("POST", base + "/api/fs/list",
                     {"path": probe, "password": "", "page": 1,
                      "per_page": 1, "refresh": False}, timeout=25)
    if code != 200:
        return "unknown", "HTTP %s" % code
    try:
        d = json.loads(txt)
    except Exception:
        return "unknown", "返回无法解析"
    msg = str(d.get("message") or "")
    if d.get("code") == 200:
        return "ok", "可读"
    low = msg.lower()
    if "not found" in low or "object not found" in low:
        return "ok", "鉴权通过"
    if "storage not found" in low:
        return "unknown", "存储未注册"
    if any(k.lower() in low for k in AUTH_HINT):
        return "bad", msg[:90]
    return "unknown", msg[:90]


# ---------------------------------------------------------------- 巡检
def check_once(conf):
    """执行一次巡检，返回 (verdict, summary_lines, detail)"""
    issues, warns, lines = [], [], []
    detail = {"ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
              "providers": [], "issues": [], "warns": []}

    # 1) alist 是否就绪（带重试，避开正好在加载的时刻）
    set_phase("检查 alist 是否就绪")
    ready = False
    for attempt in range(3):
        code, txt = http("GET", conf["alist_base"] + "/api/public/settings")
        if code == 200 and '"code":200' in txt.replace(" ", ""):
            ready = True
            break
        if attempt < 2:
            log("alist 未就绪（HTTP %s），60 秒后重试 %d/3" % (code, attempt + 1))
            time.sleep(60)
    if not ready:
        issues.append("alist 未就绪（存储加载卡住或服务停止）")
        log("alist 未就绪")

    # 2) 读存储状态
    set_phase("读取 alist 存储列表")
    rows, db_err, db_note = None, "", ""
    try:
        rows, db_note = read_storages(conf)
    except Exception as e:
        db_err = "%s: %s" % (type(e).__name__, e)
        warns.append("读不到 data.db（%s）" % db_err)
        log("读 data.db 失败：", db_err)
    detail["db_error"] = db_err
    if db_note:
        warns.append("data.db " + db_note)

    # 3) G-Box 侧的 token 链路（可选：没配地址或密码就跳过，避免误报 WARN）
    set_phase("核对 token 刷新链路")
    token_url, refresh_age = "", None
    gbox_base = (conf.get("gbox_base") or "").strip()
    gbox_pass = (conf.get("gbox_pass") or "").strip()
    if not gbox_base or not gbox_pass:
        log("未配置 G-Box（地址或密码为空），跳过 token 链路检查")
    else:
        try:
            code, txt = http("POST", gbox_base + "/api/accounts/login",
                             {"username": conf.get("gbox_user") or "",
                              "password": gbox_pass})
            tok = None
            if code == 200:
                tok = json.loads(txt).get("token")
            if tok:
                code, txt = http("GET", gbox_base + "/api/settings",
                                 headers={"X-ACCESS-TOKEN": tok})
                if code == 200:
                    st = json.loads(txt)
                    token_url = str(st.get("open_token_url", ""))
                    rtt = str(st.get("refresh_token_time", ""))
                    if rtt:
                        dt = datetime.fromisoformat(rtt.replace("Z", "+00:00"))
                        refresh_age = round(
                            (datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)
            else:
                warns.append("G-Box 登录失败（账号密码或服务地址）")
        except Exception as e:
            warns.append("G-Box 设置读取失败（%s）" % type(e).__name__)
    if refresh_age is not None and refresh_age < -2:
        warns.append("G-Box 记录的刷新时间在未来，NAS 时钟可能不准")
        refresh_age = None
    detail["open_token_url"] = token_url
    detail["refresh_age_h"] = refresh_age
    if token_url and "127.0.0.1" not in token_url and "localhost" not in token_url:
        warns.append("open_token_url 不是本地代理：%s" % token_url)
    if refresh_age is not None and refresh_age > 36:
        issues.append("token 自动刷新已停 %.0f 小时" % refresh_age)

    # 4) 按分组判定
    #    实测（fs/list 真打上游）是有成本的，这里做节流：
    #    同一网盘 min_interval_h 小时内只实测一次，其余轮次复用 data.db 的状态。
    pr = conf.get("probe") or {}
    probe_on = bool(pr.get("enabled", True))
    try:
        min_h = float(pr["min_interval_h"])
    except Exception:
        min_h = 20.0
    try:
        gap_s = float(pr["gap_s"])
    except Exception:
        gap_s = 1.2
    rs = load_runstate()
    pat = dict(rs.get("probe_at") or {})
    probed_now = 0

    skipped = []
    if rows is not None:
        groups = group_storages(rows)
        disabled = set(conf.get("disabled") or [])
        for g in sorted(groups):
            gd = groups[g]
            meta = pmeta(g)
            pers = [s for s in gd["personal"] if not s["disabled"]]
            shs = [s for s in gd["share"] if not s["disabled"]]
            # 只监测「填过账号」的网盘：一个分组若没有任何启用的个人盘，
            # 说明它只是别人的分享链接挂进来的（如 ThunderShare，只需要
            # share_id + 提取码，没有账号可探），不纳入监控——
            # 不探测、不计分、不进推送、不出卡片。
            if not pers:
                skipped.append(g)
                continue
            if g in disabled:
                detail["providers"].append({
                    "group": g, "personal": len(pers), "bad_personal": 0,
                    "share_work": len([s for s in shs if s["status"] == "work"]),
                    "share_total": len(shs), "status": "off",
                    "detail": "", "probe": "", "probe_info": "已关闭监控"})
                continue
            bad_p = [s for s in pers if s["status"] != "work"]
            work_s = [s for s in shs if s["status"] == "work"]
            item = {"group": g, "personal": len(pers), "bad_personal": len(bad_p),
                    "share_work": len(work_s), "share_total": len(shs),
                    "status": "ok", "detail": "", "probe": "", "probe_info": "",
                    "emoji": meta["emoji"], "slug": meta["slug"], "en": meta["en"]}
            if pers and bad_p:
                item["status"] = "bad"
                item["detail"] = bad_p[0]["status"][:60]
                issues.append("%s 个人盘凭证失效：%s" % (g, bad_p[0]["status"][:60]))
            elif not pers and shs and not work_s:
                item["status"] = "bad"
                item["detail"] = shs[0]["status"][:60]
                issues.append("%s 无可用存储：%s" % (g, shs[0]["status"][:60]))
            elif shs and not work_s and pers:
                item["status"] = "warn"
                warns.append("%s 分享目录全部失效（个人盘正常，多为上游取消分享）" % g)

            # 实时鉴权探测（有个人盘探个人盘，否则探一个 work 的分享盘）
            targets = pers[:1] or (work_s[:1] if work_s else [])
            if targets and item["status"] != "bad":
                rec = pat.get(g)
                if isinstance(rec, dict):
                    last_t = float(rec.get("t") or 0)
                    last_r = str(rec.get("r") or "")
                else:
                    last_t, last_r = (float(rec) if rec else 0.0), ""
                age_h = (time.time() - last_t) / 3600.0 if last_t > 0 else 1e9
                if not probe_on:
                    item["probe"], item["probe_info"] = "off", "实测已关闭（只看 alist 记录）"
                elif min_h > 0 and age_h < min_h:
                    # 复用上次实测结论，不重复打上游；上次失败的要继续报，不能被 data.db 的 work 掩盖
                    item["probe"] = last_r or "cached"
                    item["cached"] = True
                    item["probe_info"] = "%.0f 小时前实测%s，本轮未重复请求" % (
                        age_h, "通过" if last_r in ("ok", "") else "异常")
                    if last_r == "bad":
                        item["status"] = "bad"
                        item["detail"] = "上次实测未通过（%.0f 小时前）" % age_h
                        issues.append("%s 上次实测未通过（%.0f 小时前），仍需处理" % (g, age_h))
                    if age_h > min_h * 2.5:
                        warns.append("%s 已 %.0f 小时没实测，建议手动跑一次" % (g, age_h))
                else:
                    set_phase("实测 %s" % g)
                    if probed_now:
                        time.sleep(max(0.0, min(gap_s, 5.0)))
                    probed_now += 1
                    pst, pinfo = probe_auth(conf["alist_base"], targets[0]["path"])
                    pat[g] = {"t": time.time(), "r": pst}
                    item["probe"], item["probe_info"] = pst, pinfo
                    if pst == "bad":
                        item["status"] = "bad"
                        item["detail"] = pinfo
                        issues.append("%s 实时探测失败：%s" % (g, pinfo))
                    elif pst == "unknown":
                        if pers:        # 只有个人盘才把"实测异常"当降级提示
                            warns.append("%s 实测返回异常（%s），请手工确认" % (g, pinfo))

            detail["providers"].append(item)
            if item["status"] == "ok":
                tail = ""
                if item["share_total"]:
                    tail = "｜分享 %d/%d" % (item["share_work"], item["share_total"])
                mark = "实测通过" if item.get("probe") == "ok" else "状态正常"
                lines.append("%s %s ✅ %s%s" % (meta["emoji"], g, mark, tail))
            elif item["status"] == "bad":
                lines.append("%s %s ❌ %s" % (meta["emoji"], g, item["detail"][:26]))
            else:
                lines.append("%s %s ⚠️ 分享全失效" % (meta["emoji"], g))

    if probed_now:
        rs["probe_at"] = pat
        save_runstate(rs)

    set_phase("汇总结果")
    if skipped:
        log("跳过 %d 个纯分享盘分组（没有个人盘）：%s"
            % (len(skipped), "、".join(skipped)))
    if refresh_age is not None:
        lines.append("🔄 token 刷新 %.0f 小时前" % refresh_age)
    detail["issues"] = issues
    detail["warns"] = warns
    detail["skipped_groups"] = skipped

    if issues:
        return "FAIL", lines + ["问题："] + issues, detail
    if warns:
        return "WARN", lines + ["注意："] + warns, detail
    return "OK", lines, detail


# ---------------------------------------------------------------- Bark
def bark_targets(conf):
    """拆出 (服务器地址, device_key)。允许直接把含 key 的完整地址粘进来。"""
    b = conf.get("bark") or {}
    url = (b.get("url") or "").strip().rstrip("/")
    if url.endswith("/push"):
        url = url[:-5]
    key = (b.get("key") or "").strip()
    if url and not key:
        head, _, tail = url.rpartition("/")
        if head and len(tail) >= 6 and "." not in tail:
            url, key = head, tail
    if url and not url.startswith("http"):
        url = "https://" + url
    return url, key


def bark_push(conf, title, body, level="active", jump=""):
    """纯文字推送：不带 icon、不带 image。
    Bark 的 icon 会顶掉 App 图标、image 会撑出半张卡片截图，
    手机上都不好看，所以这里只发标题 + 正文。
    jump 非空时（异常推送）带上 url，点通知直达修复页。"""
    b = conf.get("bark") or {}
    url, key = bark_targets(conf)
    if not url or not key:
        return False, "未配置 Bark 地址或 Key"
    payload = {"device_key": key, "title": title, "body": body,
               "group": b.get("group") or "NAS巡检", "level": level}
    if b.get("sound"):
        payload["sound"] = b["sound"]
    if jump:
        payload["url"] = jump
    last = ""
    for i in range(3):
        code, txt = http("POST", url + "/push", payload, timeout=20)
        if code == 200 and '"code":200' in txt.replace(" ", ""):
            log("bark 推送成功：%s" % title)
            return True, "ok"
        last = "code=%s %s" % (code, txt[:120])
        if i < 2:
            time.sleep(2)
    log("bark 推送失败：", last)
    return False, last


def bark_render(conf, verdict, lines, detail=None, now=None):
    """按用户配置的模板渲染推送内容，返回 (title, body, level)
    可用占位符：{date} {time} {verdict} {count} {bad}"
    """
    b = conf.get("bark") or {}
    now = now or datetime.now(CST)
    detail = detail or {}
    bad_groups = [i.get("group") for i in detail.get("providers", [])
                  if i.get("status") == "bad"]
    subs = {"date": now.strftime("%m-%d"), "time": now.strftime("%H:%M"),
            "verdict": verdict, "count": str(len(bad_groups)),
            "bad": "、".join(bad_groups) if bad_groups else "网盘"}

    def fmt(t):
        for k, v in subs.items():
            t = t.replace("{%s}" % k, v)
        return t

    body = "\n".join(lines)
    if verdict == "OK":
        title = fmt(b.get("title_ok") or "✅ 网盘正常 {date} {time}")
        level = b.get("level_ok") or "active"
    elif verdict == "WARN":
        title = "⚠️ 巡检降级（结论不可信）"
        level = "active"
    else:
        title = fmt(b.get("title_bad") or "⛔ {bad} 需要处理")
        level = b.get("level_bad") or "timeSensitive"
        if b.get("with_hint", True):
            hint = _fix_hint(conf, detail)
            if hint:
                body += "\n\n建议：" + "；".join(hint)
    fl = detail.get("flaky") or {}
    if fl and verdict != "FAIL":
        body += "\n\n近期不稳：" + "、".join(
            "%s %d/%d 轮失败" % (g, v.get("fail", 0), v.get("tot", 0))
            for g, v in sorted(fl.items()))
    return title, body, level


# ---------------------------------------------------------------- 执行+推送
def _decide_push(conf, verdict):
    """按推送策略判断这次要不要发，返回 (要不要发, 原因, 新的 kind)"""
    st = load_runstate()
    n = conf.get("notify") or {}
    policy = n.get("policy") or "always"
    thr = max(1, min(10, int(n.get("fail_threshold") or 1)))
    repeat_bad = bool(n.get("repeat_bad", True))

    streak = int(st.get("streak_bad") or 0)
    if verdict == "OK":
        streak = 0
    else:
        streak += 1
    last_kind = st.get("last_kind") or ""
    is_bad = verdict != "OK"

    should, reason = False, ""
    if is_bad:
        if streak < thr:
            reason = "连续异常 %d 轮，未到阈值 %d" % (streak, thr)
        elif last_kind != "bad":
            should, reason = True, "首次异常"
        elif repeat_bad:
            should, reason = True, "持续异常"
        else:
            reason = "异常未恢复，已静默"
    else:
        if last_kind == "bad":
            should, reason = True, "已恢复"
        elif policy == "always" and conf.get("notify_when_ok", True):
            should, reason = True, "例行汇报"
        else:
            reason = "状态未变化"

    st["verdict"] = verdict
    st["streak_bad"] = streak
    st["ts"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    save_runstate(st)
    return should, reason, ("bad" if is_bad else "ok"), st


def _mark_pushed(kind):
    st = load_runstate()
    st["last_kind"] = kind
    st["last_push_ts"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    save_runstate(st)


def _execute(conf, push=True):
    """真正跑一次巡检（调用方已持有 running 标记）"""
    verdict, lines, detail = check_once(conf)
    now = datetime.now(CST)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    # 抖动预警：还没坏、但近 7 天在 ok/bad 之间反复横跳的盘，往往是凭证快到期的前兆
    try:
        fl = hist_stats(days=7).get("flaky") or {}
    except Exception:
        fl = {}
    if fl:
        detail["flaky"] = fl
    summary = " / ".join(lines[:6])
    rid = hist_add(ts, verdict, summary, {"lines": lines, **detail})
    res = {"verdict": verdict, "lines": lines, "ts": ts, "push": "", "cat": ts[:10],
           "hist_id": rid}

    should, reason, kind, _st = _decide_push(conf, verdict)
    res["push_reason"] = reason
    if push:
        if not should:
            res["push"] = "skip"
        elif not bark_targets(conf)[1]:
            res["push"] = "skip"
            res["push_reason"] = "未配置 Bark"
        else:
            title, body, level = bark_render(conf, verdict, lines, detail, now)
            jump = ""
            if verdict == "FAIL":
                # 点通知直达 alist 存储管理页，省得自己找入口
                jump = (conf.get("alist_base") or "").rstrip("/") + "/@manage/storage"
                # 同一个问题只在第一次强提醒，之后几轮转静默进通知中心，恢复才再响
                fp = "|".join(sorted(i.get("group") or "" for i in
                                     detail.get("providers", [])
                                     if i.get("status") == "bad"))
                st = load_runstate()
                if fp and fp == (st.get("bad_fp") or "") and st.get("last_kind") == "bad":
                    level = "passive"
                    res["push_reason"] += " · 同问题转静默"
                elif fp:
                    st["bad_fp"] = fp
                    save_runstate(st)
            if verdict == "OK":
                st = load_runstate()
                if st.pop("bad_fp", None) is not None:
                    save_runstate(st)
            ok, msg = bark_push(conf, title, body, level, jump=jump)
            res["push"] = "ok" if ok else msg
            if ok:
                _mark_pushed(kind)

    log("巡检完成 %s | 推送=%s %s" % (verdict, res["push"], res.get("push_reason", "")))
    with _state_lock:
        STATE["last"] = res
        STATE["last_error"] = ""
        STATE["phase"] = ""
    heartbeat(conf)          # 这轮活干完了，向外面报个平安
    return res


def heartbeat(conf, fail=False):
    """外部心跳：每轮巡检结束 GET 一次。NAS/容器/程序本身挂了，
    外部（Uptime Kuma / healthchecks.io）收不到心跳就会报警——
    这是唯一能发现「Netwatch 自己死了」的手段。崩溃时打 地址/fail。"""
    hb = ((conf.get("heartbeat") or {}).get("url") or "").strip().rstrip("/")
    if not hb:
        return
    try:
        http("GET", hb + ("/fail" if fail else ""), timeout=6)
        log("心跳已上报%s" % ("（fail）" if fail else ""))
    except Exception as e:
        log("心跳上报失败：", e)


def paused_left():
    """还剩多少秒暂停；<=0 表示没暂停。换凭证/搬机器时用得上。"""
    try:
        return max(0.0, float(load_runstate().get("pause_until") or 0) - time.time())
    except Exception:
        return 0.0


def probe_one(conf, group):
    """只测一个网盘：不写历史、不推送，只刷新该组的实测时间戳。
    刚重新登录某个盘时，不必等下一轮、也不必惊动其他盘。"""
    try:
        rows, _note = read_storages(conf)
    except Exception as e:
        return {"ok": False, "msg": "读不到 data.db：%s" % e}
    gd = group_storages(rows).get(group)
    if not gd:
        return {"ok": False, "msg": "没有这个网盘"}
    pers = [s for s in gd["personal"] if not s["disabled"]]
    if not pers:
        return {"ok": False, "msg": "该网盘只有分享盘、没有填账号，不在监控范围"}
    targets = pers[:1]
    if not targets:
        return {"ok": False, "msg": "该网盘没有可测的存储"}
    pst, info = probe_auth(conf["alist_base"], targets[0]["path"])
    rs = load_runstate()
    pat = dict(rs.get("probe_at") or {})
    pat[group] = {"t": time.time(), "r": pst}
    rs["probe_at"] = pat
    save_runstate(rs)
    log("单盘实测 %s：%s %s" % (group, pst, info))
    return {"ok": pst == "ok", "status": pst, "info": info,
            "ts": datetime.now(CST).strftime("%m-%d %H:%M")}


def run_and_notify(conf=None, push=True):
    """同步执行（调度线程用）"""
    conf = conf or load_conf()
    with _state_lock:
        if STATE["running"]:
            return {"error": "正在执行中"}
        STATE["running"] = True
    try:
        return _execute(conf, push)
    except Exception as e:
        return _on_crash(conf, e)
    finally:
        with _state_lock:
            STATE["running"] = False
            STATE["phase"] = ""


def start_run(conf, push=True):
    """后台执行，立即返回。前端轮询 /api/state 拿结果。"""
    with _state_lock:
        if STATE["running"]:
            return False
        STATE["running"] = True
        STATE["phase"] = "启动"

    def job():
        try:
            _execute(conf, push)
        except Exception as e:
            _on_crash(conf, e)
        finally:
            with _state_lock:
                STATE["running"] = False
                STATE["phase"] = ""

    threading.Thread(target=job, daemon=True).start()
    return True


def _on_crash(conf, e):
    """巡检自己崩了也要吭声，否则用户以为"没消息就是好消息"""
    msg = "%s: %s" % (type(e).__name__, e)
    log("巡检异常：", msg)
    with _state_lock:
        STATE["last_error"] = msg
        STATE["phase"] = ""
    try:
        st = load_runstate()
        last = st.get("last_crash_push") or ""
        if time.time() - float(st.get("last_crash_ts") or 0) > 3600:
            ok, _ = bark_push(conf, "⚠️ 巡检程序出错",
                              "Netwatch 自己报错了，这一轮没有结果：\n" + msg[:300],
                              "timeSensitive")
            if ok:
                st["last_crash_ts"] = time.time()
                st["last_crash_push"] = msg[:200]
                save_runstate(st)
        heartbeat(conf, fail=True)      # 崩了也要让外部知道
    except Exception:
        pass
    return {"error": msg}


def _fix_hint(conf, detail):
    hints = []
    for it in detail.get("providers", []):
        if it.get("status") == "bad":
            g = it["group"]
            if g == "光鸭":
                hints.append("光鸭需重新扫码授权")
            elif g in ("115", "夸克"):
                hints.append("%s 需在 G-Box 后台重填 cookie" % g)
            elif g == "阿里":
                hints.append("阿里检查 open_token_url 并重启 g-box")
            else:
                hints.append("%s 检查该网盘登录状态" % g)
    return hints or ["查看容器日志"]


# ---------------------------------------------------------------- 调度
TIME_RE = re.compile(r"^(\d{1,2})\s*[:：]\s*(\d{1,2})$")


def schedule_times(conf):
    """解析每天要跑几次、几点跑，返回去重排序后的 [(h, m), ...]
    配置写成 schedule.times = ["08:30", "12:30", "19:30"]；
    老配置的 schedule.hour/minute 仍然兼容。"""
    sc = conf.get("schedule") or {}
    out = set()
    for t in (sc.get("times") or []):
        mt = TIME_RE.match(str(t).strip())
        if mt:
            h, m = int(mt.group(1)), int(mt.group(2))
            if 0 <= h <= 23 and 0 <= m <= 59:
                out.add((h, m))
    if not out:
        try:
            out.add((int(sc.get("hour", 9)) % 24, int(sc.get("minute", 30)) % 60))
        except Exception:
            out.add((9, 30))
    return sorted(out)


def schedule_text(conf):
    return " / ".join("%02d:%02d" % t for t in schedule_times(conf))


def next_run(conf, after=None):
    """今天剩下的时刻里最早的那个；都过了就取明天第一个"""
    now = after or datetime.now(CST)
    best = None
    for h, m in schedule_times(conf):
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        if best is None or t < best:
            best = t
    return best or now


def scheduler():
    time.sleep(15)
    try:
        log("启动自检开始")
        run_and_notify(push=False)          # 启动先跑一次，UI 立刻有数据
    except Exception as e:
        log("启动自检失败：", e)
    while True:
        try:
            conf = load_conf()
            nxt = next_run(conf)
            log("每天 %s 巡检，下次 %s" % (schedule_text(conf),
                                          nxt.strftime("%m-%d %H:%M")))
            while True:
                remain = (nxt - datetime.now(CST)).total_seconds()
                if remain <= 0:
                    break
                time.sleep(min(20, max(1, remain)))
                # 在界面上改了时刻，最多 20 秒就重新排程，不用重启容器
                nc = load_conf()
                if schedule_times(nc) != schedule_times(conf):
                    conf = nc
                    nxt = next_run(conf)
                    log("巡检时刻改为 %s，重新排程：下次 %s"
                        % (schedule_text(conf), nxt.strftime("%m-%d %H:%M")))
            left = paused_left()
            if left > 0:
                # 暂停期间仍然上报心跳：程序活着只是不巡检，别让外部误判成宕机
                log("巡检已暂停，跳过本轮（剩余 %.1f 小时）" % (left / 3600.0))
                heartbeat(load_conf())
            else:
                run_and_notify(load_conf(), push=True)
            time.sleep(60)                  # 跨过整分钟，避免同一时刻被连跑两次
        except Exception as e:
            log("调度异常：", e)
            with _state_lock:
                STATE["last_error"] = "%s: %s" % (type(e).__name__, e)
            time.sleep(60)


# ---------------------------------------------------------------- 页面
LOGIN_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>登录 · Netwatch</title>
<link rel="icon" href="/icon-32.png" sizes="32x32" type="image/png">
<link rel="icon" href="/icon-192.png" sizes="192x192" type="image/png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#eef0f6" media="(prefers-color-scheme:light)">
<meta name="theme-color" content="#0d0f14" media="(prefers-color-scheme:dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Netwatch">
<style>
:root{--bg:#f5f5f7;--surf:#fff;--txt:#1d1d1f;--txt2:#6e6e73;--txt3:#8e8e93;
--line:#e5e5ea;--brand:#1d1d1f;--brand2:#3a3a3c;--bad:#d70015;
--shadow:0 1px 2px rgba(0,0,0,.04),0 24px 48px -24px rgba(0,0,0,.22)}
@media(prefers-color-scheme:dark){:root{--bg:#0d0d0f;--surf:#1c1c1e;--txt:#f5f5f7;
--txt2:#a1a1a6;--txt3:#8e8e93;--line:#2c2c2e;
--brand:#f5f5f7;--brand2:#d1d1d6;--bad:#ff6961;
--shadow:0 1px 2px rgba(0,0,0,.4),0 24px 48px -24px rgba(0,0,0,.8)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{margin:0;color:var(--txt);display:flex;align-items:center;
justify-content:center;padding:24px;overflow:hidden;
background:radial-gradient(760px 620px at 6% -8%,#ececf0 0%,transparent 62%),
radial-gradient(820px 680px at 102% 108%,#eaeaef 0%,transparent 60%),var(--bg);
background-attachment:fixed;
font:0.9375rem/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI","Microsoft YaHei",sans-serif;
font-optical-sizing:auto;-webkit-font-smoothing:antialiased}
@media(prefers-color-scheme:dark){
body{background:radial-gradient(760px 620px at 6% -8%,#1a1a1e 0%,transparent 62%),
radial-gradient(820px 680px at 102% 108%,#191920 0%,transparent 60%),var(--bg)}}
.box{position:relative;width:100%;max-width:390px;background:var(--surf);
border:1px solid var(--line);border-radius:22px;padding:36px 32px 26px;
box-shadow:var(--shadow);animation:in .5s cubic-bezier(.16,1,.3,1)}
@keyframes in{from{transform:translateY(16px) scale(.985)}to{transform:none}}
.logo{width:58px;height:58px;margin-bottom:20px;border-radius:17px;display:block;
box-shadow:0 12px 26px -12px rgba(0,0,0,.75)}
img.logo{object-fit:cover}
h1{font-size:1.3125rem;margin:0 0 6px;letter-spacing:-0.01em;font-weight:650}
p.s{margin:0 0 24px;color:var(--txt2);font-size:0.8438rem}
label{display:block;font-size:0.7812rem;color:var(--txt2);margin-bottom:7px;font-weight:500}
.inp{position:relative}
input{width:100%;padding:13px 44px 13px 14px;border:1px solid var(--line);border-radius:12px;
background:transparent;color:var(--txt);font-size:0.9375rem;outline:none;transition:.18s}
input::placeholder{color:var(--txt3)}
input:focus{border-color:var(--brand);box-shadow:0 0 0 3.5px rgba(0,0,0,.13)}
.eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:34px;height:34px;
display:grid;place-items:center;border:0;background:none;cursor:pointer;color:var(--txt3);border-radius:9px}
.eye:hover{color:var(--txt);background:rgba(125,130,150,.1)}
button.go{width:100%;margin-top:18px;padding:13px;border:0;border-radius:12px;cursor:pointer;
font-size:0.9375rem;font-weight:600;color:var(--bg);letter-spacing:.013em;
background:var(--brand);
box-shadow:0 10px 22px -12px rgba(0,0,0,.9);transition:transform 100ms ease-out,background .2s}
button.go:hover{filter:brightness(1.08);transform:translateY(-1px)}
button.go:active{transform:scale(.99)}
button.go:disabled{opacity:.6;cursor:default;transform:none}
.err{display:none;margin-top:14px;padding:10px 12px;border-radius:10px;font-size:0.8125rem;
background:rgba(239,68,68,.1);color:var(--bad);border:1px solid rgba(239,68,68,.22)}
.err.on{display:block;animation:shake .35s}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-5px)}75%{transform:translateX(5px)}}
.foot{margin-top:22px;text-align:center;font-size:0.7188rem;color:var(--txt3);letter-spacing:0.017em}
</style></head><body>
<form class="box" onsubmit="return go(event)">
  <img class="logo" src="/icon-192.png" alt="Netwatch" width="58" height="58">
  <h1>Netwatch</h1>
  <p class="s">NAS 网盘凭证巡检 · 输入访问密码</p>
  <label for="pw">访问密码</label>
  <div class="inp">
    <input id="pw" type="password" placeholder="输入访问密码" autocomplete="current-password"
      autofocus oninput="document.getElementById('err').classList.remove('on')">
    <button class="eye" type="button" onclick="tog()" aria-label="显示密码" id="eye">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.9" stroke-linecap="round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/>
        <circle cx="12" cy="12" r="3"/></svg>
    </button>
  </div>
  <button class="go" id="btn" type="submit">进入</button>
  <div class="err" id="err"></div>
  <div class="foot">Netwatch · 每日自动巡检</div>
</form>
<script>
function tog(){
  var i=document.getElementById('pw');
  i.type = i.type==='password' ? 'text' : 'password';
}
async function go(e){
  e.preventDefault();
  var pw=document.getElementById('pw').value;
  if(!pw){show('请输入密码');return false}
  var b=document.getElementById('btn'); b.disabled=true; b.textContent='验证中…';
  try{
    var r=await fetch('/api/login',{method:'POST',body:JSON.stringify({password:pw})});
    var d=await r.json();
    if(d.ok){location.replace('/');return false}
    show(d.msg||'密码不正确');
  }catch(err){show('连接失败：'+err.message)}
  b.disabled=false; b.textContent='进入';
  return false;
}
function show(m){
  var e=document.getElementById('err');
  e.textContent=m; e.classList.remove('on');
  void e.offsetWidth; e.classList.add('on');
}
</script></body></html>"""


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Netwatch · 网盘巡检</title>
<link rel="icon" href="/icon-32.png" sizes="32x32" type="image/png">
<link rel="icon" href="/icon-192.png" sizes="192x192" type="image/png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#f5f5f7" media="(prefers-color-scheme:light)">
<meta name="theme-color" content="#0d0d0f" media="(prefers-color-scheme:dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Netwatch">
<style>
:root{
--bg:#f5f5f7;--surf:#fff;--surf2:#fafafa;
--line:#e5e5ea;--line2:#f0f0f2;
--txt:#1d1d1f;--txt2:#6e6e73;--txt3:#8e8e93;
--brand:#1d1d1f;--brand2:#3a3a3c;
--ok:#1d8f45;--okbg:#eaf7ee;--okln:#c6e6d0;
--bad:#d70015;--badbg:#ffeef0;--badln:#ffd0d5;
--warn:#9a5b00;--warnbg:#fff7e6;--warnln:#f0dcb4;
--sh:0 1px 2px rgba(16,24,40,.04),0 10px 28px -18px rgba(16,24,40,.24);
--sh2:0 1px 2px rgba(16,24,40,.06),0 18px 40px -22px rgba(16,24,40,.32);
}
[data-theme=dark]{
--bg:#0d0d0f;--surf:#1c1c1e;--surf2:#242426;
--line:#2c2c2e;--line2:#242426;
--txt:#f5f5f7;--txt2:#a1a1a6;--txt3:#8e8e93;
--brand:#f5f5f7;--brand2:#d1d1d6;
--ok:#30d158;--okbg:#0e2617;--okln:#1c4128;
--bad:#ff6961;--badbg:#2b1416;--badln:#5a2225;
--warn:#ffb340;--warnbg:#291f0e;--warnln:#54401c;
--sh:0 1px 2px rgba(0,0,0,.35),0 10px 28px -18px rgba(0,0,0,.7);
--sh2:0 1px 2px rgba(0,0,0,.4),0 18px 40px -22px rgba(0,0,0,.8);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--txt);padding-bottom:40px;overflow-x:hidden;
font:0.875rem/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI","Microsoft YaHei",sans-serif;
font-optical-sizing:auto;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1060px;margin:0 auto;padding:0 18px}
a{color:var(--brand)}

/* ---------- header ---------- */
.hd{position:sticky;top:0;z-index:30;backdrop-filter:saturate(180%) blur(14px);
-webkit-backdrop-filter:saturate(180%) blur(14px);
background:color-mix(in srgb,var(--bg) 82%,transparent);border-bottom:1px solid transparent;
transition:border-color .22s ease}
/* 滚动时才浮出边缘：一条极淡分界 + 下方渐隐，替代常驻硬线 */
.hd::after{content:"";position:absolute;left:0;right:0;top:100%;height:14px;pointer-events:none;
background:linear-gradient(to bottom,color-mix(in srgb,var(--bg) 62%,transparent),transparent);
opacity:0;transition:opacity .22s ease}
.hd.scrolled{border-bottom-color:color-mix(in srgb,var(--line) 72%,transparent)}
.hd.scrolled::after{opacity:1}
.hd-in{max-width:1060px;margin:0 auto;padding:12px 18px;display:flex;align-items:center;gap:14px}
.brand{display:flex;align-items:center;gap:11px;margin-right:auto;min-width:0}
.blogo{width:36px;height:36px;border-radius:11px;flex:none;object-fit:cover;
box-shadow:0 6px 16px -8px rgba(0,0,0,.8)}
.bname{font-size:0.9375rem;font-weight:650;letter-spacing:-0.013em;line-height:1.2}
.bsub{font-size:0.7188rem;color:var(--txt3);line-height:1.3}
.hd-act{display:flex;align-items:center;gap:8px}

.btn{border:1px solid var(--line);background:var(--surf);color:var(--txt);
border-radius:10px;padding:8px 14px;font-size:0.8125rem;font-weight:500;cursor:pointer;
transition:border-color .16s,color .16s,background .16s,box-shadow .16s,transform 100ms ease-out;
white-space:nowrap;display:inline-flex;align-items:center;gap:6px}
.btn:hover{border-color:var(--txt3);color:var(--txt)}
.btn:active{transform:scale(.97)}
.btn.pri{background:var(--brand);border-color:transparent;
color:var(--bg);box-shadow:0 8px 18px -12px rgba(0,0,0,.95)}
.btn.pri:hover{color:var(--bg);border-color:transparent;filter:brightness(1.08)}
.btn.ghost{border-color:transparent;background:transparent;color:var(--txt2)}
.btn.ghost:hover{background:var(--line2);color:var(--txt)}
.btn:disabled{opacity:.55;cursor:default;transform:none}
.btn.sm{padding:6px 11px;font-size:0.7813rem;border-radius:9px}

/* ---------- hero ---------- */
.hero{margin:20px 0 16px;border-radius:18px;padding:22px;border:1px solid var(--line);
background:var(--surf);box-shadow:var(--sh2);display:flex;gap:22px;align-items:center;
position:relative;overflow:hidden}
.hero::after{content:"";position:absolute;inset:0;pointer-events:none;opacity:.55;
background:radial-gradient(700px 180px at 100% 0%,var(--okbg),transparent 70%)}
.hero.s-bad::after{background:radial-gradient(700px 180px at 100% 0%,var(--badbg),transparent 70%)}
.hero.s-warn::after{background:radial-gradient(700px 180px at 100% 0%,var(--warnbg),transparent 70%)}
.hero-l{position:relative;z-index:1;min-width:0;flex:1}
.hstate{display:flex;align-items:center;gap:10px;font-size:1.375rem;font-weight:680;letter-spacing:-0.018em;
margin-bottom:8px;line-height:1.3}
.pulse{width:11px;height:11px;border-radius:50%;flex:none;position:relative;background:var(--ok)}
.pulse.bad{background:var(--bad)}.pulse.warn{background:var(--warn)}.pulse.none{background:var(--txt3)}
.pulse::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:2px solid currentColor;
color:var(--ok);opacity:0;animation:pl 2.2s ease-out infinite}
.pulse.bad::after{color:var(--bad)}.pulse.warn::after{color:var(--warn)}.pulse.none::after{display:none}
@keyframes pl{0%{transform:scale(.7);opacity:.7}70%,100%{transform:scale(1.9);opacity:0}}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--txt2);font-size:0.7812rem}
.meta .mi{white-space:nowrap}
.meta b{color:var(--txt);font-weight:600}
.hero-r{position:relative;z-index:1;display:flex;gap:10px;flex:none}
.stat{min-width:86px;border:1px solid transparent;border-radius:13px;padding:11px 13px;
background:var(--bg);text-align:center}
[data-theme=dark] .stat{background:var(--surf2)}
.stat .v{font-size:1.25rem;font-weight:680;letter-spacing:-0.025em;line-height:1.25}
.stat .k{font-size:0.6875rem;color:var(--txt3);margin-top:2px}
.stat.ok .v{color:var(--ok)}.stat.bad .v{color:var(--bad)}

/* ---------- provider cards ---------- */
.sec{margin:0 0 8px;display:flex;align-items:baseline;gap:10px}
.sec h2{font-size:0.8125rem;font-weight:600;color:var(--txt2);margin:0;letter-spacing:0.015em}
.sec .hint{font-size:0.75rem;color:var(--txt3)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(232px,1fr));gap:12px;margin-bottom:22px}
.pv{position:relative;display:flex;flex-direction:column;border:1px solid var(--line);
border-radius:15px;padding:14px 15px 13px;background:var(--surf);box-shadow:var(--sh);
overflow:hidden;min-width:0;transition:.18s}
@media (hover:hover){.pv:hover{box-shadow:var(--sh2);transform:translateY(-1px)}}
.pv::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--ok)}
.pv.st-bad::before{background:var(--bad)}
.pv.st-warn::before{background:var(--warn)}
.pv.st-off{opacity:.5}
.pv.st-off::before{background:var(--txt3)}
.pv-t{display:flex;align-items:center;gap:8px;margin-bottom:9px;flex-wrap:wrap;min-width:0}
.pv-n{font-size:0.9062rem;font-weight:640;letter-spacing:-0.007em;min-width:0;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pv-t .pill{margin-left:auto}
.pill{font-size:0.6875rem;font-weight:600;padding:2px 9px;border-radius:20px;letter-spacing:0.018em;
border:1px solid transparent;white-space:nowrap}
.pill.ok{background:var(--okbg);color:var(--ok);border-color:var(--okln)}
.pill.bad{background:var(--badbg);color:var(--bad);border-color:var(--badln)}
.pill.warn{background:var(--warnbg);color:var(--warn);border-color:var(--warnln)}
.pill.mute{background:var(--line2);color:var(--txt3);border-color:var(--line)}
.pv-m{color:var(--txt2);font-size:0.7812rem;display:flex;flex-direction:column;gap:3px;
flex:1 1 auto;min-width:0}
.pv-m .r{display:flex;justify-content:space-between;gap:10px;min-width:0}
.pv-m .r span{white-space:nowrap;flex:none}
.pv-m .r b{color:var(--txt);font-weight:600;text-align:right;min-width:0;
overflow-wrap:anywhere}

.statgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:20px}
.scard{background:var(--surf);border:1px solid var(--line);border-radius:13px;padding:10px 13px}
.scard .k{font-size:0.7188rem;color:var(--txt3)}
.scard .v{font-size:1.25rem;font-weight:650;margin-top:3px;letter-spacing:-0.015em}
.scard .v small{font-size:0.75rem;font-weight:500;color:var(--txt3);margin-left:0}
.heat{display:flex;gap:2px;margin-top:11px}
.heat i{flex:1;height:7px;border-radius:2px;opacity:.9;min-width:4px}
.heat-cap{font-size:0.6875rem;color:var(--txt3);margin-top:5px}
.pv-d{margin-top:8px;font-size:0.7188rem;line-height:1.55;color:var(--bad);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.pv-d.warn{color:var(--warn)}
.pv-t .fix{font-size:0.75rem;color:var(--bad);text-decoration:none;white-space:nowrap;flex:none}
.pv-t .fix:hover{text-decoration:underline}

/* ---------- 品牌色 / 抖动 / 事件 ---------- */
/* 左侧竖条留给状态（绿/红/黄），顶部 2px 留给「这是哪个网盘」 */
.pv::after{content:"";position:absolute;left:0;right:0;top:0;height:3px;
background:var(--c,transparent);border-radius:15px 15px 0 0}
.pv.st-bad{border-color:var(--badln)}
.pv.st-warn{border-color:var(--warnln)}
.pv-t .rd{font-family:inherit;font-size:0.7188rem;color:var(--txt3);background:none;border:0;
padding:0;cursor:pointer;flex:none;white-space:nowrap}
.pv-t .rd:hover{color:var(--brand)}
.pv-t .rd:disabled{opacity:.45;cursor:default}
.tag-flaky{font-size:0.6562rem;font-weight:600;padding:1px 7px;border-radius:20px;flex:none;
background:var(--warnbg);color:var(--warn);border:1px solid var(--warnln);white-space:nowrap}
.hsum{font-size:0.8125rem;color:var(--txt2);line-height:1.6;margin:-2px 0 8px}
.hprog{position:absolute;left:0;right:0;top:0;height:2px;overflow:hidden;z-index:3;display:none}
.hprog i{position:absolute;top:0;bottom:0;width:34%;background:var(--brand);border-radius:2px;
animation:sl 1.3s ease-in-out infinite}
@keyframes sl{0%{left:-34%}100%{left:100%}}
.acts{display:flex;align-items:center;gap:8px;margin:-6px 0 16px;flex-wrap:wrap}
.acts .sp{flex:1}
.pausebar{display:flex;align-items:center;gap:10px;margin-bottom:14px;padding:9px 14px;
border-radius:12px;border:1px solid var(--warnln);background:var(--warnbg);
color:var(--warn);font-size:0.7812rem}
.pausebar .btn{margin-left:auto}
.dbar{display:flex;gap:3px;margin-top:6px}
.dbar i{flex:1;height:20px;border-radius:3px;background:var(--txt3);opacity:.16;
border:1px solid transparent}
.dbar i.ok{background:var(--okbg);opacity:1;border-color:var(--okln)}
.dbar i.warn{background:var(--warnbg);opacity:1;border-color:var(--warnln)}
.dbar i.bad{background:var(--bad);opacity:.85;border-color:transparent}
.dbar-x{display:flex;justify-content:space-between;font-size:0.6875rem;color:var(--txt3);margin-top:5px}
.inc{display:flex;gap:11px;padding:11px 0;align-items:flex-start}
.inc+.inc{border-top:1px dashed var(--line)}
.inc .dot{width:8px;height:8px;border-radius:50%;flex:none;margin-top:6px;background:var(--bad)}
.inc.warn .dot{background:var(--warn)}
.inc-b{min-width:0;flex:1}
.inc-t{font-size:0.8125rem;font-weight:600;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.inc-m{font-size:0.7188rem;color:var(--txt3);margin-top:3px;line-height:1.5}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;
padding:26px 10px;color:var(--txt3);font-size:0.7812rem;text-align:center;grid-column:1/-1}
.empty svg{opacity:.45}
.empty.err{color:var(--bad)}
.hday{display:flex;align-items:center;gap:10px;padding:5px 0}
.hday+.hday{border-top:1px dashed var(--line)}
.hday .d{font-size:0.75rem;color:var(--txt3);width:44px;flex:none}
.hday .c{display:flex;gap:4px;flex-wrap:wrap}
.hday .c i{width:12px;height:12px;border-radius:3px;cursor:pointer}
.hday .c i.ok{background:var(--ok)}.hday .c i.bad{background:var(--bad)}.hday .c i.warn{background:var(--warn)}
@media(max-width:820px){.statgrid{grid-template-columns:repeat(2,minmax(0,1fr))}}

/* ---------- tabs ---------- */
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:18px;overflow-x:auto}
.tab{padding:10px 15px;font-size:0.8438rem;font-weight:550;color:var(--txt2);cursor:pointer;
border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap;transition:.15s}
.tab:hover{color:var(--txt)}
.tab.on{color:var(--brand);border-bottom-color:var(--brand)}
.panel{display:none;animation:fade .28s ease}
.panel.on{display:block}
@keyframes fade{from{transform:translateY(6px)}to{transform:none}}

/* ---------- cards / forms ---------- */
.card{background:var(--surf);border:1px solid var(--line);border-radius:16px;padding:18px;
margin-bottom:14px;box-shadow:var(--sh)}
.card>h3{font-size:0.8438rem;font-weight:600;margin:0 0 4px;letter-spacing:0.007em}
.card>p.d{font-size:0.7812rem;color:var(--txt3);margin:0 0 15px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
.f{margin-bottom:13px}
label{display:block;font-size:0.7812rem;color:var(--txt2);margin-bottom:6px;font-weight:500}
input,select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;
background:var(--surf2);color:var(--txt);font-size:0.8438rem;outline:none;transition:.16s;
font-family:inherit}
input:focus,select:focus{border-color:var(--brand);background:var(--surf);
box-shadow:0 0 0 3px rgba(0,0,0,.12)}
input::placeholder{color:var(--txt3)}
.hint{font-size:0.7188rem;color:var(--txt3);margin-top:5px;line-height:1.5}
.skipnote{font-size:0.7188rem;color:var(--txt3);margin-top:10px;line-height:1.6;
padding-left:2px}
.skipnote b{font-weight:600;color:var(--txt2)}
.hint code{background:var(--line2);padding:1px 5px;border-radius:5px;font-size:0.6875rem}

/* chips */
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{border:1px solid var(--line);border-radius:11px;padding:9px 13px;font-size:0.8125rem;
cursor:pointer;user-select:none;background:var(--surf2);transition:.16s;display:flex;
align-items:center;gap:8px;font-weight:500}
.chip:hover{border-color:var(--brand)}
.chip .sw{width:30px;height:17px;border-radius:20px;background:var(--ok);position:relative;
transition:.2s;flex:none}
.chip .sw::after{content:"";position:absolute;top:2px;left:2px;width:13px;height:13px;
border-radius:50%;background:#fff;transition:.2s;box-shadow:0 1px 2px rgba(0,0,0,.2)}
.chip.off{border-color:var(--line);color:var(--txt3)}
.chip.off .sw{background:var(--line)}
.chip.off .sw::after{transform:translateX(13px)}

/* phone preview */
.notify-wrap{display:grid;grid-template-columns:minmax(0,1fr) 288px;gap:24px;align-items:start}
.phone{background:linear-gradient(160deg,#2b2f3a,#1a1d24);border-radius:24px;padding:16px 13px 20px;
box-shadow:0 18px 40px -20px rgba(0,0,0,.6);position:sticky;top:76px}
.phone .bar-top{width:44px;height:4px;border-radius:4px;background:rgba(255,255,255,.22);
margin:0 auto 14px}
.notif{background:rgba(255,255,255,.92);backdrop-filter:blur(10px);border-radius:16px;padding:11px 13px;
color:#111}
.notif-t{display:flex;align-items:center;gap:6px;font-size:0.6562rem;color:#6b7280;margin-bottom:5px}
.notif-t .tm{margin-left:auto}
.notif h4{margin:0 0 3px;font-size:0.8125rem;font-weight:650;color:#111;letter-spacing:-0.008em}
.notif p{margin:0;font-size:0.75rem;color:#4b5563;white-space:pre-wrap;line-height:1.45;
max-height:176px;overflow:auto}
.ph-hint{text-align:center;color:rgba(255,255,255,.4);font-size:0.6562rem;margin-top:12px}

/* history */
.tbl{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:0.7812rem}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line2)}
th{color:var(--txt3);font-weight:500;font-size:0.7188rem;letter-spacing:0.026em;text-transform:uppercase}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--surf2)}
.td-sum{color:var(--txt2);max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* toast */
#toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,14px);z-index:99;
background:#111827;color:#fff;padding:11px 20px;border-radius:26px;font-size:0.8125rem;font-weight:500;
opacity:0;transition:.28s cubic-bezier(.16,1,.3,1);pointer-events:none;
box-shadow:0 12px 28px -12px rgba(0,0,0,.7);max-width:90vw;text-align:center}
#toast.on{opacity:1;transform:translate(-50%,0)}
.foot-note{text-align:center;color:var(--txt3);font-size:0.7188rem;margin-top:26px}
.foot-note a{text-decoration:none}

/* ---------- 网盘徽标 ---------- */
.pvb{width:22px;height:22px;flex:none;display:block;
filter:drop-shadow(0 1px 2px rgba(0,0,0,.16))}
.pvb.lg{width:30px;height:30px}
.pvb.sm{width:18px;height:18px}
.tbl td .pvb{display:inline-block;vertical-align:-4px}

/* ---------- 运行中 ---------- */
.spin{width:13px;height:13px;border-radius:50%;flex:none;
border:2px solid color-mix(in srgb,currentColor 28%,transparent);
border-top-color:currentColor;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.phase{font-size:0.75rem;color:var(--brand);font-weight:500}

/* ---------- 保存条 ---------- */
#savebar{position:fixed;left:50%;bottom:22px;transform:translate(-50%,120%);z-index:80;
display:flex;align-items:center;gap:12px;padding:10px 12px 10px 18px;border-radius:16px;
background:var(--surf);border:1px solid var(--line);box-shadow:0 18px 40px -18px rgba(16,24,40,.5);
transition:transform .32s cubic-bezier(.16,1,.3,1);max-width:calc(100vw - 28px)}
#savebar.on{transform:translate(-50%,0)}
#savebar .t{font-size:0.8125rem;color:var(--txt2);white-space:nowrap}
#savebar .dot{width:7px;height:7px;border-radius:50%;background:var(--warn);flex:none}

/* ---------- 详情弹层 ---------- */
#modal{position:fixed;inset:0;z-index:95;display:none;align-items:center;justify-content:center;
padding:20px;background:rgba(10,12,18,.42);backdrop-filter:blur(3px)}
#modal.on{display:flex}
#modal .sheet{width:100%;max-width:620px;max-height:80vh;overflow:auto;border-radius:18px;
background:var(--surf);border:1px solid var(--line);box-shadow:0 30px 60px -20px rgba(0,0,0,.6);
padding:20px 22px}
#modal h4{margin:0 0 4px;font-size:0.9375rem}
#modal .sub{font-size:0.75rem;color:var(--txt3);margin-bottom:14px}
#modal pre{margin:0;font-size:0.7812rem;line-height:1.7;white-space:pre-wrap;word-break:break-word;
font-family:inherit;color:var(--txt2)}
tr.clk{cursor:pointer}
tr.clk:hover td{background:var(--surf2)}

@media(max-width:820px){
.notify-wrap{grid-template-columns:1fr}
.phone{position:static;max-width:300px;margin:0 auto}
.hero{flex-direction:column;align-items:flex-start;gap:16px}
.hero-r{width:100%}
.stat{flex:1;min-width:0}
}
@media(max-width:560px){
.pv-t{flex-wrap:wrap}
.pv-n{white-space:normal;overflow:visible}
.grid{grid-template-columns:minmax(0,1fr)}
.td-sum{white-space:normal;max-width:none}
th:nth-child(3),td:nth-child(3){min-width:180px}
.row,.row3{grid-template-columns:1fr}
.wrap{padding:0 14px}
.hd-in{padding:10px 14px}
.bname{font-size:0.875rem}
.bsub{display:none}
.btn .lbl{display:none}
.btn{padding:8px 11px}
.hstate{font-size:1.1875rem}
.grid{grid-template-columns:minmax(0,1fr)}
}

/* ---------- 可访问性：三态降级 ---------- */
/* 1) 减少动态：去掉无限循环与位移动画，保留状态色，反馈改为瞬时的显隐 */
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;
    transition-duration:.01ms!important;scroll-behavior:auto!important}
  .pulse::after{display:none}
  .hprog i{animation:none;left:0;width:100%;opacity:.45}
  .spin{animation:none;border-top-color:transparent;opacity:.55}
  .panel{animation:none}
  .pv:hover{transform:none}
}
/* 2) 减少透明：吸顶栏与通知卡改实底，去掉毛玻璃 */
@media (prefers-reduced-transparency: reduce){
  .hd{background:var(--bg);backdrop-filter:none;-webkit-backdrop-filter:none}
  .hd::after{display:none}
  .hd.scrolled{border-bottom-color:var(--line)}
  .notif{background:#fff;backdrop-filter:none}
}
/* 3) 增强对比：加深次级文字与描边 */
@media (prefers-contrast: more){
  :root{--txt2:#3a3a3c;--txt3:#4c4c51;--line:#c4c4c9;--line2:#d8d8dd}
  [data-theme=dark]{--txt2:#d1d1d6;--txt3:#a8a8ad;--line:#48484a;--line2:#3a3a3c}
  .pv,.hero,.stat,.scard,.card{border-color:var(--line)}
  .pill,.tag-flaky{border-width:1.5px}
}
</style></head><body>

<div class="hd"><div class="hd-in">
  <div class="brand">
    <img class="blogo" src="/icon-192.png" alt="" width="36" height="36">
    <div style="min-width:0">
      <div class="bname">Netwatch</div>
      <div class="bsub" id="bsub">加载中…</div>
    </div>
  </div>
  <div class="hd-act">
    <button class="btn ghost sm" onclick="toggleTheme()" title="切换深浅色" id="themeBtn">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/>
        <path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19"/></svg>
    </button>
    <button class="btn ghost sm" onclick="logout()"><span class="lbl">退出</span>
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M15 17l5-5-5-5M20 12H9M13 21H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7"/></svg>
    </button>
    <button class="btn sm" onclick="testPush(this)"><span class="lbl">测试推送</span>
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>
    </button>
    <button class="btn pri sm" id="runBtn" onclick="runNow()">
      <span class="spin" id="runSpin" style="display:none"></span>
      <span class="lbl">立即检测</span>
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 12a9 9 0 1 1-3.5-7.1"/><path d="M21 3v6h-6"/></svg>
    </button>
  </div>
</div></div>

<div class="wrap">
  <div class="pausebar" id="pauseBar" style="display:none">
    <span id="pauseTxt"></span>
    <button class="btn sm" onclick="setPause(0)">恢复巡检</button>
  </div>
  <section class="hero" id="hero">
    <div class="hprog" id="hprog"><i></i></div>
    <div class="hero-l">
      <div class="hstate"><span class="pulse none" id="pulse"></span><span id="hstateTxt">加载中…</span></div>
      <div class="hsum" id="hsum"></div>
      <div class="meta" id="hmeta"></div>
      <div class="phase" id="hphase" style="display:none;margin-top:7px"></div>
    </div>
    <div class="hero-r">
      <div class="stat ok"><div class="v" id="stOk">–</div><div class="k">正常网盘</div></div>
      <div class="stat bad"><div class="v" id="stBad">–</div><div class="k">需关注</div></div>
    </div>
  </section>

  <div class="statgrid">
    <div class="scard" title="按「网盘 × 轮次」加权，一个网盘挂了不会把整体打成 0%">
      <div class="k">30 天可用率</div><div class="v" id="sUp">–</div></div>
    <div class="scard"><div class="k">本周异常</div><div class="v" id="sWeek">–</div></div>
    <div class="scard" title="只统计真打上游的实测，缓存轮不计">
      <div class="k">7 天实测</div><div class="v" id="sProbe">–</div></div>
    <div class="scard" title="最近一次出现红色告警是几天前">
      <div class="k">连续全绿</div><div class="v" id="sStreak">–</div></div>
  </div>

  <div class="acts">
    <button class="btn sm" id="pBtn2" onclick="setPause(2)">暂停 2 小时</button>
    <button class="btn sm" id="pBtn8" onclick="setPause(8)">暂停 8 小时</button>
    <span class="sp"></span>
    <span class="hint" style="margin:0">暂停期间不巡检、不推送，心跳照常上报</span>
  </div>

  <div class="sec"><h2>各网盘凭证</h2><span class="hint" id="pvHint"></span></div>
  <div class="grid" id="pvs"><div class="card" style="grid-column:1/-1;color:var(--txt3)">暂无数据</div></div>
  <div class="skipnote" id="skipNote" style="display:none"></div>

  <div class="tabs">
    <div class="tab on" data-t="notify" onclick="switchTab('notify')">通知与推送</div>
    <div class="tab" data-t="src" onclick="switchTab('src')">监控范围</div>
    <div class="tab" data-t="conn" onclick="switchTab('conn')">连接与安全</div>
  </div>

  <!-- ---------- 通知 ---------- -->
  <div class="panel on" id="p-notify">
    <div class="card">
      <h3>Bark 推送</h3>
      <p class="d">iOS 上装 Bark App，把 App 首页的服务器地址和 Device Key 填进来即可。</p>
      <div class="notify-wrap">
        <div>
          <div class="row">
            <div class="f"><label>Bark 服务器地址</label>
              <input id="burl" placeholder="https://bark.example.com" oninput="renderPreview()"></div>
            <div class="f"><label>Device Key</label>
              <input id="bkey" placeholder="App 首页那串字符" oninput="renderPreview()"></div>
          </div>
          <div class="row3">
            <div class="f"><label>通知分组</label><input id="bgroup" oninput="renderPreview()"></div>
            <div class="f"><label>提示音</label>
              <select id="bsound" onchange="renderPreview()">
                <option value="minuet">minuet（默认）</option>
                <option value="alarm">alarm（闹钟）</option>
                <option value="bell">bell（铃）</option>
                <option value="electronic">electronic</option>
                <option value="glass">glass</option>
                <option value="birdsong">birdsong</option>
                <option value="none">静音</option>
              </select></div>
            <div class="f"><label>正常时级别</label>
              <select id="blevel">
                <option value="active">active（正常亮屏）</option>
                <option value="passive">passive（静默进通知中心）</option>
              </select></div>
          </div>
          <div class="f"><label>正常时的标题模板</label>
            <input id="btok" placeholder="✅ 网盘正常 {date} {time}" oninput="renderPreview()">
            <div class="hint">占位符：<code>{date}</code> 日期、<code>{time}</code> 时间、<code>{count}</code> 异常数。
              一天 3 条，建议带上 <code>{time}</code>，通知中心里一眼分清早中晚。</div></div>
          <div class="f"><label>异常时的标题模板</label>
            <input id="btbad" placeholder="⛔ 网盘凭证异常" oninput="renderPreview()">
            <div class="hint">可用 <code>{bad}</code> 自动列出出问题的网盘名，如「⛔ {bad} 凭证失效」</div></div>
          <div class="f"><label>异常时附带修复建议</label>
            <select id="bhint" onchange="renderPreview()">
              <option value="1">是（正文末尾加「建议：…」）</option>
              <option value="0">否（只发结果）</option>
            </select></div>
        </div>
        <div class="phone">
          <div class="bar-top"></div>
          <div class="notif">
            <div class="notif-t"><span id="pvGroup">NAS巡检</span>
              <span class="tm">现在</span></div>
            <h4 id="pvTitle">✅ 网盘正常 11-16 12:30</h4>
            <p id="pvBody">📦 115 ✅ 实测通过｜分享 206/206
⚛️ 夸克 ✅ 实测通过｜分享 42/65
🦆 光鸭 ✅ 实测通过
☁️ 阿里 ✅ 实测通过</p>
          </div>
          <div class="ph-hint">实时预览 · 改模板立即生效</div>
        </div>
      </div>
      <div style="margin-top:6px;display:flex;gap:9px;flex-wrap:wrap">
        <button class="btn pri" onclick="save(this)">保存设置</button>
        <button class="btn" onclick="testPush(this)">发一条测试</button>
        <button class="btn" onclick="switchTab('notify');previewBad()">预览异常样式</button>
      </div>
    </div>

    <div class="card">
      <h3>检测时间（每天 3 次）</h3>
      <p class="d">早、中、晚各巡检一次并推一条通知；保存后立即生效，不用重启容器。
        某一格留空就不跑那一次。</p>
      <div class="row3">
        <div class="f"><label>早上</label><input id="t0" type="time" step="60" oninput="markDirty();updateHbTip()"></div>
        <div class="f"><label>中午</label><input id="t1" type="time" step="60" oninput="markDirty();updateHbTip()"></div>
        <div class="f"><label>晚上</label><input id="t2" type="time" step="60" oninput="markDirty();updateHbTip()"></div>
      </div>
      <div class="hint">每轮之间会顺手核对 alist 记录，但真正向网盘发请求的「实测」
        仍是同一个网盘 8 小时才一次（见「监控范围」），一天两次实测、三次通知。</div>
    </div>

    <div class="card">
      <h3>推送策略</h3>
      <p class="d">决定什么时候真的吵你。默认每轮都发（一天 3 条），
        异常时换成红色强提醒。</p>
      <div class="row3">
        <div class="f"><label>推送时机</label>
          <select id="nok">
            <option value="always">每轮都发（每天 3 条）</option>
            <option value="change">只在状态变化时（含恢复）</option>
            <option value="fail">只在异常时（恢复也发一条）</option>
          </select></div>
        <div class="f"><label>连续异常几轮才告警</label>
          <select id="nthr">
            <option value="1">1 轮（立刻）</option>
            <option value="2">2 轮（滤掉抖动）</option>
            <option value="3">3 轮（最保守）</option>
          </select></div>
        <div class="f"><label>异常没恢复时</label>
          <select id="nrep">
            <option value="1">每轮继续提醒</option>
            <option value="0">只提醒一次</option>
          </select></div>
      </div>
      <div class="hint">「连续几轮」按巡检轮次算：早中晚各一轮，异常要连着 N 轮才算数。
        同一个问题只在第一次强提醒（响铃），之后几轮自动转为静默进通知中心，恢复时再正常提醒。</div>
    </div>

  </div>

  <!-- ---------- 监控范围 ---------- -->
  <div class="panel" id="p-src">
    <div class="card">
      <h3>实测频率（防封控）</h3>
      <p class="d">「实测」是向网盘真的发一次列表请求，用来确认凭证还能用。
        频率太高有被上游风控的风险，所以默认同一个网盘 8 小时才实测一次：
        一天 3 轮通知里，早上和晚上是真的打上游，中午那轮复用上次结论。</p>
      <div class="row3">
        <div class="f"><label>实时实测</label>
          <select id="pon"><option value="1">开启（推荐）</option>
          <option value="0">关闭，只看 alist 记录</option></select></div>
        <div class="f"><label>同一网盘间隔（小时）</label>
          <input id="pmin" type="number" min="0" max="168"></div>
        <div class="f"><label>网盘之间间隔（秒）</label>
          <input id="pgap" type="number" min="0" max="30"></div>
      </div>
      <div class="hint">间隔填 0 = 每轮都实测，不建议。按 8 小时算，
        一个网盘每天只挨两次请求，比 alist 自己启动时初始化几百个存储轻得多。</div>
    </div>
    <div class="card">
      <h3>监控哪些网盘</h3>
      <p class="d">关掉的网盘既不检测也不告警，也不会出现在推送正文里。点一下切换。<br>
        这里只列「填过账号」的网盘：纯分享链接挂进来的分组（比如 ThunderShare）
        没有账号可探，不纳入监测，也不会出现在下方列表里。</p>
      <div class="chips" id="toggles"><span style="color:var(--txt3)">加载中…</span></div>
      <div style="margin-top:16px"><button class="btn pri" onclick="save(this)">保存设置</button></div>
    </div>
    <div class="card">
      <h3>参与统计的存储</h3>
      <p class="d">按驱动自动识别，个人盘与分享盘分开统计。禁用状态的存储不计入。</p>
      <div class="tbl"><table><thead><tr><th>网盘</th><th>个人盘</th><th>分享盘</th><th>状态</th></tr></thead>
        <tbody id="srcTable"><tr><td colspan="4" style="color:var(--txt3)">暂无</td></tr></tbody></table></div>
    </div>
  </div>

  <!-- ---------- 连接 ---------- -->
  <div class="panel" id="p-conn">
    <div class="card">
      <h3>连接地址</h3>
      <p class="d">容器内访问 NAS 宿主机用 host.docker.internal。</p>
      <div class="row">
        <div class="f"><label>alist 地址</label><input id="alist"></div>
        <div class="f"><label>G-Box 后台地址（可选）</label><input id="gbox" placeholder="留空则不检查 token 链路"></div>
      </div>
      <div class="row">
        <div class="f"><label>G-Box 账号</label><input id="gu"></div>
        <div class="f"><label>G-Box 密码</label><input id="gp" type="password" placeholder="留空不修改"></div>
      </div>
      <div class="f"><label>alist 数据库路径（容器内）</label><input id="adb">
        <div class="hint">只读挂载，程序不会写入</div></div>
      <div style="margin-top:4px"><button class="btn pri" onclick="save(this)">保存设置</button></div>
    </div>
    <div class="card">
      <h3>外部心跳（可选，但建议配）</h3>
      <p class="d">每轮巡检结束后向这个地址发一个 GET。配了它，NAS 断电、容器崩了、
        程序本身挂了，外部也能知道——Netwatch 自己是没法给自己报丧的。</p>
      <div class="f"><label>心跳地址</label>
        <input id="hburl" placeholder="https://hc-ping.com/xxxx 或 Uptime Kuma 的 Push 地址">
        <div class="hint">推荐 healthchecks.io（免费）或 Uptime Kuma 的 Push Monitor：
          建好后把它的 ping 地址填进来。巡检正常跑完 = 上报成功；程序崩溃会额外请求「地址/fail」。留空关闭。</div>
        <div class="hint" id="hbTip" style="color:var(--warn)"></div></div>
      <div style="margin-top:4px"><button class="btn pri" onclick="save(this)">保存设置</button></div>
    </div>
    <div class="card">
      <h3>配置备份</h3>
      <p class="d">换 NAS 或重建容器时一键迁移。导出文件含 Bark Key、G-Box 密码等敏感信息，自己保管好。</p>
      <div style="display:flex;gap:9px;flex-wrap:wrap;align-items:center">
        <button class="btn" onclick="exportConf()">导出配置</button>
        <button class="btn" onclick="$('impfile').click()">导入配置</button>
        <input type="file" id="impfile" accept=".json,application/json" style="display:none" onchange="importConf(this)">
        <span class="hint" style="margin:0">导入会覆盖当前全部设置（含密码），导入后立即生效</span>
      </div>
    </div>
    <div class="card">
      <h3>访问密码</h3>
      <p class="d">修改后当前会话立即失效，需要用新密码重新登录。</p>
      <div class="row">
        <div class="f"><label>新密码</label><input id="pw" type="password" placeholder="留空不修改" autocomplete="new-password"></div>
        <div class="f"><label>再输一次</label><input id="pw2" type="password" placeholder="确认新密码" autocomplete="new-password"></div>
      </div>
      <button class="btn pri" onclick="savePw(this)">修改密码</button>
    </div>
  </div>

  <div class="sec" style="margin-top:24px"><h2>故障事件</h2>
    <span class="hint" id="incHint">近 30 天 · 连续异常合并为一次事件</span></div>
  <div class="card" style="padding:14px 18px" id="incCard">
    <div id="incBar"></div>
    <div id="incList"></div>
  </div>

  <div class="sec" style="margin-top:24px"><h2>检测历史</h2><span class="hint">近 7 天每轮一格 · 下面是最近 30 次</span></div>
  <div class="card" style="padding:12px 18px;margin-bottom:12px" id="histHeat"></div>
  <div class="card" style="padding:6px 4px">
    <div class="tbl"><table><thead><tr><th style="width:150px">时间</th><th style="width:90px">结果</th><th>摘要</th></tr></thead>
      <tbody id="hist"><tr><td colspan="3" style="color:var(--txt3);padding:16px 10px">暂无</td></tr></tbody></table></div>
  </div>

  <div class="foot-note">Netwatch · alist-watch v3 · 数据只存在本机 <span id="ver"></span></div>
</div>

<div id="toast"></div>
<div id="savebar"><span class="dot"></span><span class="t">有改动还没保存</span>
  <button class="btn pri sm" onclick="save(this)">保存</button>
  <button class="btn ghost sm" onclick="reloadConf()">放弃</button>
</div>
<div id="modal" onclick="if(event.target===this)closeModal()">
  <div class="sheet">
    <h4 id="mTitle">检测详情</h4>
    <div class="sub" id="mSub"></div>
    <pre id="mBody"></pre>
    <div style="margin-top:16px;text-align:right">
      <button class="btn sm" onclick="closeModal()">关闭</button></div>
  </div>
</div>
<script>
var S={conf:{},last:null,provs:[],nextTs:0,dirty:false,poll:null,stats:null};
var PM=__PMETA__;
function $(id){return document.getElementById(id)}
function t(m){var e=$('toast');e.textContent=m;e.classList.add('on');
  clearTimeout(e._h);e._h=setTimeout(function(){e.classList.remove('on')},2400)}
function esc(s){return String(s==null?'':s).replace(/[<>&"]/g,function(c){
  return {'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]})}
function pcolor(g){var m=PM[g];return m?m.color:'#6b7280'}
function pslug(g){var m=PM[g];return m?m.slug:'115'}
function pbadge(g,cls){return '<img class="pvb'+(cls?' '+cls:'')+'" src="/logo/'+
  pslug(g)+'.png" alt="'+esc(g)+'" loading="lazy">'}
function pshort(g){var m=PM[g];return m?m.short:String(g||'?').slice(0,1)}
function pemoji(g){var m=PM[g];return m?m.emoji:'☁️'}

/* 空/加载/出错三种形态统一成一套占位，别处不再各写一份灰字 */
function emptyBox(kind,msg){
  var d={empty:'M4 7h16M4 7v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7M4 7l2-3h12l2 3M9 12h6',
         load:'M12 3v4M12 17v4M3 12h4M17 12h4',
         err:'M12 8v5M12 16.5v.5M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18'}[kind]||'';
  var spin=kind==='load'?'<animateTransform attributeName="transform" type="rotate" '+
    'from="0 12 12" to="360 12 12" dur="2.6s" repeatCount="indefinite"/>':'';
  return '<div class="empty'+(kind==='err'?' err':'')+'"><svg width="32" height="32" '+
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" '+
    'stroke-linecap="round" stroke-linejoin="round"><g><path d="'+d+'"/>'+spin+
    '</g></svg><div>'+esc(msg)+'</div></div>';
}

/* ---------- 主题 ---------- */
function applyTheme(th){document.documentElement.setAttribute('data-theme',th);
  try{localStorage.setItem('aw-theme',th)}catch(e){}
  var mt=document.querySelectorAll('meta[name=theme-color]');
  for(var i=0;i<mt.length;i++){
    var media=mt[i].getAttribute('media')||'';
    if(media.indexOf('dark')<0){mt[i].setAttribute('content',th==='dark'?'#0d0d0f':'#f5f5f7')}
  }}
function toggleTheme(){
  var cur=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
  applyTheme(cur);
}
(function(){var s='light';try{s=localStorage.getItem('aw-theme')||'light'}catch(e){}
  applyTheme(s)})();

/* ---------- 未保存保护 ---------- */
function markDirty(){if(S.dirty)return;S.dirty=true;$('savebar').classList.add('on')}
function clearDirty(){S.dirty=false;$('savebar').classList.remove('on')}
function editable(el){return el&&(el.tagName==='INPUT'||el.tagName==='SELECT')}
document.addEventListener('input',function(e){
  var id=e.target&&e.target.id;
  if(id==='pw'||id==='pw2'||id==='gp')return;
  markDirty();
});
document.addEventListener('change',function(e){
  var id=e.target&&e.target.id;
  if(id==='pw'||id==='pw2'||id==='gp')return;
  markDirty();
});
window.addEventListener('beforeunload',function(e){
  if(S.dirty){e.preventDefault();e.returnValue='';return ''}
});

function switchTab(n){
  document.querySelectorAll('.tab').forEach(function(e){e.classList.toggle('on',e.dataset.t===n)});
  document.querySelectorAll('.panel').forEach(function(e){e.classList.toggle('on',e.id==='p-'+n)});
  try{history.replaceState(null,'','?tab='+n)}catch(e){}
}

/* ---------- 倒计时 ---------- */
function tick(){
  var el=$('hcd'); if(!el)return;
  if(!S.nextTs){el.textContent='';return}
  var d=Math.round(S.nextTs-Date.now()/1000);
  if(d<=0){el.textContent='马上开始';return}
  var h=Math.floor(d/3600),m=Math.floor(d%3600/60),s=d%60;
  el.textContent = h?(h+' 小时 '+m+' 分后'):(m?(m+' 分 '+s+' 秒后'):'不到 1 分钟');
}
setInterval(tick,1000);

/* ---------- 通知预览 ---------- */
function fmtTpl(tpl,verdict,badList){
  var d=new Date();
  var p2=function(n){return n<10?'0'+n:''+n};
  var subs={date:p2(d.getMonth()+1)+'-'+p2(d.getDate()),
    time:p2(d.getHours())+':'+p2(d.getMinutes()),
    verdict:verdict||'OK',
    count:String((badList||[]).length),
    bad:(badList&&badList.length)?badList.join('、'):'网盘'};
  return (tpl||'').replace(/\{(date|time|verdict|count|bad)\}/g,function(_,k){return subs[k]});
}
function previewBad(){
  var lines=(S.last&&S.last.lines)||[];
  var fake=lines.slice(0,6);
  var first=S.provs.length?S.provs[0].group:'115';
  fake[0]=pemoji(first)+' '+first+' ❌ cookie 已失效';
  renderPreview('FAIL',fake.slice(0,6),[first]);
}
function renderPreview(verdictOverride,linesOverride,badOverride){
  var g=($('bgroup').value||'NAS巡检').trim();
  $('pvGroup').textContent=g;
  var ver=verdictOverride||(S.last&&S.last.verdict)||'OK';
  var lines=linesOverride||(S.last&&S.last.lines)||
    ['📦 115 ✅ 实测通过｜分享 206/206','⚛️ 夸克 ✅ 实测通过｜分享 42/65',
     '🦆 光鸭 ✅ 实测通过','☁️ 阿里 ✅ 实测通过'];
  var seen={},keep=[];
  lines.forEach(function(l){
    var s=String(l).trim();
    if(/^(注意|问题|建议)[：:]/.test(s))return;
    var nm=null;
    for(var i=0;i<S.provs.length;i++){
      if(s.indexOf(S.provs[i].group)>=0){nm=S.provs[i].group;break}
    }
    if(nm){ if(seen[nm])return; seen[nm]=1 }
    keep.push(s);
  });
  lines=keep.slice(0,6);
  var bads=badOverride||S.provs.filter(function(p){return p.status==='bad'}).map(function(p){return p.group});
  var tpl=ver==='OK'?($('btok').value||'✅ 网盘正常 {date} {time}'):($('btbad').value||'⛔ {bad} 需要处理');
  $('pvTitle').textContent = ver==='WARN' ? '⚠️ 巡检降级（结论不可信）' : fmtTpl(tpl,ver,bads);
  var body=lines.join('\n');
  if(ver!=='OK' && $('bhint').value==='1'){
    var pri=bads.length?'需重新授权/重填 cookie':'';
    body+='\n\n建议：'+pri;
  }
  $('pvBody').textContent=body;
}
/* ---------- 加载 ---------- */
async function load(){
  var r;
  try{ r=await fetch('/api/state').then(function(x){
      if(x.status===401){location.reload();return null}
      return x.json()}); }catch(e){ return }
  if(!r) return;
  S.conf=r.config||{}; S.last=r.last; S.nextTs=r.next_run_ts||0;
  S.provs=(r.detail_providers||[]);
  renderHero(r); renderProvs(r.detail_providers||[]); renderToggles(r.detail_providers||[]);
  renderSkip(r.skipped_groups||[]);
  if(!S.dirty && !editable(document.activeElement)) fillForms(S.conf);
  renderPreview();
  fetch('/api/stats').then(function(x){return x.json()}).then(function(st){
    S.stats=st; renderStats(st);
    renderProvs(r.detail_providers||[]);
    renderHistHeat(st.recent||[]);
    renderIncidents();
    renderHero(r);                 // 统计到位后重算「最近一次异常在几天前」
  }).catch(function(){});
  setRunUI(!!r.running, r.phase||'');
  tick();
  var h=await fetch('/api/history').then(function(x){return x.json()});
  var hb=$('hist');
  hb.innerHTML=(h.items||[]).map(function(i){
    var cls=i.verdict==='OK'?'ok':(i.verdict==='FAIL'?'bad':'warn');
    return '<tr class="clk" onclick="showHist('+i.id+')">'+
      '<td style="color:var(--txt2)">'+esc(i.ts)+'</td>'+
      '<td><span class="pill '+cls+'">'+esc(i.verdict)+'</span></td>'+
      '<td class="td-sum">'+esc(String(i.summary||'').slice(0,110))+'</td></tr>';
  }).join('')||'<tr><td colspan="3" style="color:var(--txt3);padding:16px 10px">暂无记录</td></tr>';
}

function setRunUI(running,phase){
  $('runSpin').style.display = running?'':'none';
  $('runBtn').disabled = running;
  var pg=$('hprog'); if(pg) pg.style.display = running?'block':'none';
  var ph=$('hphase');
  if(running){ ph.style.display=''; ph.textContent='正在检测'+(phase?(' · '+phase):'')+'…' }
  else ph.style.display='none';
}

function renderHero(r){
  var last=r.last, provs=(r.detail_providers||[]).filter(function(p){return p.status!=='off'});
  var ok=provs.filter(function(p){return p.status==='ok'}).length;
  var bad=provs.filter(function(p){return p.status==='bad'}).length;
  var warn=provs.filter(function(p){return p.status==='warn'}).length;
  $('stOk').textContent=ok; $('stBad').textContent=bad+warn;
  $('stOk').parentNode.classList.toggle('bad', !ok);
  var hero=$('hero'), pulse=$('pulse'), txt=$('hstateTxt');
  hero.className='hero';
  if(r.running){ pulse.className='pulse warn'; txt.textContent='正在检测…'; }
  else if(!last){ pulse.className='pulse none'; txt.textContent='正在首次检测…';
    $('bsub').textContent='等待首次结果'; }
  else if(bad){ hero.classList.add('s-bad'); pulse.className='pulse bad';
    txt.textContent=bad+' 个网盘凭证失效'; }
  else if(warn){ hero.classList.add('s-warn'); pulse.className='pulse warn';
    txt.textContent='检测降级，结论需人工确认'; }
  else if(last.verdict==='OK'){ pulse.className='pulse';
    txt.textContent=(provs.length||'全部')+' 个网盘全部正常'; }
  else { pulse.className='pulse warn'; txt.textContent='结果：'+last.verdict; }
  $('bsub').textContent=last?('上次 '+last.ts.slice(5,16)):'等待首次结果';

  var pt=last?last.push:'';
  var pushTxt=pt==='ok'?'成功':(pt==='skip'?'未推送':(pt?'失败：'+pt:'未推送'));
  var m=[];
  if(last) m.push('<span class="mi">上次 <b>'+esc(last.ts.slice(5,16))+'</b></span>');
  if(r.schedule_text) m.push('<span class="mi">每天 <b>'+esc(r.schedule_text)+'</b></span>');
  if(r.next_run) m.push('<span class="mi">下次 <b>'+esc(r.next_run)+'</b> <span id="hcd" style="color:var(--txt3)"></span></span>');
  m.push('<span class="mi">推送 <b>'+esc(pushTxt)+'</b></span>');
  $('hmeta').innerHTML=m.join('');
  $('pvHint').textContent=last?('实测于 '+last.ts.slice(5,16)):'';
  if(r.error){ m=null; $('hmeta').innerHTML+='<span style="color:var(--bad)">· 上次执行出错：'+esc(r.error)+'</span>' }

  renderPause(r);
  $('hsum').textContent=heroSum(r,provs,bad,warn);
}

/* 一句人话总结：比罗列碎指标更快看懂「现在该做什么」 */
function heroSum(r,provs,bad,warn){
  if((r.pause_left||0)>0)
    return '巡检已暂停，暂停期间不会报警；心跳仍在上报，外部监控不会误判宕机。';
  if(r.running) return '正在逐个核对网盘凭证，完成后自动刷新。';
  if(!r.last) return '还没有巡检结果，点右上角「立即巡检」跑一次。';
  if(bad){
    var names=provs.filter(function(p){return p.status==='bad'}).map(function(p){return p.group});
    return names.join('、')+' 需要处理：卡片上点「重测」可单独验证，点「去修复」直达 alist 存储管理。';
  }
  if(warn) return '有网盘的分享目录全部失效，但个人盘正常——多为上游取消分享，不一定要改凭证。';
  var st=S.stats||{}, days=(st.incidents||{}).days||[], lastBad='';
  for(var i=days.length-1;i>=0;i--){ if(days[i].v==='FAIL'){ lastBad=days[i].d; break } }
  var ago='';
  if(lastBad){
    var now=new Date(), d0=new Date(lastBad+'T00:00:00');
    var n=Math.round((new Date(now.getFullYear(),now.getMonth(),now.getDate())-d0)/86400000);
    ago = n<=0?'今天':(n===1?'昨天':n+' 天前');
  }
  var first=((st.incidents||{}).items||[])[0]||null;
  var who=(first&&first.groups&&first.groups.length)?first.groups.join('、'):'';
  if(ago){
    return '最近一次异常在 '+ago+(who?('（'+who+'，已恢复）'):'')+'，其余巡检全部正常'+
      (st.streak?('，已连续 '+st.streak+' 天全绿'):'')+'。';
  }
  return '近 30 天没有出现过故障'+(st.streak?('，已连续 '+st.streak+' 天全绿'):'')+'。';
}

function renderProvs(provs){
  var box=$('pvs');
  var list=provs.filter(function(p){return p.status!=='off'});
  if(!list.length){
    box.innerHTML = S.last
      ? emptyBox('empty','所有网盘都已在设置里关闭监控')
      : emptyBox('load','正在读取 alist 存储列表…');
    return }
  var flaky=(S.stats&&S.stats.flaky)||{};
  box.innerHTML=list.map(function(p){
    var st=p.status==='ok'?'ok':(p.status==='bad'?'bad':(p.status==='off'?'off':'warn'));
    var label={'ok':'正常','bad':'失效','warn':'降级','off':'已关闭'}[p.status]||p.status;
    var per=p.personal||0, badP=p.bad_personal||0, sw=p.share_work||0, st2=p.share_total||0;
    var lines=[];
    if(per) lines.push('<div class="r"><span>个人盘</span><b>'+per+' 个'+
      (badP?' <span style="color:var(--bad)">· '+badP+' 异常</span>':'')+'</b></div>');
    if(st2) lines.push('<div class="r"><span>分享盘</span><b>'+sw+' / '+st2+'</b></div>');
    else lines.push('<div class="r"><span>分享盘</span><b style="color:var(--txt3)">—</b></div>');
    if(!per&&!st2) lines.push('<div class="r"><span>存储</span><b>—</b></div>');
    var pL={ok:'通过',bad:'异常',unknown:'未知',cached:'缓存',off:'关闭'}[p.probe];
    if(pL && p.cached && p.probe!=='cached') pL+=' · 缓存';
    var pC=p.probe==='ok'?'var(--ok)':(p.probe==='bad'?'var(--bad)':
      (p.probe==='cached'||p.probe==='off'?'var(--txt3)':'var(--warn)'));
    var probe=pL?('<div class="r"><span>实测</span><b title="'+esc(p.probe_info||'')+
      '" style="color:'+pC+'">'+pL+'</b></div>'):'';
    var fl=flaky[p.group];
    var flakyTag=fl?(' <span class="tag-flaky" title="近 7 天 '+fl.tot+' 轮里失败 '+fl.fail+
      ' 轮，中途恢复过 '+fl.recover+' 次">近期不稳</span>'):'';
    var detail=p.detail?('<div class="pv-d" title="'+esc(p.detail)+'">'+esc(p.detail)+'</div>'):'';
    if(!detail && st2 && sw===0) detail='<div class="pv-d warn">分享全部失效（上游取消）</div>';
    if(fl && !detail) detail='<div class="pv-d warn">近 7 天失效 '+fl.fail+
      ' 次，建议重新登录</div>';
    var strip=((S.stats||{}).strips||{})[p.group];
    var stripHtml='';
    if(strip&&strip.length){
      // 已经恢复的盘，热力条里仍有红格——说清楚那是「当时」失败，别让人以为现在还坏着
      var cap = (S.stats.bad_since&&S.stats.bad_since[p.group])
        ? ('自 '+esc(S.stats.bad_since[p.group])+' 起持续失效')
        : ((strip.indexOf('bad')>=0 && st==='ok')
            ? '近 7 天 · 红格是当时的失败，现已恢复'
            : '近 7 天 · 每格一轮巡检');
      stripHtml='<div class="heat">'+strip.map(function(x){
        var c=x==='ok'?'var(--ok)':(x==='bad'?'var(--bad)':
          (x==='warn'?'var(--warn)':'var(--line)'));
        return '<i style="background:'+c+'"></i>';}).join('')+'</div>'+
        '<div class="heat-cap">'+cap+'</div>';
    }
    var fix=(st==='bad')?('<a class="fix" target="_blank" rel="noopener" href="'+
      esc((S.conf.alist_base||'').replace(/\/+$/,''))+'/@manage/storage">去修复 ↗</a>'):'';
    return '<div class="pv st-'+st+'" style="--c:'+pcolor(p.group)+'">'+
      '<div class="pv-t">'+pbadge(p.group,'lg')+
        '<span class="pv-n">'+esc(p.group)+'</span>'+flakyTag+
        '<button class="rd" title="只测这个网盘，不写历史也不发通知" '+
        'onclick="reprobe(this,\''+esc(p.group)+'\')">重测</button>'+fix+
      '<span class="pill '+st+'">'+label+'</span></div>'+
      '<div class="pv-m">'+lines.join('')+probe+'</div>'+detail+stripHtml+'</div>';
  }).join('');
}

function renderStats(st){
  if(!st){return}
  if(!st.recent||!st.recent.length){
    $('sUp').textContent='–'; $('sWeek').textContent='–';
    $('sProbe').textContent='–'; $('sStreak').textContent='–'; return;
  }
  $('sUp').innerHTML=(st.uptime30==null?'–':st.uptime30+'<small>%</small>')+
    (st.total30?'<small> · '+st.total30+' 轮</small>':'');
  $('sWeek').innerHTML=st.week_bad+(st.week_total?'<small> / '+st.week_total+' 轮</small>':'<small> 轮</small>');
  $('sProbe').textContent=st.probe_tot?(st.probe_ok+' / '+st.probe_tot):'–';
  $('sStreak').innerHTML=st.streak+'<small> 天</small>';
}
function renderHistHeat(recent){
  var el=$('histHeat'); if(!el)return;
  if(!recent||!recent.length){el.innerHTML=emptyBox('empty','还没有巡检记录');return}
  var days=[],cur=null;
  recent.forEach(function(r){
    var d=String(r.ts).slice(5,10);
    if(!cur||cur.d!==d){cur={d:d,items:[]};days.push(cur)}
    cur.items.push(r.verdict);
  });
  el.innerHTML=days.map(function(dd){
    return '<div class="hday"><span class="d">'+esc(dd.d)+'</span><span class="c">'+
      dd.items.map(function(v){
        var c=v==='OK'?'ok':(v==='FAIL'?'bad':'warn');
        return '<i class="'+c+'" title="'+esc(v)+'"></i>';
      }).join('')+'</span></div>';
  }).join('');
}
/* ---------- 故障事件 ---------- */
function renderIncidents(){
  var inc=(S.stats&&S.stats.incidents)||{items:[],days:[]};
  var bar=$('incBar'), list=$('incList');
  if(!bar||!list) return;
  var days=inc.days||[];
  if(!days.length){
    bar.innerHTML=''; list.innerHTML=emptyBox('empty','近 30 天还没有巡检记录');
    $('incHint').textContent='近 30 天 · 连续异常合并为一次事件';
    return;
  }
  var bars=days.map(function(d){
    var c=d.v==='FAIL'?'bad':(d.v==='WARN'?'warn':(d.v===''?'':'ok'));
    var tip=d.d+(d.n?(' · '+d.n+' 轮 · '+
      (d.v==='FAIL'?'有异常':(d.v==='WARN'?'降级':'正常'))):' · 无记录');
    return '<i class="'+c+'" title="'+esc(tip)+'"></i>';
  }).join('');
  bar.innerHTML='<div class="dbar">'+bars+'</div><div class="dbar-x"><span>'+
    esc(days[0].d.slice(5))+'</span><span style="opacity:.7">浅灰 = 当天没有巡检记录</span>'+
    '<span>今天</span></div>';

  var items=inc.items||[];
  $('incHint').textContent='近 30 天 · 共 '+inc.total+' 次';
  if(!items.length){
    list.innerHTML=emptyBox('empty','近 30 天没有故障事件，一直平稳');
    return;
  }
  list.innerHTML=items.map(function(it){
    var cls=it.kind==='FAIL'?'':' warn';
    var st=it.ongoing?'<span class="pill bad">进行中</span>':('<span class="pill '+
      (it.kind==='FAIL'?'mute':'warn')+'">已恢复</span>');
    var gs=(it.groups&&it.groups.length)?it.groups.join('、'):'整体原因';
    var gsTitle=(it.groups&&it.groups.length)?''
      :'不只是某个网盘的问题，多半出在 alist 未就绪或 data.db 读取上';
    var when=esc(it.start.slice(5,16))+(it.end?(' → '+esc(it.end.slice(5,16))):' → 现在');
    // 单轮异常若显示「持续 13 小时」会夸大：那只是两次巡检之间的空档，不是真故障了 13 小时
    var durTxt=it.rounds<=1?'单轮异常，下一轮已恢复'
      :('持续 '+it.dur+' · 连续 '+it.rounds+' 轮异常');
    return '<div class="inc'+cls+'"><span class="dot"></span><div class="inc-b">'+
      '<div class="inc-t"><span title="'+esc(gsTitle)+'">'+esc(gs)+'</span>'+st+'</div>'+
      '<div class="inc-m">'+when+' · '+esc(durTxt)+'</div>'+
      '</div></div>';
  }).join('');
  if(inc.total>items.length){
    list.innerHTML+='<div class="inc-m" style="padding-top:9px;color:var(--txt3)">另有 '+
      (inc.total-items.length)+' 次更早的事件未显示</div>';
  }
}

/* ---------- 暂停 ---------- */
function renderPause(r){
  var left=r.pause_left||0, bar=$('pauseBar');
  if(!bar) return;
  if(left>0){
    var mm=Math.floor(left/60), hh=Math.floor(mm/60), m2=mm%60;
    $('pauseTxt').textContent='巡检已暂停，还剩 '+(hh?hh+' 小时 ':'')+m2+
      ' 分钟。「立即巡检」仍可用，心跳也照常上报。';
    bar.style.display='flex';
    $('pBtn2').style.display='none'; $('pBtn8').style.display='none';
  }else{
    bar.style.display='none';
    $('pBtn2').style.display=''; $('pBtn8').style.display='';
  }
}
async function setPause(h){
  try{
    await fetch('/api/pause',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({hours:h})}).then(function(x){return x.json()});
    t(h>0?('已暂停 '+h+' 小时'):'已恢复巡检');
    await load();
  }catch(e){ t('操作失败：'+e.message) }
}

/* ---------- 单盘重测 ---------- */
async function reprobe(btn,g){
  var old=btn.textContent;
  btn.disabled=true; btn.textContent='测试中…';
  var msg='';
  try{
    var r=await fetch('/api/probe',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({group:g})}).then(function(x){return x.json()});
    if(r.ok) msg=g+' 实测通过';
    else if(r.status==='bad') msg=g+' 实测未通过：'+(r.info||'');
    else msg=g+' '+(r.msg||r.info||'结果未知，请手工确认');
  }catch(e){ msg='重测失败：'+e.message }
  btn.disabled=false; btn.textContent=old;
  t(msg);
  await load();
}

function renderSkip(list){
  var el=$('skipNote'); if(!el) return;
  var sk=list||[];
  if(!sk.length){ el.style.display='none'; el.innerHTML=''; return }
  el.style.display='';
  el.innerHTML='未纳入监控：<b>'+sk.map(function(g){return esc(g)}).join('、')+
    '</b> —— 只是挂进来的分享链接，没有填账号，不探测也不推送。';
}

function renderToggles(provs){
  var tg=$('toggles');
  if(!provs.length){tg.innerHTML='<span style="color:var(--txt3)">暂无</span>';return}
  tg.innerHTML=provs.map(function(p){
    return '<div class="chip '+(p.status==='off'?'off':'')+'" data-g="'+esc(p.group)+
      '" onclick="this.classList.toggle(\'off\');markDirty()">'+
      '<span class="sw"></span>'+pbadge(p.group)+esc(p.group)+'</div>';
  }).join('');
  var tb=$('srcTable');
  tb.innerHTML=provs.map(function(p){
    var cls=p.status==='ok'?'ok':(p.status==='bad'?'bad':(p.status==='off'?'mute':'warn'));
    var label={'ok':'正常','bad':'失效','warn':'降级','off':'已关闭'}[p.status]||p.status;
    return '<tr><td>'+pbadge(p.group,'sm')+
      '<b style="margin-left:8px">'+esc(p.group)+'</b></td>'+
      '<td>'+(p.personal||0)+'</td><td>'+(p.share_work||0)+' / '+(p.share_total||0)+'</td>'+
      '<td><span class="pill '+cls+'">'+label+'</span></td></tr>';
  }).join('')||'<tr><td colspan="4" style="color:var(--txt3)">暂无</td></tr>';
}

/* 后端可能给 times 数组，也可能是老配置的 hour/minute */
function scheduleList(c){
  var sc=(c&&c.schedule)||{}, out=[];
  if(sc.times&&sc.times.length){
    sc.times.forEach(function(t){
      var m=/^(\d{1,2}):(\d{1,2})$/.exec(String(t).trim());
      if(m) out.push(('0'+m[1]).slice(-2)+':'+('0'+m[2]).slice(-2));
    });
  }
  if(!out.length){
    var h=sc.hour!=null?+sc.hour:8, mi=sc.minute!=null?+sc.minute:30;
    out.push(('0'+h).slice(-2)+':'+('0'+mi).slice(-2));
  }
  return out.sort();
}

/* 按当前早/中/晚时刻算最长空档，推出心跳超时该设多少 */
function hbTipText(raw){
  var mins=[];
  (raw||[]).forEach(function(t){
    var m=/^(\d{1,2}):(\d{2})$/.exec(String(t||'').trim());
    if(m) mins.push((+m[1])*60+(+m[2]));
  });
  mins=mins.sort(function(a,b){return a-b});
  function hhmm(v){
    v=((v%1440)+1440)%1440;
    return ('0'+Math.floor(v/60)).slice(-2)+':'+('0'+(v%60)).slice(-2);
  }
  if(mins.length<2){
    return '只有一轮检测时，两次打卡间隔可能长达 24 小时，超时请设 26 小时以上，'+
      '否则会天天误报。';
  }
  var gap=0, from=0, to=0;
  for(var i=0;i<mins.length;i++){
    var cur=mins[i], nxt=(i+1<mins.length)?mins[i+1]:mins[0]+1440, g=nxt-cur;
    if(g>gap){ gap=g; from=cur; to=nxt; }
  }
  var hours=Math.round(gap/60*10)/10;
  return '当前时刻表最长空档 '+hours+' 小时（'+hhmm(from)+' → '+(to>=1440?'次日 ':'')+hhmm(to)+'），'+
    '超时请设 '+Math.ceil(gap/60+2)+' 小时以上，留 2 小时余量；设小了每天凌晨都会误报「心跳丢失」。';
}
function updateHbTip(){
  var el=$('hbTip'); if(!el) return;
  el.textContent=hbTipText([$('t0').value,$('t1').value,$('t2').value]);
}

function fillForms(c){
  var b=c.bark||{};
  if(!document.activeElement||document.activeElement.tagName!=='INPUT'){
    $('burl').value=b.url||''; $('bkey').value=b.key||'';
  }
  $('bgroup').value=b.group||'NAS巡检';
  var snd=b.sound||'minuet';
  var opt=Array.prototype.slice.call($('bsound').options).map(function(o){return o.value});
  $('bsound').value=opt.indexOf(snd)>=0?snd:'minuet';
  $('blevel').value=b.level_ok||'active';
  $('btok').value=b.title_ok||'✅ 网盘正常 {date} {time}';
  $('btbad').value=b.title_bad||'⛔ {bad} 需要处理';
  $('bhint').value=(b.with_hint===false?'0':'1');
  var pb=c.probe||{};
  $('pon').value=(pb.enabled===false?'0':'1');
  $('pmin').value=(pb.min_interval_h!=null?pb.min_interval_h:8);
  $('pgap').value=(pb.gap_s!=null?pb.gap_s:1.2);
  var ts=scheduleList(c);
  $('t0').value=ts[0]||''; $('t1').value=ts[1]||''; $('t2').value=ts[2]||'';
  updateHbTip();
  var n=c.notify||{};
  $('nok').value=n.policy||'always';
  $('nthr').value=String(n.fail_threshold||1);
  $('nrep').value=(n.repeat_bad===false?'0':'1');
    $('hburl').value=((c.heartbeat||{}).url)||'';
  $('alist').value=c.alist_base||''; $('gbox').value=c.gbox_base||'';
  $('gu').value=c.gbox_user||''; $('adb').value=c.alist_db||'';
}

function collect(){
  return {
    schedule:{times:[$('t0').value,$('t1').value,$('t2').value]
      .filter(function(x){return /^\d{1,2}:\d{2}$/.test(x)})},
    notify:{policy:$('nok').value,fail_threshold:+$('nthr').value,
      repeat_bad:$('nrep').value==='1'},
    bark:{url:$('burl').value.trim(),key:$('bkey').value.trim(),
      group:$('bgroup').value.trim()||'NAS巡检',sound:$('bsound').value,
      level_ok:$('blevel').value,
      title_ok:$('btok').value.trim(),title_bad:$('btbad').value.trim(),
      with_hint:$('bhint').value==='1'},
    probe:{enabled:$('pon').value==='1',min_interval_h:+$('pmin').value,
      gap_s:+$('pgap').value},
    heartbeat:{url:$('hburl').value.trim()},
    alist_base:$('alist').value.trim(),gbox_base:$('gbox').value.trim(),
    gbox_user:$('gu').value.trim(),alist_db:$('adb').value.trim(),
    disabled:Array.prototype.slice.call(document.querySelectorAll('#toggles .chip.off'))
      .map(function(e){return e.dataset.g})
  };
}

async function save(btn){
  var body=collect();
  if(!body.schedule.times.length){ t('早中晚至少要填一个时间'); return }
  var gp=$('gp').value; if(gp) body.gbox_pass=gp;
  if(btn) btn.disabled=true;
  try{
    var r=await fetch('/api/save',{method:'POST',body:JSON.stringify(body)}).then(function(x){return x.json()});
    if(r.ok){ clearDirty(); t('已保存'); $('gp').value=''; await load(); }
    else t('保存失败');
  }catch(e){t('保存失败：'+e.message)}
  if(btn) btn.disabled=false;
}

async function reloadConf(){ clearDirty(); await load(); t('已放弃改动') }

function download(name,text){
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([text],{type:'application/json'}));
  a.download=name; document.body.appendChild(a); a.click();
  setTimeout(function(){URL.revokeObjectURL(a.href);a.remove()},400);
}
async function exportConf(){
  try{
    var r=await fetch('/api/export').then(function(x){return x.json()});
    if(!r.ok){t('导出失败');return}
    download('netwatch-config-'+r.exported_at.slice(0,10)+'.json',
      JSON.stringify(r.config,null,2));
    t('已导出（含密码，注意保管）');
  }catch(e){t('导出失败：'+e.message)}
}
async function importConf(inp){
  var f=inp.files&&inp.files[0]; inp.value='';
  if(!f)return;
  var txt;
  try{ txt=await f.text(); var cfg=JSON.parse(txt);
    if(!cfg||typeof cfg!=='object'||!cfg.bark) throw new Error('不像 Netwatch 的配置文件');
  }catch(e){ t('导入失败：'+e.message); return }
  if(!confirm('导入会覆盖当前全部设置（含密码），确定继续？'))return;
  try{
    var r=await fetch('/api/import',{method:'POST',body:JSON.stringify(cfg)})
      .then(function(x){return x.json()});
    if(r.ok){ clearDirty(); t('已导入，配置已生效'); await load(); }
    else t('导入失败：'+(r.msg||'未知错误'));
  }catch(e){ t('导入失败：'+e.message) }
}

async function savePw(btn){
  var a=$('pw').value,b=$('pw2').value;
  if(!a){t('请输入新密码');return}
  if(a!==b){t('两次输入不一致');return}
  if(a.length<4){t('密码至少 4 位');return}
  btn.disabled=true;
  try{
    var r=await fetch('/api/save',{method:'POST',body:JSON.stringify({ui_password:a})})
      .then(function(x){return x.json()});
    if(r.ok){ t('密码已修改，请重新登录');
      setTimeout(function(){location.reload()},900); return }
    t('修改失败');
  }catch(e){t('修改失败：'+e.message)}
  btn.disabled=false;
}

async function runNow(){
  var b=$('runBtn'); b.disabled=true;
  try{
    var r=await fetch('/api/run',{method:'POST'}).then(function(x){return x.json()});
    if(!r.started){ t(r.msg||'正在执行中'); b.disabled=false; return }
    t('已开始检测…');
    setRunUI(true,'启动');
    if(S.poll) clearInterval(S.poll);
    S.poll=setInterval(async function(){
      var st;
      try{ st=await fetch('/api/state').then(function(x){return x.json()}) }catch(e){ return }
      setRunUI(!!st.running, st.phase||'');
      if(!st.running){
        clearInterval(S.poll); S.poll=null; b.disabled=false;
        await load();
        var v=st.last?st.last.verdict:'?';
        t('完成：'+v+(st.last&&st.last.push==='skip'?'（按策略未推送）':''));
      }
    },1500);
  }catch(e){ t('失败：'+e.message); b.disabled=false }
}

async function testPush(btn){
  if(btn) btn.disabled=true;
  t('发送中…');
  try{
    var r=await fetch('/api/test-push',{method:'POST',body:'{}'}).then(function(x){return x.json()});
    t(r.ok?'已发送，看看手机':'失败：'+(r.msg||''));
  }catch(e){t('失败：'+e.message)}
  if(btn) btn.disabled=false;
}

async function showHist(id){
  var d;
  try{ d=await fetch('/api/history/'+id).then(function(x){return x.json()}) }catch(e){ t('读取失败'); return }
  if(!d||!d.ts){ t('读取失败'); return }
  var det=d.detail||{};
  $('mTitle').textContent='检测结果 '+d.verdict+' · '+d.ts;
  var sub=[];
  if(det.ts) sub.push('检测于 '+det.ts);
  if(det.open_token_url) sub.push('token 代理 '+det.open_token_url);
  if(det.refresh_age_h!=null) sub.push('刷新间隔 '+det.refresh_age_h+' h');
  $('mSub').textContent=sub.join('  ·  ');
  var body=(det.lines||[]).join('\n');
  if(det.issues&&det.issues.length) body+='\n\n问题：\n- '+det.issues.join('\n- ');
  if(det.warns&&det.warns.length) body+='\n\n注意：\n- '+det.warns.join('\n- ');
  if(det.db_error) body+='\n\ndata.db：'+det.db_error;
  $('mBody').textContent=body||d.summary||'（无详情）';
  $('modal').classList.add('on');
}
function closeModal(){ $('modal').classList.remove('on') }
document.addEventListener('keydown',function(e){ if(e.key==='Escape') closeModal() });

/* 吸顶栏：只在内容真的滚到下面时才浮出边缘 */
(function(){
  var hd=document.querySelector('.hd');
  if(!hd) return;
  function onScroll(){ hd.classList.toggle('scrolled', window.scrollY > 4) }
  window.addEventListener('scroll',onScroll,{passive:true});
  onScroll();
})();

async function logout(){
  try{await fetch('/api/logout',{method:'POST',body:'{}'})}catch(e){}
  location.replace('/');
}

(function(){
  var m=location.search.match(/[?&]tab=(notify|src|conn)/);
  if(m) switchTab(m[1]);
})();
load(); setInterval(function(){ if(!S.dirty) load() },60000);
</script></body></html>"""


# ---------------------------------------------------------------- 服务
SESSION_COOKIE = "aw_sess"
SESSION_DAYS = 7


def _sess_sig(pw, exp):
    key = (pw or "open").encode("utf-8")
    msg = ("%d|%s" % (exp, pw or "")).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def make_session(pw):
    exp = int(time.time()) + SESSION_DAYS * 86400
    return "%d.%s" % (exp, _sess_sig(pw, exp))


def session_ok(pw, val):
    try:
        exp_s, sig = (val or "").split(".", 1)
        exp = int(exp_s)
    except Exception:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sig, _sess_sig(pw, exp))


def cookie_val(handler, name):
    raw = handler.headers.get("Cookie") or ""
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == name:
                return v.strip()
    return ""


def guess_base(handler):
    """从请求里推断"外面能访问到的地址"，用于 PWA manifest 的 scope"""
    host = (handler.headers.get("Host") or "").strip()
    if not host:
        return ""
    proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = "http"
    return "%s://%s" % (proto, host)


def is_authed(handler, conf):
    """cookie 会话优先；兼容 Basic Auth（方便 curl 调试）"""
    pw = conf.get("ui_password") or ""
    if not pw:
        return True
    if session_ok(pw, cookie_val(handler, SESSION_COOKIE)):
        return True
    hdr = handler.headers.get("Authorization") or ""
    if hdr.startswith("Basic "):
        try:
            raw = base64.b64decode(hdr[6:]).decode("utf-8", "ignore")
            if ":" in raw and hmac.compare_digest(raw.split(":", 1)[1], pw):
                return True
        except Exception:
            return False
    return False


class Handler(BaseHTTPRequestHandler):
    server_version = "alist-watch"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None,
              cache="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        conf = load_conf()
        path = self.path.split("?")[0]

        # ---- 图标：登录页也要显示，所以放在鉴权之前
        if path in ("/icon.png", "/icon-512.png"):
            return self._send(200, icon.png(512), "image/png", cache="public, max-age=604800")
        if path in ("/icon-192.png", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            return self._send(200, icon.png(192), "image/png", cache="public, max-age=604800")
        if path in ("/favicon.ico", "/favicon.png", "/icon-32.png"):
            return self._send(200, icon.png(32), "image/png", cache="public, max-age=604800")
        if path == "/icon.svg":
            return self._send(200, icon.SVG, "image/svg+xml", cache="public, max-age=604800")
        if path.startswith("/logo/"):
            name = path[6:]
            if name.endswith(".png"):
                name = name[:-4]
            if name not in icon.LOGO_COLORS:
                return self._send(404, {"error": "no such logo"})
            try:
                sz = int((parse_qs(self.path.split("?", 1)[1]).get("sz") or ["96"])[0])
            except Exception:
                sz = 96
            sz = max(16, min(256, sz))
            return self._send(200, icon.logo_png(name, sz), "image/png",
                              cache="public, max-age=604800")
        if path == "/manifest.webmanifest":
            base = (conf.get("public_base") or "").rstrip("/")
            mf = {"name": "Netwatch · 网盘巡检", "short_name": "Netwatch",
                  "start_url": "/", "display": "standalone",
                  "background_color": "#f5f5f7", "theme_color": "#f5f5f7",
                  "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                            {"src": "/icon.png", "sizes": "512x512", "type": "image/png",
                             "purpose": "any maskable"}]}
            if base:
                mf["scope"] = base + "/"
            return self._send(200, mf, "application/manifest+json")

        if path == "/healthz":
            return self._send(200, {"ok": True,
                                    "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")})
        authed = is_authed(self, conf)
        if path in ("/", "/index.html", "/login"):
            if not authed:
                return self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
            if path == "/login":
                return self._send(302, "", "text/plain", {"Location": "/"})
            # 头一次打开页面时，顺手记下"用户是通过哪个地址访问的"，
            # PWA 装到桌面时 manifest 的 scope 要用它。
            if not (conf.get("public_base") or "").strip():
                gb = guess_base(self)
                if gb:
                    conf["public_base"] = gb
                    save_conf(conf)
                    log("记录访问地址 public_base=%s" % gb)
            page = PAGE.replace("__PMETA__",
                               json.dumps(PROVIDER_META, ensure_ascii=False))
            return self._send(200, page, "text/html; charset=utf-8")
        if not authed:
            return self._send(401, {"error": "auth"})
        if self.path == "/api/state":
            with _state_lock:
                last = STATE["last"]
                running = STATE["running"]
                err = STATE["last_error"]
                phase = STATE["phase"]
            detail_providers = []
            skipped_groups = []
            if not last:
                # 容器刚重启时内存里还没有结果，先用历史最后一轮顶上，
                # 否则页面会短暂显示「首次检测」+ 全 0，看着像数据丢了
                try:
                    con = sqlite3.connect(HIST_PATH, timeout=10)
                    row = con.execute("select ts,verdict,summary from checks "
                                      "order by id desc limit 1").fetchone()
                    con.close()
                    if row:
                        last = {"ts": row[0], "verdict": row[1],
                                "lines": [row[2] or ""], "push": "", "cat": row[0][:10]}
                except Exception:
                    pass
            if last:
                try:
                    con = sqlite3.connect(HIST_PATH, timeout=10)
                    row = con.execute("select detail from checks order by id desc limit 1"
                                      ).fetchone()
                    con.close()
                    if row:
                        _d = json.loads(row[0]) or {}
                        detail_providers = _d.get("providers", [])
                        skipped_groups = _d.get("skipped_groups", [])
                except Exception:
                    pass
            try:
                nr = next_run(conf)
                nxt = nr.strftime("%m-%d %H:%M")
                nxt_ts = int(nr.timestamp())
            except Exception:
                nxt, nxt_ts = "", 0
            host = self.headers.get("Host") or ""
            return self._send(200, {"config": public_conf(conf), "last": last,
                                    "running": running, "phase": phase, "error": err,
                                    "next_run": nxt, "next_run_ts": nxt_ts,
                                    "schedule_text": schedule_text(conf),
                                    "pause_left": paused_left(),
                                    "host": host,
                                    "detail_providers": detail_providers,
                                    "skipped_groups": skipped_groups})
        if self.path == "/api/history":
            return self._send(200, {"items": hist_list(30)})
        if self.path == "/api/stats":
            st = hist_stats()
            st["incidents"] = hist_incidents()
            return self._send(200, st)
        if self.path == "/api/export":
            # 换 NAS 迁移用：整份配置原样导出（含密码），提醒用户保管好
            return self._send(200, {"ok": True, "exported_at":
                                    datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
                                    "config": load_conf()})
        if self.path.startswith("/api/history/"):
            try:
                hid = int(self.path.rsplit("/", 1)[1])
            except Exception:
                return self._send(400, {"error": "bad id"})
            try:
                con = sqlite3.connect(HIST_PATH, timeout=10)
                row = con.execute("select ts,verdict,summary,detail from checks where id=?",
                                  (hid,)).fetchone()
                con.close()
            except Exception as e:
                return self._send(500, {"error": str(e)})
            if not row:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"id": hid, "ts": row[0], "verdict": row[1],
                                    "summary": row[2],
                                    "detail": json.loads(row[3] or "{}")})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        conf = load_conf()
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}
        if path == "/api/login":
            pw = conf.get("ui_password") or ""
            got = str(body.get("password") or "")
            if not pw or hmac.compare_digest(got, pw):
                ck = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
                    SESSION_COOKIE, make_session(pw), SESSION_DAYS * 86400)
                return self._send(200, {"ok": True}, headers={"Set-Cookie": ck})
            time.sleep(0.7)
            return self._send(200, {"ok": False, "msg": "密码不正确"})
        if path == "/api/logout":
            return self._send(200, {"ok": True}, headers={
                "Set-Cookie": "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                              % SESSION_COOKIE})
        if not is_authed(self, conf):
            return self._send(401, {"error": "auth"})
        if path == "/api/pause":
            try:
                h = float(body.get("hours") or 0)
            except Exception:
                h = 0.0
            st = load_runstate()
            st["pause_until"] = (time.time() + h * 3600) if h > 0 else 0
            save_runstate(st)
            log(("暂停巡检 %.1f 小时" % h) if h > 0 else "已取消暂停")
            return self._send(200, {"ok": True, "pause_left": paused_left()})
        if path == "/api/probe":
            g = str(body.get("group") or "").strip()
            if not g:
                return self._send(400, {"ok": False, "msg": "缺少 group"})
            return self._send(200, probe_one(conf, g))
        if path == "/api/run":
            # 立即返回，前端轮询 /api/state（一次巡检可能要一两分钟）
            started = start_run(conf, push=False)
            return self._send(200, {"started": started,
                                    "msg": "" if started else "正在执行中"})
        if path == "/api/test-push":
            now = datetime.now(CST)
            last = (STATE.get("last") or {})
            rid = last.get("hist_id")
            detail = hist_detail(rid) if rid else None
            lines = ["🔔 这是一条测试推送"]
            if last.get("lines"):
                lines += [""] + list(last["lines"])[:6]
            # 按最近一次真实结论渲染，否则标题会跟实际状态打架
            title, body, level = bark_render(conf, last.get("verdict") or "OK",
                                             lines, detail, now)
            ok, msg = bark_push(conf, title, body, level)
            return self._send(200, {"ok": ok, "msg": msg})
        if path == "/api/save":
            for k in ("schedule", "bark", "notify", "probe", "heartbeat"):
                if isinstance(body.get(k), dict):
                    conf[k].update(body[k])
            for k in ("notify_when_ok", "alist_base", "gbox_base", "gbox_user",
                      "gbox_pass", "ui_password", "alist_db", "public_base"):
                if k in body and body[k] not in ("", None):
                    conf[k] = body[k]
            if "" == str(body.get("public_base") or "").strip() and "public_base" in body:
                # 留空 = 自动用当前访问地址
                gb = guess_base(self)
                if gb:
                    conf["public_base"] = gb
            if isinstance(body.get("disabled"), list):
                conf["disabled"] = [str(x) for x in body["disabled"]][:200]
            save_conf(conf)
            log("配置已保存")
            return self._send(200, {"ok": True,
                                    "public_base": conf.get("public_base", ""),
                                    "schedule_text": schedule_text(conf)})
        if path == "/api/import":
            # 与 load_conf 相同的合并规则：dict 逐键合并、其余整体覆盖。
            # 密码类字段为空字符串时跳过，防止手改过的导出文件把密码清掉。
            try:
                for k, v in (body or {}).items():
                    if k in ("ui_password", "gbox_pass") and not str(v or "").strip():
                        continue
                    if isinstance(v, dict) and isinstance(conf.get(k), dict):
                        conf[k].update(v)
                    else:
                        conf[k] = v
            except Exception as e:
                return self._send(400, {"ok": False, "msg": str(e)})
            save_conf(conf)
            log("配置已从备份导入")
            return self._send(200, {"ok": True,
                                    "schedule_text": schedule_text(conf)})
        return self._send(404, {"error": "not found"})


def main():
    hist_init()
    conf = load_conf()
    if not os.path.exists(CONF_PATH):
        save_conf(conf)
    t = threading.Thread(target=scheduler, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log("listening on :%d  data=%s" % (PORT, DATA_DIR))
    srv.serve_forever()


if __name__ == "__main__":
    main()
