"""
SATH · 感知器 · ActivityWatch 行为轨迹摘要器
=============================================
从 ActivityWatch REST API 拉取原始窗口事件,
清洗、聚合、脱敏后生成 LLM 可理解的环境摘要。

隐私原则:
  - 所有处理在本地完成
  - 仅输出脱敏摘要给 LLM, 不传原始窗口标题
  - 敏感 URL 路径自动截断
"""

import json
import sqlite3
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse

import httpx  # pip install httpx

# ── 配置 ──────────────────────────────────────────────────

AW_BASE_URL = "http://localhost:5600/api/0"
AW_TIMEOUT = 5.0  # 秒

# ActivityWatch bucket 名称模式
BUCKET_WINDOW = "aw-watcher-window_{hostname}"
BUCKET_WEB    = "aw-watcher-web-firefox"  # 或 aw-watcher-web-chrome
BUCKET_AFK    = "aw-watcher-afk_{hostname}"

# 脱敏: 这些 app 的窗口标题会被遮蔽
SENSITIVE_APPS = {"1password", "keychain", "bitwarden", "telegram", "signal"}

# URL 白名单域名 (其余只保留域名)
URL_SAFE_DOMAINS = {
    "github.com", "stackoverflow.com", "developer.apple.com",
    "docs.python.org", "shopify.dev", "developer.mozilla.org",
}


# ── 数据结构 ────────────────────────────────────────────

@dataclass
class ActivityEvent:
    """单条窗口/浏览器事件"""
    timestamp: str
    duration_s: float
    app: str
    title: str
    url: Optional[str] = None
    event_type: str = "app_switch"


@dataclass
class ContextSummary:
    """5 分钟窗口的环境摘要 — 喂给 LLM 的结构"""
    window_start: str
    window_end: str
    duration_minutes: float
    top_apps: list[dict]          # [{"app": "Cursor", "minutes": 3.2, "pct": 0.64}]
    active_urls: list[dict]       # [{"domain": "github.com", "path": "/...", "minutes": 1.5}]
    key_titles: list[str]         # 脱敏后的关键窗口标题 (去重, top-5)
    app_switches: int             # 切换次数 → 推断专注度
    idle_pct: float               # 空闲占比
    inferred_activity: str        # "coding" | "browsing" | "communicating" | "designing" | "mixed"
    git_branch: Optional[str] = None
    summary_text: str = ""        # 最终自然语言摘要


# ── ActivityWatch API 客户端 ────────────────────────────

class ActivityWatchClient:
    """轻量 AW REST 客户端, 仅读取"""

    def __init__(self, base_url: str = AW_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=AW_TIMEOUT)
        self._hostname: Optional[str] = None

    @property
    def hostname(self) -> str:
        if not self._hostname:
            info = self.client.get(f"{self.base_url}/info").json()
            self._hostname = info.get("hostname", "unknown")
        return self._hostname

    def _bucket_id(self, pattern: str) -> str:
        return pattern.replace("{hostname}", self.hostname)

    def get_events(
        self,
        bucket_pattern: str,
        start: datetime,
        end: datetime,
        limit: int = 500,
    ) -> list[dict]:
        bucket_id = self._bucket_id(bucket_pattern)
        params = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": limit,
        }
        try:
            resp = self.client.get(
                f"{self.base_url}/buckets/{bucket_id}/events",
                params=params,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, httpx.ConnectError):
            return []

    def close(self):
        self.client.close()


# ── 数据清洗 ────────────────────────────────────────────

def sanitize_title(app: str, title: str) -> str:
    """脱敏窗口标题"""
    app_lower = app.lower()
    # 敏感 app: 完全遮蔽
    if any(s in app_lower for s in SENSITIVE_APPS):
        return f"[{app} - 已隐藏]"
    # 终端: 保留命令部分, 隐藏路径中的用户名
    if any(t in app_lower for t in ("terminal", "iterm", "warp", "kitty")):
        import re
        title = re.sub(r"/Users/\w+", "~", title)
    # 通用: 截断过长标题
    if len(title) > 120:
        title = title[:117] + "..."
    return title


def sanitize_url(url: str) -> dict:
    """URL 脱敏: 安全域名保留路径, 其余只保留域名"""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if any(domain.endswith(d) for d in URL_SAFE_DOMAINS):
        path = parsed.path[:80] if len(parsed.path) > 80 else parsed.path
        return {"domain": domain, "path": path}
    return {"domain": domain, "path": "/[hidden]"}


def infer_activity(top_apps: list[dict]) -> str:
    """根据 top app 推断当前活动类型"""
    if not top_apps:
        return "idle"

    app_set = {a["app"].lower() for a in top_apps[:3]}
    app_str = " ".join(app_set)

    coding_signals = {"cursor", "vscode", "code", "xcode", "terminal", "iterm", "warp", "kitty", "pycharm", "webstorm"}
    browse_signals = {"safari", "chrome", "firefox", "arc", "edge", "brave"}
    comm_signals   = {"slack", "wechat", "lark", "feishu", "telegram", "discord", "zoom", "teams"}
    design_signals = {"figma", "sketch", "photoshop", "illustrator", "canva"}

    scores = {
        "coding":        sum(1 for s in coding_signals if s in app_str),
        "browsing":      sum(1 for s in browse_signals if s in app_str),
        "communicating": sum(1 for s in comm_signals   if s in app_str),
        "designing":     sum(1 for s in design_signals if s in app_str),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "mixed"


# ── 核心: 摘要生成 ──────────────────────────────────────

def summarize_window(
    minutes: int = 5,
    aw_client: Optional[ActivityWatchClient] = None,
) -> ContextSummary:
    """
    获取并摘要过去 N 分钟的行为轨迹。

    返回 ContextSummary — 可直接 JSON 序列化后注入 LLM prompt。
    """
    client = aw_client or ActivityWatchClient()
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes)

    # 1. 拉取原始事件
    window_events = client.get_events(BUCKET_WINDOW, start, now)
    web_events    = client.get_events(BUCKET_WEB, start, now)
    afk_events    = client.get_events(BUCKET_AFK, start, now)

    if not aw_client:
        client.close()

    # 2. 解析窗口事件 → ActivityEvent 列表
    events: list[ActivityEvent] = []
    for e in window_events:
        data = e.get("data", {})
        events.append(ActivityEvent(
            timestamp=e["timestamp"],
            duration_s=e.get("duration", 0),
            app=data.get("app", "Unknown"),
            title=sanitize_title(data.get("app", ""), data.get("title", "")),
        ))

    # 3. 合并浏览器 URL
    url_map: dict[str, float] = {}  # domain+path → seconds
    for e in web_events:
        data = e.get("data", {})
        raw_url = data.get("url", "")
        if raw_url:
            sanitized = sanitize_url(raw_url)
            key = f"{sanitized['domain']}{sanitized['path']}"
            url_map[key] = url_map.get(key, 0) + e.get("duration", 0)

    # 4. 计算空闲时间
    total_afk_s = sum(
        e.get("duration", 0)
        for e in afk_events
        if e.get("data", {}).get("status") == "not-afk"
    )
    total_window_s = minutes * 60
    idle_pct = max(0.0, 1.0 - (total_afk_s / total_window_s)) if total_window_s > 0 else 0.0

    # 5. 聚合 top apps
    app_time: dict[str, float] = {}
    for ev in events:
        app_time[ev.app] = app_time.get(ev.app, 0) + ev.duration_s

    total_active_s = sum(app_time.values()) or 1.0
    top_apps = sorted(
        [
            {
                "app": app,
                "minutes": round(secs / 60, 1),
                "pct": round(secs / total_active_s, 2),
            }
            for app, secs in app_time.items()
        ],
        key=lambda x: x["pct"],
        reverse=True,
    )[:6]

    # 6. 聚合 active URLs
    active_urls = sorted(
        [
            {"domain": k.split("/")[0], "path": "/" + "/".join(k.split("/")[1:]), "minutes": round(v / 60, 1)}
            for k, v in url_map.items()
        ],
        key=lambda x: x["minutes"],
        reverse=True,
    )[:5]

    # 7. 去重窗口标题 (top-5)
    seen = set()
    key_titles = []
    for ev in sorted(events, key=lambda e: e.duration_s, reverse=True):
        t = ev.title
        if t not in seen and "[已隐藏]" not in t:
            key_titles.append(t)
            seen.add(t)
        if len(key_titles) >= 5:
            break

    # 8. 计算 app 切换次数
    switches = 0
    prev_app = None
    for ev in events:
        if prev_app and ev.app != prev_app:
            switches += 1
        prev_app = ev.app

    # 9. 尝试获取 Git 分支
    git_branch = _get_git_branch()

    # 10. 推断活动类型
    activity = infer_activity(top_apps)

    # 11. 生成自然语言摘要
    summary_text = _build_summary_text(
        top_apps, active_urls, key_titles, switches, idle_pct, activity, git_branch, minutes
    )

    return ContextSummary(
        window_start=start.isoformat(),
        window_end=now.isoformat(),
        duration_minutes=minutes,
        top_apps=top_apps,
        active_urls=active_urls,
        key_titles=key_titles,
        app_switches=switches,
        idle_pct=round(idle_pct, 2),
        inferred_activity=activity,
        git_branch=git_branch,
        summary_text=summary_text,
    )


def _get_git_branch() -> Optional[str]:
    """安全获取当前 Git 分支"""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=2,
        )
        branch = result.stdout.strip()
        return branch if branch else None
    except Exception:
        return None


def _build_summary_text(
    top_apps, active_urls, key_titles, switches, idle_pct, activity, git_branch, minutes
) -> str:
    """生成给 LLM 的自然语言环境摘要"""
    lines = [f"[过去 {minutes} 分钟环境快照]"]

    # 活动类型
    activity_zh = {
        "coding": "编码开发",
        "browsing": "浏览研究",
        "communicating": "沟通协作",
        "designing": "设计创作",
        "mixed": "混合活动",
        "idle": "空闲状态",
    }
    lines.append(f"主要活动: {activity_zh.get(activity, activity)}")

    # Top apps
    if top_apps:
        app_str = ", ".join(f"{a['app']}({a['pct']:.0%})" for a in top_apps[:3])
        lines.append(f"使用应用: {app_str}")

    # URLs
    if active_urls:
        url_str = ", ".join(f"{u['domain']}" for u in active_urls[:3])
        lines.append(f"浏览网站: {url_str}")

    # 窗口标题
    if key_titles:
        lines.append(f"关键窗口: {key_titles[0]}")

    # 专注度
    focus = "高" if switches < 5 else ("中" if switches < 15 else "低")
    lines.append(f"专注度: {focus} (切换{switches}次, 空闲{idle_pct:.0%})")

    # Git
    if git_branch:
        lines.append(f"Git 分支: {git_branch}")

    return "\n".join(lines)


# ── 持久化: 写入 SQLite ────────────────────────────────

def persist_summary(summary: ContextSummary, db_path: str | Path) -> int:
    """将摘要写入 context_summary 表, 返回 row ID"""
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        """
        INSERT INTO context_summary (window_start, window_end, summary, top_apps, top_urls, mood, raw_event_ids)
        VALUES (?, ?, ?, ?, ?, ?, '[]')
        """,
        (
            summary.window_start,
            summary.window_end,
            summary.summary_text,
            json.dumps(summary.top_apps, ensure_ascii=False),
            json.dumps(summary.active_urls, ensure_ascii=False),
            summary.inferred_activity,
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


# ── 便捷: 获取最近上下文 (给 Planner 用) ───────────────

def get_recent_context(db_path: str | Path, count: int = 3) -> list[dict]:
    """读取最近 N 条环境摘要, 用于注入 Planner prompt"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM context_summary ORDER BY window_end DESC LIMIT ?",
        (count,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── CLI 入口 ────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    print(f"采集过去 {minutes} 分钟的行为轨迹...\n")

    try:
        summary = summarize_window(minutes=minutes)
        print(summary.summary_text)
        print("\n─── 完整 JSON ───")
        print(json.dumps(asdict(summary), indent=2, ensure_ascii=False))
    except httpx.ConnectError:
        print("⚠ 无法连接 ActivityWatch (http://localhost:5600)")
        print("  请确认 ActivityWatch 正在运行。")
        sys.exit(1)
