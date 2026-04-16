#!/usr/bin/env python3
"""
SATH SQLite Bridge — 满血版
照搬 SATH 完整 pipeline:
  - classify (intent_classifier.py prompt)
  - tool loop (action/params/final_answer 协议, max_turns)
  - Tavily/Brave 搜索
  - web_fetch
  - ActivityWatch 环境感知
  - context_snapshot 注入
"""

import json, sqlite3, os, sys, uuid, threading, re, subprocess, time
import urllib.request, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from collections import deque
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta

# DB 放在用户数据目录，不跟 app bundle 走
_DATA_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "com.sath.todo")
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, "sath.db")
# 首次启动：从 bundle Resources 拷贝初始 DB
_BUNDLE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sath.db")
if not os.path.exists(DB_PATH) and os.path.exists(_BUNDLE_DB):
    import shutil
    shutil.copy2(_BUNDLE_DB, DB_PATH)
    print(f"[bridge] initialized DB from {_BUNDLE_DB}")
PORT = 4800

# ══════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_setting(key):
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    db.close()
    if row:
        try: return json.loads(row['value'])
        except: return row['value']
    return None

def get_llm_config():  return get_setting('llm_config') or {}
def get_persona():     return get_setting('persona') or {}
def get_agent_config(): return get_setting('agent_config') or {}
def get_location():    return get_setting('location') or {}
def get_context_config(): return get_setting('context_config') or {}

# ── Pipeline progress — 照搬 SATH sath://pipeline-progress ──
_progress = {}  # todo_id → deque of {text, status, ts}
_progress_lock = threading.Lock()

def progress_emit(todo_id, text, status='running'):
    with _progress_lock:
        if todo_id not in _progress:
            _progress[todo_id] = deque(maxlen=50)
        _progress[todo_id].append({'text': text, 'status': status, 'ts': time.time()})

def progress_get(todo_id):
    with _progress_lock:
        return list(_progress.get(todo_id, []))

# ── Focus captured — 照搬 SATH: AppleScript 获取前台窗口 ──

def capture_focus():
    """照搬 SATH: AppleScript 获取 frontApp + winTitle"""
    script = '''
    tell application "System Events"
        set frontApp to name of first application process whose frontmost is true
        set winTitle to ""
        try
            tell process frontApp
                set winTitle to name of front window
            end tell
        end try
        return frontApp & "|SATH_SEP|" & winTitle
    end tell
    '''
    try:
        r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=5)
        parts = r.stdout.strip().split('|SATH_SEP|')
        return {'app': parts[0] if parts else '', 'title': parts[1] if len(parts) > 1 else ''}
    except:
        return None

# ── 位置刷新 — 通过 macOS CoreLocation ──

def refresh_location():
    """用 swift 脚本获取 GPS 定位"""
    swift_code = '''
    import CoreLocation
    import Foundation
    class LD: NSObject, CLLocationManagerDelegate {
        let m = CLLocationManager()
        let s = DispatchSemaphore(value: 0)
        var lat = 0.0; var lng = 0.0
        override init() { super.init(); m.delegate = self; m.requestWhenInUseAuthorization(); m.requestLocation() }
        func locationManager(_ m: CLLocationManager, didUpdateLocations l: [CLLocation]) {
            if let c = l.last?.coordinate { lat = c.latitude; lng = c.longitude }; s.signal()
        }
        func locationManager(_ m: CLLocationManager, didFailWithError e: Error) { s.signal() }
        func wait() { _ = s.wait(timeout: .now() + 10) }
    }
    let d = LD(); d.wait()
    print("\\(d.lat),\\(d.lng)")
    '''
    try:
        r = subprocess.run(['swift', '-e', swift_code], capture_output=True, text=True, timeout=15)
        parts = r.stdout.strip().split(',')
        if len(parts) == 2:
            lat, lng = float(parts[0]), float(parts[1])
            if lat != 0 or lng != 0:
                loc = {'lat': lat, 'lng': lng, 'updated_at': datetime.now(timezone.utc).isoformat()}
                db = get_db()
                db.execute("INSERT INTO settings (key,value) VALUES ('location',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (json.dumps(loc),))
                db.commit(); db.close()
                return loc
    except: pass
    return None

# ── Day context 写入 — 照搬 SATH day_context 表 ──

def write_day_context(kind, content, duration=None, todo_id=None):
    now = datetime.now(timezone.utc)
    db = get_db()
    db.execute("INSERT INTO day_context (date, time, kind, content, duration, todo_id, created_at) VALUES (?,?,?,?,?,?,?)",
               (now.strftime('%Y-%m-%d'), now.strftime('%H:%M'), kind, content, duration, todo_id, now.isoformat()))
    db.commit(); db.close()

# ══════════════════════════════════════════════════════════
# LLM call — 照搬 SATH: OpenAI 兼容 /chat/completions
# ══════════════════════════════════════════════════════════

def llm_call(messages, response_format=None):
    cfg = get_llm_config()
    if not cfg or not cfg.get('api_key'):
        print("[llm] no api_key configured")
        return None
    base_url = cfg.get('base_url', 'https://api.openai.com/v1').rstrip('/')
    body = {"model": cfg.get('model', 'gpt-4o-mini'), "messages": messages, "temperature": 0.3}
    if response_format:
        body["response_format"] = response_format
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{base_url}/chat/completions", data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[llm] call failed: {e}")
        return None

# ══════════════════════════════════════════════════════════
# Tool implementations — 照搬 SATH Rust 端
# ══════════════════════════════════════════════════════════

def tool_web_search(query):
    """照搬 SATH: tavily > brave > none"""
    ac = get_agent_config()
    provider = ac.get('search_provider', 'none')
    api_key = ac.get('search_api_key', '')

    if provider == 'tavily' and api_key:
        return _tavily_search(query, api_key)
    elif provider == 'brave' and api_key:
        return _brave_search(query, api_key)
    else:
        return f"search disabled (provider={provider})"

def _tavily_search(query, api_key):
    body = json.dumps({"query": query, "max_results": 6, "include_answer": True}).encode()
    req = urllib.request.Request("https://api.tavily.com/search", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            parts = []
            if data.get('answer'):
                parts.append(f"**摘要**: {data['answer']}\n")
            for r in data.get('results', [])[:6]:
                parts.append(f"- [{r.get('title','')}]({r.get('url','')})\n  {r.get('content','')[:200]}")
            return "\n".join(parts) or "no results"
    except Exception as e:
        return f"tavily error: {e}"

def _brave_search(query, api_key):
    url = f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote(query)}&count=6"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "X-Subscription-Token": api_key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            parts = []
            for r in data.get('web', {}).get('results', [])[:6]:
                parts.append(f"- [{r.get('title','')}]({r.get('url','')})\n  {r.get('description','')[:200]}")
            return "\n".join(parts) or "no results"
    except Exception as e:
        return f"brave error: {e}"

def tool_web_fetch(url):
    """照搬 SATH: fetch URL content"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (SATH Agent)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
            # strip HTML tags, keep text
            text = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.S)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.S)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:8000]  # cap at 8k chars
    except Exception as e:
        return f"web_fetch error: {e}"

# ── 文件系统工具 — 照搬 SATH: fs_read/fs_list/fs_write/shell_run/file_download ──

def tool_fs_read(path):
    try:
        with open(os.path.expanduser(path), 'r', errors='replace') as f:
            return f.read(50000)
    except Exception as e:
        return f"fs_read error: {e}"

def tool_fs_list(path):
    try:
        entries = os.listdir(os.path.expanduser(path))
        return "\n".join(entries[:200])
    except Exception as e:
        return f"fs_list error: {e}"

def tool_fs_write(path, content):
    try:
        p = os.path.expanduser(path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write(content)
        return f"written {len(content)} chars to {p}"
    except Exception as e:
        return f"fs_write error: {e}"

def tool_shell_run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        out = r.stdout[:5000]
        if r.stderr: out += "\nSTDERR: " + r.stderr[:2000]
        return out or "(no output)"
    except Exception as e:
        return f"shell_run error: {e}"

def tool_file_download(url, path):
    try:
        p = os.path.expanduser(path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        urllib.request.urlretrieve(url, p)
        return f"downloaded to {p}"
    except Exception as e:
        return f"file_download error: {e}"

# ── Risk gate — 照搬 SATH: conservative / balanced / aggressive ──

def check_risk_gate(action):
    """照搬 SATH risk gate: conservative 只允许 web_search/web_fetch/final_answer"""
    risk_level = get_setting('risk_level') or 'conservative'
    if isinstance(risk_level, str):
        risk_level = risk_level.strip('"')

    if action == 'final_answer':
        return True

    GATES = {
        'conservative': {'web_search', 'web_fetch'},
        'balanced':     {'web_search', 'web_fetch', 'fs_read', 'fs_list'},
        'aggressive':   {'web_search', 'web_fetch', 'fs_read', 'fs_list', 'fs_write', 'shell_run', 'file_download'},
    }
    allowed = GATES.get(risk_level, GATES['conservative'])
    return action in allowed

TOOL_DISPATCH = {
    "web_search":    lambda args: tool_web_search(args.get("query", "")),
    "web_fetch":     lambda args: tool_web_fetch(args.get("url", "")),
    "fs_read":       lambda args: tool_fs_read(args.get("path", "")),
    "fs_list":       lambda args: tool_fs_list(args.get("path", "")),
    "fs_write":      lambda args: tool_fs_write(args.get("path", ""), args.get("content", "")),
    "shell_run":     lambda args: tool_shell_run(args.get("cmd", "")),
    "file_download": lambda args: tool_file_download(args.get("url", ""), args.get("path", "")),
}

# ── SOUL.md 角色库 — 照搬 SATH: 从 agents/ 目录读取 ──

AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents")

def load_soul(agent_name):
    """从 SATH agents/ 目录读取 SOUL.md"""
    for root, dirs, files in os.walk(AGENTS_DIR):
        for d in dirs:
            soul_path = os.path.join(root, d, 'SOUL.md')
            if d == agent_name and os.path.exists(soul_path):
                with open(soul_path, 'r') as f:
                    return f.read()
    return None

def list_agents():
    """列出所有可用 agent 角色"""
    agents = []
    if not os.path.exists(AGENTS_DIR):
        return agents
    for cat in sorted(os.listdir(AGENTS_DIR)):
        cat_path = os.path.join(AGENTS_DIR, cat)
        if not os.path.isdir(cat_path) or cat.startswith('.'):
            continue
        for name in sorted(os.listdir(cat_path)):
            soul = os.path.join(cat_path, name, 'SOUL.md')
            if os.path.exists(soul):
                agents.append({'category': cat, 'name': name})
    return agents

# ── Obsidian 写入 — 照搬 SATH: obsidian_config → vault/subfolder ──

def obsidian_write(todo_id, title, content):
    """写入 Obsidian vault"""
    obs = get_setting('obsidian_config') or {}
    if not obs.get('enabled') or not obs.get('vault_path'):
        return "obsidian sync disabled"
    vault = os.path.expanduser(obs['vault_path'])
    sub = obs.get('subfolder', 'SATH')
    dirpath = os.path.join(vault, sub)
    os.makedirs(dirpath, exist_ok=True)
    safe_title = re.sub(r'[<>:"/\\|?*]', '_', title or todo_id)[:80]
    filepath = os.path.join(dirpath, f"{safe_title}.md")
    with open(filepath, 'w') as f:
        f.write(content)
    return f"[obsidian] sync {filepath}"

# ── 提醒触发 — 照搬 SATH: due_at check + reminder_fired ──

def check_reminders():
    """检查到期任务，标记 reminder_fired"""
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    rows = db.execute(
        "SELECT id, content, title, due_at FROM todos "
        "WHERE due_at IS NOT NULL AND due_at != '' AND status != 'done' AND status != 'archived' "
        "AND COALESCE(reminder_fired, 0) = 0 AND due_at <= ?", (now,)).fetchall()
    fired = []
    for r in rows:
        db.execute("UPDATE todos SET reminder_fired = 1, updated_at = ? WHERE id = ?", (now, r['id']))
        fired.append({'id': r['id'], 'content': r['content'], 'title': r['title'], 'due_at': r['due_at']})
    db.commit()
    db.close()
    return fired

# ══════════════════════════════════════════════════════════
# 上下文层 — 统一上下文快照组装
# ══════════════════════════════════════════════════════════

def build_context_snapshot(content=None):
    """统一上下文组装：用户模型 + 位置 + 节律 + 最近相关任务
    返回结构化 dict，用于 classify 和 orchestration 注入。
    """
    snap = {}

    # 1. 用户模型快照（v2 UserModel 优先，兜底 settings）
    try:
        from pathlib import Path as _Path
        from sath_source.brain.pipeline import UserModel
        um = UserModel(_Path(DB_PATH))
        model_snap = um.load_snapshot(include_active=True)
        snap['user_model'] = model_snap
    except Exception:
        uprefs = get_setting('user_preferences') or {}
        if isinstance(uprefs, str):
            try: uprefs = json.loads(uprefs)
            except: uprefs = {}
        snap['user_model'] = {'user_preferences': uprefs}

    # 2. 位置
    loc = get_location()
    if loc:
        snap['location'] = loc

    # 3. 节律参数
    rhythm = get_setting('rhythm_params') or {}
    if isinstance(rhythm, str):
        try: rhythm = json.loads(rhythm)
        except: rhythm = {}
    if rhythm:
        snap['rhythm'] = rhythm

    # 4. 事件群（Event Cluster）识别
    # 宽关键词匹配是有意为之的：溯溪 + 露营都能匹配"五一"，说明它们是同一事件群的不同任务。
    # 我们把命中的多条任务作为"整体安排"注入，而不是孤立条目。
    if content:
        try:
            keywords = [w for w in re.split(r'[\s,，。、]+', content) if len(w) > 1][:5]
            if keywords:
                db = get_db()
                like_clauses = " OR ".join(["title LIKE ?" for _ in keywords])
                params = [f"%{kw}%" for kw in keywords]
                rows = db.execute(
                    f"SELECT title, due_at, agent_output, updated_at FROM todos "
                    f"WHERE ({like_clauses}) AND agent_status='done' "
                    "ORDER BY updated_at DESC LIMIT 6",
                    params
                ).fetchall()
                db.close()
                if rows:
                    # 尝试识别事件群：同一时间窗口（±7天）内的任务归为一群
                    # due_at 相近或 title 共享关键词 → 标记为同一 cluster
                    cluster_label = None
                    for kw in keywords:
                        if kw in content and len(kw) > 1:
                            cluster_label = kw
                            break
                    snap['related_todos'] = [
                        {
                            'title': r['title'],
                            'summary': (r['agent_output'] or '')[:200],
                            'due_at': r['due_at'] or '',
                        }
                        for r in rows
                    ]
                    if cluster_label:
                        snap['event_cluster'] = cluster_label
        except Exception as _e:
            print(f"[context] related_todos error: {_e}")

    snap['assembled_at'] = datetime.now(timezone.utc).isoformat()
    return snap


def context_snapshot_to_blocks(snap):
    """将 context_snapshot dict 转换为可注入 prompt 的文本块列表"""
    blocks = []

    # 用户模型
    um = snap.get('user_model', {})
    model_lines = []
    for pat in (um.get('fixed_patterns') or [])[:5]:
        if isinstance(pat, dict):
            model_lines.append(f"- {pat.get('pattern', str(pat))} (置信度 {pat.get('confidence','?')})")
        else:
            model_lines.append(f"- {pat}")
    active = um.get('active_preferences') or {}
    for k, v in list(active.items())[:4]:
        model_lines.append(f"- {k}: {str(v)[:60]}")
    # 兜底：旧 user_preferences
    uprefs = um.get('user_preferences') or {}
    if not model_lines and uprefs:
        for field in ('inputPatterns', 'workRhythm', 'domains', 'preferences'):
            if uprefs.get(field):
                model_lines.append(f"- [{field}] {str(uprefs[field])[:80]}")
    if model_lines:
        blocks.append("## 用户模型\n" + "\n".join(model_lines))

    # 位置
    loc = snap.get('location', {})
    loc_str = loc.get('city') or loc.get('region') or ''
    if loc_str:
        blocks.append(f"## 位置\n{loc_str}")

    # 节律
    rhythm = snap.get('rhythm', {})
    if rhythm.get('peak_hours'):
        blocks.append(f"## 工作节律\n专注时段: {rhythm['peak_hours']}")

    # 事件群 / 相关历史
    related = snap.get('related_todos', [])
    cluster = snap.get('event_cluster')
    if related:
        header = f"## 事件群「{cluster}」的已有安排" if cluster else "## 相关历史任务"
        lines = [header, f"（以下任务可能属于同一整体计划，供意图推断参考）"]
        for r in related:
            due = f" [到期: {r['due_at'][:10]}]" if r.get('due_at') else ''
            lines.append(f"- {r['title']}{due}")
            if r.get('summary'):
                lines.append(f"  摘要: {r['summary'][:100]}")
        blocks.append("\n".join(lines))

    return blocks


# ══════════════════════════════════════════════════════════
# 记忆层 — 跨任务语义记忆提取与检索
# ══════════════════════════════════════════════════════════

def extract_memories_async(todo_id, title, content, agent_output):
    """从 agent 执行结果中提取语义记忆，异步调用，不阻塞 pipeline。
    门控：输出 < 300 字直接跳过（太短没有提取价值）。
    去重：写入前检查语义相似条目（关键词重叠 ≥ 2），避免反复积累同一事实。
    """
    if not agent_output or len(agent_output) < 300:
        return
    messages = [
        {"role": "system", "content": (
            "你是一个信息提取助手。从下方任务执行结果中提取值得长期记忆的具体事实。\n"
            "规则：\n"
            "1. 只提取具体可复用的事实（品牌名、地点、价格区间、具体建议）\n"
            "2. 不提取泛化知识或通用建议\n"
            "3. 注意否定语气：'X 不好用' 和 'X 好用' 是截然不同的记忆，必须保留否定\n"
            "4. 每条一行，以'-'开头；最多 5 条；若无值得记忆的内容则只输出'无'\n"
            "5. 每条末尾附 3 个关键词，格式：- 记忆内容 [词1,词2,词3]"
        )},
        {"role": "user", "content": (
            f"任务：{title}\n原始输入：{content}\n\n执行结果（节选）：\n{agent_output[:2000]}"
        )}
    ]
    raw = llm_call(messages)
    if not raw or raw.strip() in ('无', ''):
        return
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    # 预加载现有关键词集合，用于去重
    existing_keys = [
        set(r['relevance_keys'].split(',')) if r['relevance_keys'] else set()
        for r in db.execute("SELECT relevance_keys FROM memories ORDER BY created_at DESC LIMIT 100").fetchall()
    ]
    count = 0
    for line in raw.strip().split('\n'):
        line = line.strip().lstrip('- ').strip()
        if not line or line == '无':
            continue
        keys_m = re.search(r'\[([^\]]+)\]$', line)
        keys_str = keys_m.group(1) if keys_m else ''
        memory_text = line[:keys_m.start()].strip() if keys_m else line
        if not memory_text:
            continue
        # 去重：与现有记忆关键词重叠 ≥ 2 则跳过
        new_keys = set(k.strip() for k in keys_str.split(',') if k.strip())
        is_dup = any(len(new_keys & ek) >= 2 for ek in existing_keys if ek)
        if is_dup:
            print(f"[memory] dedup skip: {memory_text[:40]}")
            continue
        mem_id = str(uuid.uuid4())
        try:
            db.execute(
                "INSERT INTO memories (id, content, source_todo_id, relevance_keys, created_at, access_count) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (mem_id, memory_text, todo_id, keys_str, now)
            )
            existing_keys.append(new_keys)
            count += 1
        except Exception as _e:
            print(f"[memory] insert error: {_e}")
    db.commit(); db.close()
    print(f"[memory] extracted {count} memories for {todo_id[:8]}")
    # 积累超过 50 条时触发记忆蒸馏
    try:
        total = get_db().execute("SELECT COUNT(*) as n FROM memories").fetchone()['n']
        get_db().close()
        if total > 50:
            threading.Thread(target=distill_memories, daemon=True).start()
    except Exception: pass


def distill_memories():
    """记忆蒸馏：合并重复、删除矛盾、清除30天未命中的老旧记忆。
    每次记忆超过 50 条自动触发，也可手动调用。
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    db = get_db()
    # 1. 过期清理：30 天以上且从未命中 → 直接删除
    cutoff = (now.timestamp() - 30 * 86400)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    deleted = db.execute(
        "DELETE FROM memories WHERE created_at < ? AND access_count = 0", (cutoff_iso,)
    ).rowcount
    if deleted:
        print(f"[memory distill] pruned {deleted} stale memories (>30d, 0 hits)")

    # 2. 提取剩余记忆，交给 LLM 做合并/去矛盾
    rows = db.execute(
        "SELECT id, content, relevance_keys, access_count FROM memories "
        "ORDER BY access_count DESC, created_at DESC LIMIT 80"
    ).fetchall()
    db.commit(); db.close()

    if len(rows) < 5:
        print("[memory distill] too few memories, skip LLM merge")
        return

    mem_list = "\n".join(
        f"{i+1}. {r['content']} [{r['relevance_keys']}] (命中{r['access_count']}次)"
        for i, r in enumerate(rows)
    )
    messages = [
        {"role": "system", "content": (
            "你是一个记忆管理助手。对下方记忆列表执行：\n"
            "1. 合并表达同一事实的重复条目（保留命中次数多的那条，措辞取最清晰的）\n"
            "2. 标记矛盾条目（如'A好用'和'A不好用'）→ 保留更近或命中更多的一条\n"
            "3. 删除太泛化、没有具体信息量的条目\n"
            "输出格式（JSON数组，每条包含 content 和 keys）：\n"
            '[{"content": "保留的记忆内容", "keys": "词1,词2,词3"}, ...]\n'
            "只输出 JSON，不要解释。"
        )},
        {"role": "user", "content": f"记忆列表：\n{mem_list}"}
    ]
    raw = llm_call(messages, response_format={"type": "json_object"})
    if not raw:
        print("[memory distill] LLM failed, skip")
        return

    try:
        result = json.loads(raw)
        # 兼容 {"memories": [...]} 和 [...] 两种格式
        if isinstance(result, dict):
            result = result.get('memories', result.get('list', list(result.values())[0] if result else []))
        if not isinstance(result, list):
            raise ValueError("unexpected format")
    except Exception as _e:
        print(f"[memory distill] parse error: {_e}")
        return

    # 用蒸馏结果替换原有记忆
    now_iso2 = datetime.now(timezone.utc).isoformat()
    db2 = get_db()
    old_ids = [r['id'] for r in rows]
    db2.execute(f"DELETE FROM memories WHERE id IN ({','.join('?'*len(old_ids))})", old_ids)
    kept = 0
    for item in result:
        c = item.get('content', '').strip()
        k = item.get('keys', '')
        if not c:
            continue
        db2.execute(
            "INSERT INTO memories (id, content, source_todo_id, relevance_keys, created_at, access_count) "
            "VALUES (?, ?, NULL, ?, ?, 0)",
            (str(uuid.uuid4()), c, k, now_iso2)
        )
        kept += 1
    db2.commit(); db2.close()
    print(f"[memory distill] done: {len(rows)} → {kept} memories")


def search_relevant_memories(query, limit=5):
    """根据关键词搜索相关记忆，命中时更新 access_count。"""
    if not query:
        return []
    keywords = [w.strip() for w in re.split(r'[\s,，。、]+', query) if len(w.strip()) > 1][:5]
    if not keywords:
        return []
    try:
        db = get_db()
        seen = set()
        results = []
        for kw in keywords:
            rows = db.execute(
                "SELECT id, content, relevance_keys, access_count FROM memories "
                "WHERE content LIKE ? OR relevance_keys LIKE ? "
                "ORDER BY access_count DESC, created_at DESC LIMIT ?",
                (f'%{kw}%', f'%{kw}%', limit * 2)
            ).fetchall()
            for r in rows:
                if r['id'] not in seen:
                    seen.add(r['id'])
                    results.append(dict(r))
        # 更新命中记忆的访问计数
        now = datetime.now(timezone.utc).isoformat()
        for r in results[:limit]:
            db.execute(
                "UPDATE memories SET access_count=access_count+1, accessed_at=? WHERE id=?",
                (now, r['id'])
            )
        db.commit(); db.close()
        return results[:limit]
    except Exception as _e:
        print(f"[memory] search error: {_e}")
        return []


# ══════════════════════════════════════════════════════════
# 常驻层 — 截止日期扫描 + 早报推送
# ══════════════════════════════════════════════════════════

def _resident_notify(text):
    """常驻层通知分发（微信 or 仅打印）"""
    try:
        bot_token = get_setting('wx_bot_token') or ''
        wx_cfg = get_setting('wx_config') or {}
        if isinstance(wx_cfg, str):
            try: wx_cfg = json.loads(wx_cfg)
            except: wx_cfg = {}
        user_id = wx_cfg.get('context_token') or wx_cfg.get('to_user_id') or ''
        if bot_token and user_id:
            threading.Thread(target=_wx_send_reply, args=(user_id, text), daemon=True).start()
        else:
            print(f"[resident] notify (no wx): {text}")
    except Exception as _e:
        print(f"[resident] notify error: {_e}")


def _parse_due_ts(due_str):
    """将 due_at 字符串安全转换为 UTC timestamp。
    处理三种格式：
      - 带时区：'2026-05-01T10:00:00+08:00' / '2026-05-01T02:00:00Z'
      - 无时区：'2026-05-01T10:00:00'  → 视为本地时间转 UTC
      - 仅日期：'2026-05-01'           → 本地时间当天 00:00
    返回 float timestamp，失败返回 None。
    """
    if not due_str:
        return None
    try:
        s = due_str.strip()
        # 标准化 Z → +00:00
        s = re.sub(r'Z$', '+00:00', s)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # 无时区 → 本地时间，附加本地 UTC offset
            import time as _time
            offset = _time.timezone if (_time.daylight == 0 or _time.localtime().tm_isdst == 0) else _time.altzone
            dt = dt.replace(tzinfo=timezone(timedelta(seconds=-offset)))
        return dt.timestamp()
    except Exception:
        return None


def _push_morning_brief(db, now_local):
    """推送今日任务早报"""
    today = now_local.strftime('%Y-%m-%d')
    rows = db.execute(
        "SELECT title, due_at, priority FROM todos "
        "WHERE DATE(COALESCE(due_at, created_at)) = DATE(?) AND classify_status='done' "
        "ORDER BY priority DESC, due_at ASC LIMIT 10",
        (today,)
    ).fetchall()
    if not rows:
        return
    lines = [f"📋 今日任务 ({today})"]
    for r in rows:
        pri = '🔴' if r['priority'] == 3 else '🟡' if r['priority'] == 2 else '⚪'
        due_str = ''
        due_ts = _parse_due_ts(r['due_at'])
        if due_ts:
            local_due = datetime.fromtimestamp(due_ts)  # 转本地时间显示
            due_str = f" [{local_due.strftime('%H:%M')}]"
        lines.append(f"{pri} {r['title']}{due_str}")
    _resident_notify('\n'.join(lines))


def _resident_scan_once():
    """单次扫描：截止提醒 + 超时提醒 + 早报。
    所有时间比较统一在 timestamp（浮点秒）层面进行，完全绕开时区解析歧义。
    """
    now_ts = time.time()          # 当前 UTC 时间戳（float）
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now()    # 仅用于判断本地小时（早报）
    db = get_db()

    # 1. 截止日期提醒
    rows = db.execute(
        "SELECT id, title, due_at FROM todos "
        "WHERE due_at IS NOT NULL AND classify_status='done'"
    ).fetchall()
    for row in rows:
        due_ts = _parse_due_ts(row['due_at'])
        if due_ts is None:
            continue
        diff = due_ts - now_ts
        try:
            if 0 < diff < 86400:          # 24h 内即将到期
                sent = db.execute(
                    "SELECT id FROM resident_log WHERE todo_id=? AND action='due_reminder'",
                    (row['id'],)
                ).fetchone()
                if not sent:
                    hours = max(1, int(diff / 3600))
                    _resident_notify(f"⏰ 任务将在 {hours}h 后到期：{row['title']}")
                    db.execute(
                        "INSERT INTO resident_log (todo_id, action, created_at) VALUES (?, 'due_reminder', ?)",
                        (row['id'], now_utc.isoformat())
                    )
            elif -86400 < diff < 0:        # 24h 内刚超时
                sent = db.execute(
                    "SELECT id FROM resident_log WHERE todo_id=? AND action='overdue_reminder'",
                    (row['id'],)
                ).fetchone()
                if not sent:
                    _resident_notify(f"⚠️ 任务已超时：{row['title']}")
                    db.execute(
                        "INSERT INTO resident_log (todo_id, action, created_at) VALUES (?, 'overdue_reminder', ?)",
                        (row['id'], now_utc.isoformat())
                    )
        except Exception:
            pass

    # 2. 早报（每天 09:00 本地时间，误差 ±90s）
    if now_local.hour == 9 and now_local.minute < 2:
        today_str = now_local.strftime('%Y-%m-%d')
        sent = db.execute(
            "SELECT id FROM resident_log WHERE action='morning_brief' AND created_at LIKE ?",
            (f'{today_str}%',)
        ).fetchone()
        if not sent:
            _push_morning_brief(db, now_local)
            db.execute(
                "INSERT INTO resident_log (todo_id, action, created_at) VALUES (NULL, 'morning_brief', ?)",
                (now_utc.isoformat(),)
            )
            print("[resident] morning brief sent")

    db.commit(); db.close()


def resident_scanner_loop():
    """常驻层主循环，每 60s 扫描一次。"""
    time.sleep(15)  # 启动后稍作等待
    while True:
        try:
            _resident_scan_once()
        except Exception as _e:
            print(f"[resident] scan error: {_e}")
        time.sleep(60)


# ══════════════════════════════════════════════════════════
# 知识库 API 代理 — 照搬 Nio List 全部知识源
# ══════════════════════════════════════════════════════════

def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode('utf-8', errors='replace')[:500]
        try: return json.loads(body_text)
        except: raise Exception(f"HTTP {e.code}: {body_text[:100]}")

def _http_post(url, headers=None, body=None):
    data = json.dumps(body, ensure_ascii=True).encode('utf-8') if body is not None else None
    h = {'Content-Type': 'application/json'}
    if headers: h.update(headers)
    # urllib / http.client 用 latin-1 校验 header 值，统一转成 ASCII 安全字符串
    h = {k: v.encode('ascii', 'ignore').decode('ascii') if isinstance(v, str) else v for k, v in h.items()}
    req = urllib.request.Request(url, data=data, headers=h, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode('utf-8', errors='replace')[:500]
        try: return json.loads(body_text)
        except: raise Exception(f"HTTP {e.code}: {body_text[:200]}")

def _http_raw(url, method='GET', headers=None, body=None):
    data = body.encode() if isinstance(body, str) else body
    h = {k: v.encode('ascii', 'ignore').decode('ascii') if isinstance(v, str) else v for k, v in (headers or {}).items()}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return {'status': r.status, 'body': r.read().decode('utf-8', errors='replace')}
    except urllib.error.HTTPError as e:
        return {'status': e.code, 'body': e.read().decode('utf-8', errors='replace')}

def get_kb_config():
    """从 SQLite 读知识库配置"""
    return get_setting('kb_config') or {}

def save_kb_config(cfg):
    db = get_db()
    db.execute("INSERT INTO settings (key,value) VALUES ('kb_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (json.dumps(cfg, ensure_ascii=False),))
    db.commit(); db.close()

# Notion
NOTION_API = 'https://api.notion.com/v1'
NOTION_VER = '2022-06-28'

def kb_scan_notion(cfg):
    token = cfg.get('kbNotionToken', '').strip()
    # 去掉复制粘贴时可能带入的不可见 Unicode 字符
    token = ''.join(c for c in token if ord(c) < 128)
    if not token: raise Exception('未配置 Integration Token')
    headers = {'Authorization': 'Bearer ' + token, 'Notion-Version': NOTION_VER, 'Content-Type': 'application/json'}
    db_id = cfg.get('kbNotionDb', '').strip()
    if db_id:
        data = _http_post(f"{NOTION_API}/databases/{db_id}/query", headers, {'page_size': 10})
    else:
        data = _http_post(f"{NOTION_API}/search", headers, {'query': '', 'page_size': 20})
    results = data.get('results') or []
    if not results:
        raise Exception('API 正常但无内容 — 请在 Notion 页面右上角「...」→「Add connections」添加集成')
    docs = []
    for p in results[:20]:
        obj_type = p.get('object', '')
        title = ''
        if obj_type == 'database':
            title = ''.join(t.get('plain_text', '') for t in (p.get('title') or []))
        else:
            for k, v in (p.get('properties') or {}).items():
                if v.get('type') == 'title':
                    title = ''.join(t.get('plain_text', '') for t in (v.get('title') or []))
                    break
        docs.append({'title': title or '(无标题)'})
    return [{'source': 'Notion', 'docs': docs}]

# 飞书
FEISHU_API = 'https://open.feishu.cn/open-apis'
_feishu_token_cache = {'token': None, 'expire': 0}

def _get_feishu_token(cfg):
    # 优先用 user_access_token（OAuth 登录后存储）
    user_token = cfg.get('_feishuToken', '').strip()
    if user_token:
        return user_token
    if _feishu_token_cache['expire'] > time.time():
        return _feishu_token_cache['token']
    data = _http_post(f"{FEISHU_API}/auth/v3/tenant_access_token/internal", {},
                      {'app_id': cfg.get('kbFeishuId', ''), 'app_secret': cfg.get('kbFeishuSecret', '')})
    token = data.get('tenant_access_token')
    if token:
        _feishu_token_cache['token'] = token
        _feishu_token_cache['expire'] = time.time() + 6600
    return token

def _feishu_exchange_code(app_id, app_secret, code):
    """用 OAuth code 换取 user_access_token"""
    # 先获取 app_access_token
    app_token_data = _http_post(f"{FEISHU_API}/auth/v3/app_access_token/internal", {},
                                {'app_id': app_id, 'app_secret': app_secret})
    app_token = app_token_data.get('app_access_token', '')
    if not app_token:
        raise Exception('获取 app_access_token 失败')
    # 用 code 换 user_access_token
    data = _http_post(f"{FEISHU_API}/authen/v1/oidc/access_token",
                      {'Authorization': 'Bearer ' + app_token},
                      {'grant_type': 'authorization_code', 'code': code})
    print(f'[kb] Feishu exchange resp: {str(data)[:300]}')
    token = (data.get('data') or data).get('access_token', '')
    if not token:
        raise Exception(data.get('msg') or '换取 user_access_token 失败')
    return token

def kb_scan_feishu(cfg):
    app_id     = cfg.get('kbFeishuId', '').strip()
    app_secret = cfg.get('kbFeishuSecret', '').strip()
    if not app_id or not app_secret: raise Exception('未填写 App ID 或 App Secret')
    user_token = cfg.get('_feishuToken', '').strip()
    if not user_token: raise Exception('未授权，请先点击登录授权')
    headers = {'Authorization': 'Bearer ' + user_token}
    # 读取个人云盘根目录文件列表
    r = _http_raw(f"{FEISHU_API}/drive/v1/files?page_size=20&order_by=EditedTime&direction=DESC", 'GET', headers)
    print(f'[kb] Feishu drive → {r["status"]} {r["body"][:300]}')
    if r['status'] == 401:
        raise Exception('授权已过期，请重新登录')
    if r['status'] != 200:
        raise Exception(f'飞书 API 返回 {r["status"]}: {r["body"][:200]}')
    data = json.loads(r['body'])
    code = data.get('code', 0)
    if code != 0:
        raise Exception(data.get('msg') or f'飞书返回错误码 {code}')
    files = (data.get('data') or {}).get('files') or []
    docs = [{'title': f.get('name', '')} for f in files[:10]]
    return [{'source': '飞书', 'docs': docs}] if docs else [{'source': '飞书', 'docs': [{'title': '飞书已连接，云盘暂无文件'}]}]

# Wolai（Bearer Token + MCP JSON-RPC）
def kb_scan_wolai(cfg):
    token = cfg.get('kbWolaiToken', '').strip()
    if not token: raise Exception('未填写 Wolai Token')
    import json as _json
    headers = {
        'Authorization': 'Bearer ' + token,
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream'
    }
    # MCP initialize
    payload = _json.dumps({
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2024-11-05',
                   'capabilities': {},
                   'clientInfo': {'name': 'sath', 'version': '1.0'}}
    }).encode('utf-8')
    r = _http_raw('https://api.wolai.com/v1/mcp', 'POST', headers, payload)
    if r['status'] != 200:
        raise Exception(f'Wolai MCP 认证失败 (HTTP {r["status"]})')
    # MCP tools/list to enumerate available tools
    payload2 = _json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}).encode('utf-8')
    r2 = _http_raw('https://api.wolai.com/v1/mcp', 'POST', headers, payload2)
    if r2['status'] != 200:
        raise Exception(f'Wolai MCP tools/list 失败 (HTTP {r2["status"]})')
    try:
        rdata = _json.loads(r2['body']) if isinstance(r2['body'], (str, bytes)) else r2['body']
        tools = (rdata.get('result') or {}).get('tools') or []
        docs = [{'title': t.get('name', '')} for t in tools[:10]]
    except Exception:
        docs = [{'title': 'Wolai MCP 连接成功'}]
    return [{'source': 'Wolai', 'docs': docs}] if docs else [{'source': 'Wolai', 'docs': [{'title': 'Wolai MCP 连接成功，暂无内容'}]}]

# IMA
def kb_scan_ima(cfg):
    client_id = cfg.get('kbImaClientId', '').strip()
    api_key   = cfg.get('kbImaApiKey', '').strip()
    if not client_id or not api_key: raise Exception('未填写 Client ID 或 API Key')
    headers = {
        'ima-openapi-clientid': client_id,
        'ima-openapi-apikey':   api_key,
        'Content-Type': 'application/json'
    }
    data = _http_post('https://ima.qq.com/openapi/wiki/v1/get_addable_knowledge_base_list',
                      headers, {'cursor': '', 'limit': 20})
    print(f'[kb] IMA response: {str(data)[:300]}')
    retcode = data.get('retcode', 0)
    if retcode != 0:
        raise Exception(data.get('errmsg') or f'IMA 返回错误码 {retcode}')
    kb_list = (data.get('data') or {}).get('addable_knowledge_base_list') or []
    docs = [{'title': kb.get('name', '')} for kb in kb_list[:10]]
    return [{'source': 'IMA', 'docs': docs}] if docs else [{'source': 'IMA', 'docs': [{'title': 'IMA 已连接，暂无知识库'}]}]

# 语雀（cookie 方式）
def kb_scan_yuque(cfg):
    cookie = cfg.get('_yuqueCookie', '').strip()
    if not cookie: raise Exception('未授权，请先登录语雀')
    headers = {
        'Cookie': cookie,
        'Referer': 'https://www.yuque.com/',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36',
        'Accept': 'application/json'
    }
    r = _http_raw('https://www.yuque.com/api/zsearch?q=a&type=doc&scope=user&page=1&limit=20', 'GET', headers)
    if r['status'] != 200:
        raise Exception(f'HTTP {r["status"]}，cookie 可能已过期，请重新登录')
    data = json.loads(r['body'])
    # zsearch 返回结构: {"data": {"hits": [...], "totalHits": N}}
    hits = (data.get('data') or {}).get('hits') or []
    if not hits:
        raise Exception('已登录但未获取到文档，请确认语雀账号下有内容')
    docs = [{'title': (d.get('title') or '').replace('<em>', '').replace('</em>', '') or '(无标题)'} for d in hits[:20]]
    return [{'source': '语雀', 'docs': docs}]

# 石墨（cookie 方式）
def kb_scan_shimo(cfg):
    cookie = cfg.get('_shimoCookie', '').strip()
    if not cookie: raise Exception('未授权，请先登录石墨文档')
    headers = {
        'Cookie': cookie,
        'Referer': 'https://shimo.im/dashboard',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36',
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest'
    }
    # 依次尝试几个端点，取第一个 200 的
    endpoints = [
        'https://shimo.im/lizard-api/mine/recent?limit=10',
        'https://shimo.im/lizard-api/files?count=10',
        'https://shimo.im/lizard-api/files',
    ]
    last_err = ''
    for url in endpoints:
        r = _http_raw(url, 'GET', headers)
        print(f'[kb] Shimo {url} → {r["status"]} body={r["body"][:200]}')
        if r['status'] == 200:
            try:
                data = json.loads(r['body'])
                items = data if isinstance(data, list) else (data.get('data') or data.get('files') or data.get('items') or [])
                docs = [{'title': f.get('name') or f.get('title', '')} for f in (items if isinstance(items, list) else [])[:10]]
                return [{'source': '石墨文档', 'docs': docs}] if docs else [{'source': '石墨文档', 'docs': [{'title': '石墨文档已连接，暂无文件'}]}]
            except Exception as e:
                last_err = str(e)
        else:
            last_err = f'HTTP {r["status"]}: {r["body"][:100]}'
    raise Exception(f'石墨 API 请求失败: {last_err}')

# 本地文件
def kb_scan_local(cfg):
    path = os.path.expanduser(cfg.get('kbLocalPath', '').strip())
    if not path: raise Exception('未填写文件夹路径')
    if not os.path.exists(path): raise Exception(f'路径不存在: {path}')
    ext_raw = cfg.get('kbLocalExt', '').strip()
    exts = [e.strip().lstrip('.') for e in ext_raw.split(',') if e.strip()] if ext_raw else ['md', 'txt']
    docs = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in sorted(files):
            if not f.startswith('.') and any(f.lower().endswith('.' + e) for e in exts):
                docs.append({'title': os.path.splitext(f)[0]})
        if len(docs) >= 200:
            break
    if not docs:
        raise Exception(f'未找到匹配文件（格式: {", ".join(exts)}，路径: {path}）')
    return [{'source': '本地文件', 'docs': docs}]

# Obsidian 输出同步（保留，给 obsidian_config 用）
def kb_scan_obsidian_vault():
    obs = get_setting('obsidian_config') or {}
    if not obs.get('enabled') or not obs.get('vault_path'): return []
    vault = obs['vault_path']
    sub = obs.get('subfolder', 'SATH')
    scan_dir = os.path.join(vault, sub)
    if not os.path.exists(scan_dir): return []
    titles = [f.replace('.md', '') for f in sorted(os.listdir(scan_dir))[:20] if f.endswith('.md')]
    return [{'source': 'Obsidian', 'docs': [{'title': t} for t in titles]}] if titles else []

# Obsidian 知识库读取（KB 设置页面，与输出同步完全隔离）
def kb_scan_obsidian_kb(cfg):
    vault = cfg.get('kbObsVault', '').strip()
    if not vault:
        raise Exception('未填写 Vault 路径')
    vault = os.path.expanduser(vault)
    if not os.path.exists(vault):
        raise Exception(f'路径不存在: {vault}')
    subfolder = cfg.get('kbObsFolder', '').strip()
    scan_dir = os.path.join(vault, subfolder) if subfolder else vault
    if not os.path.exists(scan_dir):
        raise Exception(f'子目录不存在: {scan_dir}')
    docs = []
    for root, dirs, files in os.walk(scan_dir):
        # 跳过隐藏目录（.obsidian 等）
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.endswith('.md') and not f.startswith('.'):
                title = os.path.splitext(f)[0]
                docs.append({'title': title})
        if len(docs) >= 200:
            break
    if not docs:
        raise Exception(f'未找到 .md 文件（扫描路径: {scan_dir}）')
    return [{'source': 'Obsidian', 'docs': docs}]

# 全量扫描所有已配置的知识源
def kb_scan_all():
    """蒸馏前调用：扫描所有已配置的知识源，返回合并的 knowledgeBases 数组"""
    kb_cfg = get_kb_config()
    results = []
    def _safe(fn, *args):
        try:
            results.extend(fn(*args))
        except Exception as e:
            print(f"[kb] {fn.__name__} failed: {e}")
    _safe(kb_scan_obsidian_vault)
    if kb_cfg.get('notion'): _safe(kb_scan_notion, kb_cfg['notion'])
    if kb_cfg.get('feishu'): _safe(kb_scan_feishu, kb_cfg['feishu'])
    if kb_cfg.get('wolai'): _safe(kb_scan_wolai, kb_cfg['wolai'])
    if kb_cfg.get('ima'): _safe(kb_scan_ima, kb_cfg['ima'])
    if kb_cfg.get('yuque'): _safe(kb_scan_yuque, kb_cfg['yuque'])
    if kb_cfg.get('shimo'): _safe(kb_scan_shimo, kb_cfg['shimo'])
    if kb_cfg.get('local'): _safe(kb_scan_local, kb_cfg['local'])
    print(f"[kb] scanned {len(results)} sources, {sum(len(r.get('docs',[])) for r in results)} docs total")
    return results

# ══════════════════════════════════════════════════════════
# 反馈采集 + 蒸馏 — 照搬 Nio List 4 维输入 → 4 字段输出
# ══════════════════════════════════════════════════════════

def submit_feedback(todo_id, diffs):
    """记录用户修正：AI 原值 vs 用户改后的值"""
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO feedback (todo_id, verdict, raw, title, types, tags, created_at) VALUES (?,?,?,?,?,?,?)",
        (todo_id, json.dumps(diffs, ensure_ascii=False), '', '', '[]', '[]', now))
    db.commit()
    db.close()

def get_feedback_count():
    db = get_db()
    row = db.execute("SELECT COUNT(*) FROM feedback").fetchone()
    db.close()
    return row[0] if row else 0

def distill_preferences():
    """满血蒸馏：行为(6维) + 反馈(带细节) + 知识库 + 任务历史 → 4 字段 user_preferences"""
    db = get_db()

    # 维度 1: 行为统计（day_context）— 补齐 WiFi/地点/时段/活跃率/输入率
    ctx_rows = db.execute("SELECT kind, content, date, time FROM day_context ORDER BY created_at DESC LIMIT 200").fetchall()
    behavior = {}
    if ctx_rows:
        app_count = {}
        period_app = {}  # 时段 → app 统计
        loc_count = {}
        active_n = 0
        for r in ctx_rows:
            if r['kind'] == 'focus':
                parts = r['content'].split(' — ') if ' — ' in r['content'] else [r['content']]
                app = parts[0]
                app_count[app] = app_count.get(app, 0) + 1
                # 时段模式
                hour = int(r['time'].split(':')[0]) if r['time'] else 12
                period = '早晨' if hour < 9 else '上午' if hour < 12 else '下午' if hour < 18 else '晚间'
                if period not in period_app: period_app[period] = {}
                period_app[period][app] = period_app[period].get(app, 0) + 1
                active_n += 1
            elif r['kind'] == 'classify':
                active_n += 1

        top_n = lambda d, n=5: [f"{k}({v}次)" for k, v in sorted(d.items(), key=lambda x: x[1], reverse=True)[:n]]
        if app_count:
            behavior['topApps'] = top_n(app_count)
        # 时段模式
        if period_app:
            period_summary = {}
            for p, apps in period_app.items():
                period_summary[p] = ', '.join(top_n(apps, 3))
            behavior['periodPattern'] = period_summary
        # 活跃率
        behavior['totalRecords'] = len(ctx_rows)
        behavior['activityRate'] = str(round(active_n / len(ctx_rows) * 100)) + '%' if ctx_rows else '0%'

        # 位置（从 settings.location）
        loc = get_setting('location') or {}
        if loc.get('city') or loc.get('address'):
            loc_str = ' '.join(filter(None, [loc.get('city'), loc.get('address')]))
            behavior['topLocations'] = [loc_str]

    # 维度 2: 反馈信号（8方向表态）— 拆成意图维度(accurate) + 内容维度(effective)
    fb_rows = db.execute("""
        SELECT f.verdict, t.types, t.tags, t.title
        FROM feedback f
        LEFT JOIN todos t ON t.id = f.todo_id
        ORDER BY f.created_at DESC LIMIT 200
    """).fetchall()

    feedback_stats = {}
    if fb_rows:
        # 意图维度：accurate → 影响 inputPatterns
        intent_accurate   = []  # 意图理解准确的任务特征
        intent_inaccurate = []  # 意图理解偏差的任务特征

        # 内容维度：effective → 影响 preferences
        content_effective   = []  # 内容有效的任务特征
        content_ineffective = []  # 内容无效的任务特征

        total = len(fb_rows)
        for r in fb_rows:
            try:
                diffs = json.loads(r['verdict'])
                types = json.loads(r['types'] or '[]') if r['types'] else []
                tags  = json.loads(r['tags']  or '[]') if r['tags']  else []
                title = r['title'] or ''
                feature = {'types': types, 'tags': tags[:3], 'title': title[:30]}

                acc = diffs.get('accurate')
                eff = diffs.get('effective')

                if acc:
                    if acc.get('user') == 'true':
                        intent_accurate.append(feature)
                    elif acc.get('user') == 'false':
                        intent_inaccurate.append(feature)

                if eff:
                    if eff.get('user') == 'true':
                        content_effective.append(feature)
                    elif eff.get('user') == 'false':
                        content_ineffective.append(feature)
            except: pass

        feedback_stats = {
            'total': total,
            'intentAccuracy': {
                'accurateCount': len(intent_accurate),
                'inaccurateCount': len(intent_inaccurate),
                'accurateSamples': intent_accurate[:8],
                'inaccurateSamples': intent_inaccurate[:8],
            },
            'contentEffectiveness': {
                'effectiveCount': len(content_effective),
                'ineffectiveCount': len(content_ineffective),
                'effectiveSamples': content_effective[:8],
                'ineffectiveSamples': content_ineffective[:8],
            }
        }

    # 维度 3: 知识库元信息 — 主动扫描所有已配置的知识源
    kb_info = kb_scan_all()

    # 维度 4: 任务历史统计
    todo_rows = db.execute("SELECT types, tags, priority, status, agent_status FROM todos").fetchall()
    task_stats = {}
    if todo_rows:
        all_tags = set()
        done = 0; high = 0; with_agent = 0
        for r in todo_rows:
            if r['status'] == 'done': done += 1
            if r['priority'] and r['priority'] >= 3: high += 1
            if r['agent_status'] == 'done': with_agent += 1
            try:
                for t in json.loads(r['tags'] or '[]'):
                    all_tags.add(t)
            except: pass
        # 分类分布
        cat_count = {}
        for r in todo_rows:
            try:
                types = json.loads(r['types'] or '[]')
                for tp in types:
                    cat_count[tp] = cat_count.get(tp, 0) + 1
            except: pass
        task_stats = {'total': len(todo_rows), 'done': done, 'highPriority': high,
                      'withAgent': with_agent, 'categoryDistribution': cat_count,
                      'tags': list(all_tags)[:20]}

    db.close()

    # 组装蒸馏输入
    distill_input = {}
    if behavior: distill_input['behavior'] = behavior
    if feedback_stats: distill_input['feedback'] = feedback_stats
    if kb_info: distill_input['knowledgeBases'] = kb_info
    if task_stats: distill_input['taskStats'] = task_stats

    if not distill_input:
        return None

    # 调 LLM 蒸馏，产出 4 字段
    prompt = f"""你是一个用户行为分析器。根据以下脱敏后的统计数据，提炼出用户的行为模式和偏好。

## 统计数据
{json.dumps(distill_input, ensure_ascii=False, indent=2)}

## 反馈信号说明
数据中的 feedback 包含两个独立维度，来自用户对每个AI任务的表态：
- **intentAccuracy（意图维度）**：用户判断AI是否正确理解了任务意图。accurateSamples = 理解准确的任务，inaccurateSamples = 理解偏差的任务。这个维度用于推断 inputPatterns。
- **contentEffectiveness（内容维度）**：用户判断AI给出的内容是否对自己有实际价值。effectiveSamples = 内容有效的任务，ineffectiveSamples = 内容无效的任务。这个维度用于推断 preferences。

## 要求
输出JSON，包含以下4个字段（每个字段是2-3句自然语言）：

{{
  "inputPatterns": "基于意图准确率(intentAccuracy)推断：AI在哪些类型/标签的任务上容易误解意图？哪些任务意图理解准确？用户的任务表达有什么规律？（无数据则留空）",
  "workRhythm": "基于时段模式和应用使用习惯推断：用户的核心工作时段、活跃高峰、不同时段的工作类型。",
  "domains": "基于知识库、标签分布、任务分类推断：用户的专业领域、关注方向、常用术语。",
  "preferences": "基于内容有效率(contentEffectiveness)推断：哪些类型的AI输出对用户有价值？哪些无效？用户更偏好什么样的内容形式和深度？（无数据则留空）"
}}

规则：
- 只输出模式，不包含具体人名、公司名
- 每字段2-3句话
- 数据不足以判断某项则留空字符串
- 只返回JSON"""

    raw = llm_call([
        {"role": "system", "content": "你是用户行为分析器。只返回JSON。"},
        {"role": "user", "content": prompt}
    ], response_format={"type": "json_object"})

    if not raw:
        return None

    try:
        result = json.loads(raw)
    except:
        start = raw.find('{'); end = raw.rfind('}') + 1
        if start >= 0 and end > start:
            try: result = json.loads(raw[start:end])
            except: return None
        else: return None

    # 写回 settings.user_preferences
    db2 = get_db()
    db2.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ('user_preferences', json.dumps(result, ensure_ascii=False)))
    db2.commit()
    db2.close()

    print(f"[distill] done: {json.dumps(result, ensure_ascii=False)[:200]}")
    return result

# ══════════════════════════════════════════════════════════
# ActivityWatch — 照搬 SATH sensor/activity_watch.py
# ══════════════════════════════════════════════════════════

AW_BASE = "http://localhost:5600/api/0"
SENSITIVE_APPS = {"1password", "keychain", "bitwarden", "telegram", "signal"}

def get_activity_context(minutes=5):
    """照搬 SATH: 从 ActivityWatch 拉取环境摘要"""
    try:
        # get hostname
        with urllib.request.urlopen(f"{AW_BASE}/info", timeout=3) as r:
            hostname = json.loads(r.read()).get("hostname", "unknown")
    except:
        return None  # AW not running

    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes)
    params = f"start={start.isoformat()}&end={now.isoformat()}&limit=500"

    # window events
    events = _aw_get(f"/buckets/aw-watcher-window_{hostname}/events?{params}")
    afk_events = _aw_get(f"/buckets/aw-watcher-afk_{hostname}/events?{params}")

    if not events:
        return None

    # aggregate apps
    app_time = {}
    for e in events:
        app = e.get("data", {}).get("app", "Unknown")
        app_time[app] = app_time.get(app, 0) + e.get("duration", 0)

    total = sum(app_time.values()) or 1
    top_apps = sorted([{"app": a, "pct": round(s/total, 2), "minutes": round(s/60, 1)}
                       for a, s in app_time.items()], key=lambda x: x["pct"], reverse=True)[:5]

    # key titles (sanitized)
    seen = set()
    key_titles = []
    for e in sorted(events, key=lambda x: x.get("duration", 0), reverse=True):
        d = e.get("data", {})
        app, title = d.get("app", ""), d.get("title", "")
        if any(s in app.lower() for s in SENSITIVE_APPS):
            title = f"[{app} - 已隐藏]"
        if len(title) > 120: title = title[:117] + "..."
        if title not in seen and "[已隐藏]" not in title:
            key_titles.append(title); seen.add(title)
        if len(key_titles) >= 5: break

    # switches
    switches = 0
    prev = None
    for e in events:
        app = e.get("data", {}).get("app", "")
        if prev and app != prev: switches += 1
        prev = app

    # idle
    active_s = sum(e.get("duration", 0) for e in afk_events if e.get("data", {}).get("status") == "not-afk")
    idle_pct = max(0, 1 - active_s / (minutes * 60)) if minutes > 0 else 0

    # infer activity
    app_str = " ".join(a["app"].lower() for a in top_apps[:3])
    activity = "mixed"
    for label, signals in [("coding", ["cursor","vscode","code","xcode","terminal","iterm","warp"]),
                           ("browsing", ["safari","chrome","firefox","arc","edge","brave"]),
                           ("communicating", ["slack","wechat","lark","feishu","telegram","discord","zoom"]),
                           ("designing", ["figma","sketch","photoshop","canva"])]:
        if any(s in app_str for s in signals):
            activity = label; break

    # git branch
    git_branch = None
    try:
        r = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, timeout=2)
        git_branch = r.stdout.strip() or None
    except: pass

    activity_zh = {"coding":"编码开发","browsing":"浏览研究","communicating":"沟通协作","designing":"设计创作","mixed":"混合活动","idle":"空闲"}
    focus = "高" if switches < 5 else ("中" if switches < 15 else "低")

    app_str = ', '.join(a['app'] + '(' + str(int(a['pct']*100)) + '%)' for a in top_apps[:3])
    lines = [f"[过去 {minutes} 分钟环境快照]",
             f"主要活动: {activity_zh.get(activity, activity)}",
             f"使用应用: {app_str}"]
    if key_titles: lines.append(f"关键窗口: {key_titles[0]}")
    lines.append(f"专注度: {focus} (切换{switches}次, 空闲{int(idle_pct*100)}%)")
    if git_branch: lines.append(f"Git 分支: {git_branch}")

    return "\n".join(lines)

def _aw_get(path):
    try:
        with urllib.request.urlopen(f"{AW_BASE}{path}", timeout=5) as r:
            return json.loads(r.read())
    except: return []

# ══════════════════════════════════════════════════════════
# 拓扑分解器 — 规则层：底座维度 × 场景维度（无 LLM，纯正则）
# ══════════════════════════════════════════════════════════

_DIM_PATTERNS = {
    'time': re.compile(
        r'今[天晚早]|明[天早晚]|后天|下周|下个?月|月底|年底|截止|deadline|到期|'
        r'[0-9]+[天小时分钟]后|几[点号]|周[一二三四五六日]|星期[一二三四五六日]|'
        r'[上下]午|晚上|早上|ASAP|asap|尽快|马上|立刻|今晚|明早', re.I
    ),
    'location': re.compile(
        r'在哪|哪里|附近|多远|地方|地点|位置|地址|导航|路线|'
        r'北京|上海|广州|深圳|成都|杭州|南京|武汉|[区县市省路街道号]'
    ),
    'people': re.compile(
        r'发给|告诉|通知|联系|约|给.{0,6}[看说发]|和.{0,6}一起|'
        r'@[^\s]+|客户|老板|同事|团队|朋友|家人'
    ),
    'cost': re.compile(
        r'预算|花费|价格|多少钱|便宜|最贵|免费|付款|报销|费用|'
        r'[0-9]+[元块万千百]|[¥$][0-9]|[0-9][元]'
    ),
    'priority': re.compile(
        r'重要|紧急|urgent|asap|必须|一定|优先|首先|最重要|P0|P1', re.I
    ),
    'state': re.compile(
        r'状态|进度|已完成|未完成|待办|阻塞|等待|暂停|pending|blocked', re.I
    ),
}

_SCENE_PATTERNS = [
    ('工作',  re.compile(r'代码|bug|会议|文档|报告|项目|需求|测试|部署|API|PR|发布|review|上线|产品|技术', re.I)),
    ('查询',  re.compile(r'查|搜|推荐|怎么|如何|什么是|有没有|哪个好|了解|调研|竞品|对比')),
    ('餐饮',  re.compile(r'吃|餐|饭|咖啡|外卖|菜|喝|甜点|奶茶|烤肉|火锅|餐厅|饭店')),
    ('出行',  re.compile(r'打车|地铁|公交|机票|酒店|出差|旅行|交通|导航|高铁|出发|到达')),
    ('运动',  re.compile(r'跑步|健身|游泳|打球|锻炼|运动|体育|瑜伽|骑行')),
    ('购物',  re.compile(r'买|购|订|下单|商品|产品|快递|电商|优惠|折扣')),
    ('其他',  re.compile(r'.')),
]

_DIM_HINTS = {
    'time':     'pipeline 必须包含提醒/截止处理 step（设置 due_at 或创建提醒）',
    'location': 'pipeline 必须包含位置搜索 step（查询地点/距离/导航）',
    'people':   'pipeline 必须包含通知/发送 step（发送给具体人员）',
    'cost':     'pipeline 必须包含费用估算 step（价格比较或预算分析）',
    'priority': 'pipeline 中最关键 step 应排在最前（高优先级任务）',
    'state':    'pipeline 应包含状态追踪 step（更新任务进度）',
}

_SCENE_TOOLS = {
    '工作': ['fs_read', 'web_search'],
    '查询': ['web_search', 'web_fetch'],
    '餐饮': ['web_search'],
    '出行': ['web_search'],
    '运动': ['web_search'],
    '购物': ['web_search', 'web_fetch'],
    '其他': ['web_search'],
}

_DIM_LABELS = {'time':'时间', 'location':'位置', 'people':'人员',
               'cost':'费用', 'priority':'优先级', 'state':'状态'}


def topology_decompose(text):
    """
    规则层拓扑分解：纯正则，无 LLM，< 1ms。

    返回:
      {
        "dimensions": {"time": True, "location": False, ...},
        "scene": "工作",
        "hints": ["pipeline 必须包含...", ...],
        "suggested_tools": ["web_search", ...],
        "dimension_summary": "时间✓ 人员✓",   ← 注入 prompt 用
      }

    DAG 原则（注入给 LLM）：
      - 命中维度 → LLM 必须为该维度生成对应 step
      - 无依赖的 step → depends_on: []（并行）
      - 有依赖的 step → depends_on: [前序 step 的 0-based 索引]
    """
    dims = {d: bool(pat.search(text)) for d, pat in _DIM_PATTERNS.items()}

    scene = '其他'
    for s, pat in _SCENE_PATTERNS:
        if pat.search(text):
            scene = s
            break

    hints = [_DIM_HINTS[d] for d, hit in dims.items() if hit]
    tools = _SCENE_TOOLS.get(scene, ['web_search'])
    active = [d for d, hit in dims.items() if hit]
    summary = ' '.join(f"{_DIM_LABELS[d]}✓" for d in active) or '无明确维度'

    return {
        'dimensions': dims,
        'scene': scene,
        'hints': hints,
        'suggested_tools': tools,
        'dimension_summary': summary,
    }


# ══════════════════════════════════════════════════════════
# Classify — 照搬 SATH intent_classifier.py
# ══════════════════════════════════════════════════════════

CLASSIFY_SYSTEM = """\
你是 SATH 的意图物化引擎。你的唯一任务是将用户的碎片输入转化为结构化的 TODO 对象。

## 你的身份 (Persona)
{persona_block}

## 规则
1. 你只输出 JSON, 不输出任何解释文字
2. 每次输入产出 1 个 TODO 对象
3. **置信度执行原则**：任何置信度下都生成 pipeline，不反问用户。信息缺口从「用户认知」段推断，无法推断时选最高概率路径，在 reasoning 里注明推断依据
4. 意图分类:
   - research: 需要查资料、对比方案、调研竞品、推荐、找地方、找东西 → 触发搜索 Agent
   - task: 明确的执行动作 (写代码、修 bug、发邮件) → 附加环境快照
   - record: 备忘、想法、灵感 → 存入知识库
   - reminder: 有时间触发条件的事项 → 设置提醒时间
4. 优先级判定:
   - 4(urgent): 线上故障、阻断性 bug、今天必须完成
   - 3(high): 影响进度、客户相关、有 deadline
   - 2(mid): 常规工作任务
   - 1(low): 优化、nice-to-have
   - 0(none): 纯记录
5. 置信度 confidence: 0.0~1.0, 反映你对意图判定的把握程度
6. 如果环境上下文提供了相关信息(正在看的代码、网页), 主动关联到 context_snapshot
7. **pipeline 是必填字段**, 每种意图都必须生成至少一个 pipeline step:
   - research → 搜索+整理 Agent (extra_tools: ["web_search", "web_fetch"])
   - task → 任务拆解 Agent (分解并执行步骤)
   - record → 知识库关联 Agent
   - reminder → 提醒设置 Agent
8. 如果用户提到"想吃/想去/想买/推荐/哪里有/附近"等隐含搜索需求, 必须分类为 research
9. **category 和 intent 是两个独立字段**:
   - intent: 决定 AI 怎么执行（research/task/record/reminder），不要改
   - category: 决定前端显示什么分类（work=工作 / personal=个人 / study=学习 / other=其他），根据内容语义判断

## 输出格式
```json
{{
  "title": "简洁的任务标题 (≤30字)",
  "intent": "research | task | record | reminder",
  "priority": 0-4,
  "confidence": 0.0-1.0,
  "category": "work | personal | study | other",
  "tags": ["标签1"],
  "due_at": "ISO 8601 或 null",
  "reasoning": "一句话推理",
  "pipeline": [
    {{
      "agent": "agent_name",
      "display_name": "显示名",
      "brief": "具体的执行指令，告诉 Agent 要做什么",
      "success_criteria": "完成标准",
      "extra_tools": ["web_search", "web_fetch"],
      "depends_on": []
    }}
  ],
  "meta": {{}}
}}
```

## 拓扑维度规则（底座 × 场景）
系统已预先对输入做了维度检测，结果注入在用户消息的「拓扑分析」段。
你必须在 pipeline 中覆盖所有检测到的维度：
- **时间✓** → pipeline 必须包含提醒/截止处理 step
- **位置✓** → pipeline 必须包含位置搜索/地图 step
- **人员✓** → pipeline 必须包含发送/通知 step
- **费用✓** → pipeline 必须包含费用估算 step

DAG 依赖原则：
- 互不依赖的 step → `"depends_on": []`（系统并行执行）
- 需要前序结果的 step → `"depends_on": [0]`（0-based 索引）
- 例: 搜索(0) → 整理(1,depends_on:[0]) → 发送(2,depends_on:[1]) / 提醒(独立,depends_on:[])\
"""

# ══════════════════════════════════════════════════════════
# Agent tool loop — 照搬 SATH Rust 端 action/final_answer 协议
# ══════════════════════════════════════════════════════════

AGENT_SYSTEM = """\
你是 SATH 的执行 Agent。你工作在「先做后告」模式下——不打断用户、不提问、按最高置信路径直接执行。

## 核心原则
- **不提问原则**：遇到信息缺口时，从上下文推断最合理答案后继续执行。不要问用户"您是指……吗？"
- **置信度原则**：任何置信度下都执行，0.8+ 直接做，0.5-0.8 做并标注"基于推断"，< 0.5 也做但在 final_answer 里说明不确定点
- **结果优先**：final_answer 必须是可操作的结构化结果，不是对话，不是提问，不是"建议您……"

## 可用工具
{tools_block}

## 工具调用格式（每次只调用一个）
{{"action": "web_search", "args": {{"query": "搜索关键词"}}}}
{{"action": "web_fetch", "args": {{"url": "https://..."}}}}
{{"action": "final_answer", "args": {{"markdown": "最终 Markdown 结果"}}}}

## 执行规则
1. 先用 web_search 获取信息，需要时用 web_fetch 抓取详情
2. 信息充分后用 final_answer 输出，格式要结构化（标题/列表/表格）
3. 搜索无结果时，用已知知识给出最佳回答，仍然输出 final_answer
4. 严禁以任何形式向用户提问或让用户做选择
"""

def build_tools_block(extra_tools):
    """照搬 SATH: default_tools + extra_tools, filtered by risk gate"""
    default = ["web_search", "web_fetch"]
    all_tools = list(set(default + (extra_tools or [])))
    # filter by risk gate
    allowed = [t for t in all_tools if check_risk_gate(t)]

    TOOL_DOCS = {
        "web_search":    '- web_search: 联网搜索\n  {"action": "web_search", "args": {"query": "搜索关键词"}}',
        "web_fetch":     '- web_fetch: 抓取网页内容\n  {"action": "web_fetch", "args": {"url": "https://..."}}',
        "fs_read":       '- fs_read: 读取文件\n  {"action": "fs_read", "args": {"path": "/path/to/file"}}',
        "fs_list":       '- fs_list: 列出目录\n  {"action": "fs_list", "args": {"path": "/path/to/dir"}}',
        "fs_write":      '- fs_write: 写入文件\n  {"action": "fs_write", "args": {"path": "/path", "content": "..."}}',
        "shell_run":     '- shell_run: 执行命令\n  {"action": "shell_run", "args": {"cmd": "command"}}',
        "file_download": '- file_download: 下载文件\n  {"action": "file_download", "args": {"url": "...", "path": "..."}}',
    }
    lines = [TOOL_DOCS[t] for t in allowed if t in TOOL_DOCS]
    lines.append('- final_answer: 输出最终结果\n  {"action": "final_answer", "args": {"markdown": "..."}}')
    return "\n".join(lines)

def run_agent_with_tools(brief, content, extra_tools=None, max_turns=None, agent_name=None, todo_id=None):
    """照搬 SATH tool loop: LLM ↔ tool 多轮循环直到 final_answer
    支持 SOUL.md 角色注入 + risk gate"""
    ac = get_agent_config()
    if max_turns is None:
        max_turns = ac.get('max_turns', 15)

    tools_block = build_tools_block(extra_tools)

    # SOUL.md 角色注入 — 照搬 SATH: 如果 pipeline step 指定了 agent name, 读取对应 SOUL.md
    soul_content = None
    if agent_name:
        soul_content = load_soul(agent_name)
    if soul_content:
        # 直接执行模式：SOUL.md 提供角色和专业知识，但输出格式以下方规则为准
        # （SOUL.md 里的 Orchestration Protocol 描述的是多Agent汇报结构，
        #  在直接执行模式下统一用 final_answer + markdown，由编排层负责转发给用户）
        system = (
            soul_content
            + "\n\n---\n## 当前执行模式：直接执行（DIRECT EXECUTION MODE）\n"
            + "以上角色定义适用。但在本次执行中，你通过工具循环完成任务，**不需要返回 JSON 结构体**。\n"
            + "最终结果用 `final_answer` 工具以 Markdown 格式输出，由编排层统一处理后呈现给用户。\n"
            + "严禁向用户提问。遇到信息缺口时，基于最高置信路径自行推断后继续。\n\n"
            + "## 可用工具\n" + tools_block
            + '\n\n## 工具调用格式\n每次只调用一个工具，输出严格的 JSON。\n'
            + '{"action": "web_search", "args": {"query": "..."}}\n'
            + '{"action": "final_answer", "args": {"markdown": "最终 Markdown 结果"}}'
        )
    else:
        system = AGENT_SYSTEM.replace("{tools_block}", tools_block)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"## Brief\n{brief}\n\n## 原始输入\n{content}"},
    ]

    collected = []  # 收集每轮有效输出，LLM 中途失败时降级返回

    for turn in range(max_turns):
        raw = llm_call(messages)
        if not raw:
            print(f"[agent] LLM call failed at turn {turn+1}, degrading to collected output")
            # 降级：用已收集的工具结果拼成摘要
            if collected:
                return "（搜索中断，以下为已收集信息）\n\n" + "\n\n---\n\n".join(collected[-3:])
            return f"LLM call failed at turn {turn+1}"

        # parse action
        action_data = _parse_action(raw)
        if not action_data:
            # no valid action — treat as final answer
            return raw

        action = action_data.get("action", "")
        args = action_data.get("args", {})

        print(f"[agent] turn {turn+1}: {action} {json.dumps(args, ensure_ascii=False)[:80]}")
        if todo_id: progress_emit(todo_id, f"turn {turn+1} · {action}")

        if action == "final_answer":
            md = args.get("markdown", raw)
            # 剥离推理模型的 <think>...</think> 块
            md = re.sub(r'<think>.*?</think>', '', md, flags=re.S).strip()
            return md

        # risk gate check — 照搬 SATH: "shell_run not allowed (risk gate)"
        if not check_risk_gate(action):
            tool_result = f"{action} not allowed (risk gate)"
            print(f"[agent] BLOCKED by risk gate: {action}")
        elif action in TOOL_DISPATCH:
            tool_result = TOOL_DISPATCH[action](args)
        else:
            tool_result = f"unknown action: {action}"

        # 截断工具返回，防止 messages 过长导致 API 失败
        tool_result_trimmed = tool_result[:4000] if len(tool_result) > 4000 else tool_result
        if tool_result_trimmed and action in ('web_search', 'web_fetch'):
            collected.append(f"[{action}]\n{tool_result_trimmed[:1500]}")

        # 滑动窗口：保留 system + 首条 user + 最近 8 条消息，避免超长上下文
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": f"工具 `{action}` 返回结果:\n\n{tool_result_trimmed}"})
        if len(messages) > 10:  # system(1) + init_user(1) + 最近8条
            messages = messages[:2] + messages[-8:]

    # max turns reached — force final answer
    messages.append({"role": "user", "content": "已达到最大轮次，请立即用 final_answer 输出当前已有的结果。"})
    raw = llm_call(messages)
    if raw:
        action_data = _parse_action(raw)
        if action_data and action_data.get("action") == "final_answer":
            return action_data.get("args", {}).get("markdown", raw)
    return raw or "agent failed"

def _parse_action(text):
    """从 LLM 输出中提取 action JSON — 处理 <think> 标签和嵌套引号"""
    # strip <think>...</think>
    clean = re.sub(r'<think>.*?</think>', '', text, flags=re.S).strip()
    if not clean:
        clean = text
    # fix double braces {{ → {
    clean = clean.replace('{{', '{').replace('}}', '}')
    # try direct parse
    try:
        d = json.loads(clean)
        if "action" in d: return d
    except: pass
    # try find action json — allow nested braces for args
    m = re.search(r'\{\s*"action"\s*:\s*"[^"]+"\s*,\s*"args"\s*:\s*\{[^}]*\}\s*\}', clean)
    if m:
        try: return json.loads(m.group())
        except: pass
    # simpler pattern
    m = re.search(r'\{[^{}]*"action"[^{}]*\}', clean)
    if m:
        try: return json.loads(m.group())
        except: pass
    # ```json block
    m = re.search(r'```json\s*(\{.*?\})\s*```', clean, re.S)
    if m:
        try: return json.loads(m.group(1))
        except: pass
    # last resort: find in original text (with think tags stripped)
    m = re.search(r'"action"\s*:\s*"(final_answer|web_search|web_fetch)"', clean)
    if m:
        # reconstruct minimal json
        action = m.group(1)
        if action == "final_answer":
            # extract markdown
            md = re.search(r'"markdown"\s*:\s*"(.*?)"', clean, re.S)
            return {"action": "final_answer", "args": {"markdown": md.group(1) if md else clean}}
        elif action == "web_search":
            q = re.search(r'"query"\s*:\s*"(.*?)"', clean)
            return {"action": "web_search", "args": {"query": q.group(1) if q else ""}}
        elif action == "web_fetch":
            u = re.search(r'"url"\s*:\s*"(.*?)"', clean)
            return {"action": "web_fetch", "args": {"url": u.group(1) if u else ""}}
    return None

# ══════════════════════════════════════════════════════════
# Pipeline — 照搬 SATH brain/pipeline.py
# ══════════════════════════════════════════════════════════

def classify_todo(content, context_text=None, topology=None, ctx_snap=None):
    """意图分类。ctx_snap 为 build_context_snapshot() 返回值，若不传则内部构建。"""
    persona = get_persona()
    persona_block = f"角色: {persona.get('role', '未设置')}"
    if persona.get('current_focus'): persona_block += f"\n当前关注: {persona['current_focus']}"
    if persona.get('typical_work'): persona_block += f"\n典型工作: {persona['typical_work']}"
    if persona.get('tools'): persona_block += f"\n常用工具: {persona['tools']}"

    system = CLASSIFY_SYSTEM.replace("{persona_block}", persona_block)

    # ── 统一上下文注入（上下文层）──
    if ctx_snap is None:
        ctx_snap = build_context_snapshot(content=content)
    ctx_blocks = context_snapshot_to_blocks(ctx_snap)
    if ctx_blocks:
        system += "\n\n## 用户认知（从历史行为学习，用于推断信息缺口）\n" + "\n\n".join(ctx_blocks)

    user_parts = []
    if context_text:
        user_parts.append(f"## 当前环境\n{context_text}")
    loc = ctx_snap.get('location', {})
    if loc.get('city') or loc.get('region'):
        user_parts.append(f"## 位置\n{loc.get('city', '')} {loc.get('region', '')}")

    # 注入拓扑分解结果
    if topology:
        topo_lines = [f"## 拓扑分析",
                      f"场景: {topology['scene']}",
                      f"活跃维度: {topology['dimension_summary']}"]
        if topology['hints']:
            topo_lines.append("维度要求:")
            topo_lines.extend(f"  - {h}" for h in topology['hints'])
        if topology['suggested_tools']:
            topo_lines.append(f"推荐工具: {', '.join(topology['suggested_tools'])}")
        user_parts.append("\n".join(topo_lines))

    user_parts.append(f"## 用户输入\n来源: web\n时间: {datetime.now(timezone.utc).isoformat()}\n内容: {content}")
    user_parts.append("请分析并输出结构化 JSON。")

    messages = [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user_parts)}]
    raw = llm_call(messages, response_format={"type": "json_object"})
    if not raw: return None
    try: return json.loads(raw)
    except:
        start = raw.find('{'); end = raw.rfind('}') + 1
        if start >= 0 and end > start:
            try: return json.loads(raw[start:end])
            except: pass
    return None

def ingest_pipeline(todo_id, content):
    """满血 pipeline: focus → context → classify → DAG agent → obsidian → day_context"""
    progress_emit(todo_id, 'pipeline started')
    print(f"[pipeline] start: {todo_id} | {content[:60]}")
    db = get_db()

    # Step 0: Focus capture — 照搬 SATH sath://focus-captured
    focus = capture_focus()
    if focus:
        progress_emit(todo_id, f"focus · {focus['app']}")
        write_day_context('focus', f"{focus['app']} — {focus['title']}", todo_id=todo_id)

    # Step 1: ActivityWatch 环境感知
    progress_emit(todo_id, 'context · sensing')
    ctx_cfg = get_context_config()
    ctx_minutes = ctx_cfg.get('window_minutes', 5) if ctx_cfg.get('enabled') else 5
    context_text = get_activity_context(ctx_minutes)
    if context_text:
        print(f"[pipeline] context: {context_text[:80]}...")
        db.execute("UPDATE todos SET context_snapshot=?, updated_at=? WHERE id=?",
                   (context_text, datetime.now(timezone.utc).isoformat(), todo_id))
        db.commit()
        progress_emit(todo_id, 'context · done', 'done')
    else:
        progress_emit(todo_id, 'context · skipped', 'done')

    # Step 1.5: 拓扑分解 — 规则层，纯正则，< 1ms
    topo = topology_decompose(content)
    progress_emit(todo_id, f"topo · {topo['scene']} · {topo['dimension_summary']}", 'done')
    print(f"[pipeline] topo: scene={topo['scene']} dims={topo['dimension_summary']}")

    # Step 1.8: 统一上下文快照（上下文层）— 全 pipeline 共用
    progress_emit(todo_id, 'context · assembling')
    ctx_snap = build_context_snapshot(content=content)
    print(f"[pipeline] context_snap: loc={ctx_snap.get('location',{}).get('city','-')} "
          f"related={len(ctx_snap.get('related_todos',[]))}")

    # Step 2: classify（注入统一上下文快照）
    progress_emit(todo_id, 'classify · thinking')
    classified = classify_todo(content, context_text, topology=topo, ctx_snap=ctx_snap)
    if not classified:
        db.execute("UPDATE todos SET classify_status='failed', updated_at=? WHERE id=?",
                   (datetime.now(timezone.utc).isoformat(), todo_id))
        db.commit(); db.close()
        progress_emit(todo_id, 'classify · failed', 'err')
        print(f"[pipeline] classify failed: {todo_id}")
        return

    # Step 3: update DB with classify result
    now = datetime.now(timezone.utc).isoformat()
    title = classified.get('title', '')
    tags = json.dumps(classified.get('tags', []), ensure_ascii=False)
    types = json.dumps([classified.get('category', 'other')], ensure_ascii=False)
    priority = classified.get('priority', 0)
    due_at = classified.get('due_at')
    confidence = classified.get('confidence', 0.5)
    plan = json.dumps(classified, ensure_ascii=False) if classified.get('pipeline') else None

    db.execute("""UPDATE todos SET
        types=?, confidence=?, title=COALESCE(?, title), tags=?,
        due_at=COALESCE(?, due_at), priority=?,
        classify_status='done', plan=?, updated_at=?
        WHERE id=?""",
        (types, confidence, title, tags, due_at, priority, plan, now, todo_id))
    db.commit()
    progress_emit(todo_id, f"classify · {classified.get('intent')} · {title[:20]}", 'done')
    write_day_context('classify', f"{classified.get('intent')}: {title}", todo_id=todo_id)
    print(f"[pipeline] classified: {todo_id} → {classified.get('intent')} | {title}")

    # Step 4: run agents with DAG depends_on
    pipeline = classified.get('pipeline', [])
    if pipeline:
        db.execute("UPDATE todos SET agent_status='running', updated_at=? WHERE id=?", (now, todo_id))
        db.commit()

        for i, step in enumerate(pipeline):
            step['_idx'] = i
        completed = {}
        remaining = list(pipeline)
        outputs = []

        while remaining:
            ready = []
            for step in remaining:
                deps = step.get('depends_on', [])
                if not deps:
                    ready.append(step)
                else:
                    all_done = all(
                        (d in completed) if isinstance(d, int) else
                        any(completed.get(s['_idx']) for s in pipeline if s.get('agent') == d)
                        for d in deps
                    )
                    if all_done:
                        ready.append(step)
            if not ready:
                ready = [remaining[0]]

            for step in ready:
                brief = step.get('brief', '')
                display = step.get('display_name', step.get('agent', ''))
                extra_tools = step.get('extra_tools', [])
                agent_name = step.get('agent')
                deps = step.get('depends_on', [])
                if deps and completed:
                    dep_context = "\n\n".join(f"[前序结果: {pipeline[d].get('display_name','')}]\n{completed[d]}"
                                              for d in deps if isinstance(d, int) and d in completed)
                    if dep_context:
                        brief = brief + "\n\n## 前序 Agent 输出\n" + dep_context

                # 位置兜底注入：如果 brief 没有城市信息且有搜索工具，自动追加用户位置
                if 'web_search' in extra_tools or 'web_fetch' in extra_tools:
                    loc = ctx_snap.get('location', get_location())
                    loc_str = loc.get('city', '') or loc.get('region', '')
                    if loc_str and loc_str not in brief:
                        brief = f"【用户当前位置：{loc_str}】请优先搜索该位置周边信息，不要返回无关城市的数据。\n\n" + brief

                # 记忆层注入：搜索相关历史记忆，追加到 brief
                relevant_mems = search_relevant_memories(content + ' ' + brief[:60], limit=4)
                if relevant_mems:
                    mem_block = "\n".join(f"- {m['content']}" for m in relevant_mems)
                    brief = brief + f"\n\n## 相关历史记忆（供参考）\n{mem_block}"
                    print(f"[pipeline] injected {len(relevant_mems)} memories into brief")

                progress_emit(todo_id, f"agent · {display}")
                print(f"[pipeline] agent step {step['_idx']}: {display} | {brief[:60]}")
                result = run_agent_with_tools(brief, content, extra_tools=extra_tools, agent_name=agent_name, todo_id=todo_id)
                completed[step['_idx']] = result or ''
                if result:
                    outputs.append(f"### {display}\n{result}")
                progress_emit(todo_id, f"agent · {display} · done", 'done')
                remaining.remove(step)

        agent_output = "\n\n".join(outputs) if outputs else None
        now2 = datetime.now(timezone.utc).isoformat()
        db.execute("UPDATE todos SET agent_output=?, agent_status='done', agent_error=NULL, updated_at=? WHERE id=?",
                   (agent_output, now2, todo_id))
        db.commit()
        print(f"[pipeline] done: {todo_id} | {len(outputs)} outputs, {sum(len(o) for o in outputs)} chars")

        # Step 5: Obsidian sync
        if agent_output:
            md = f"# {title or content}\n\n{agent_output}"
            obs_result = obsidian_write(todo_id, title or content, md)
            print(f"[pipeline] {obs_result}")

        # Step 5.5: 记忆层 — 异步从结果中提取语义记忆
        if agent_output:
            threading.Thread(
                target=extract_memories_async,
                args=(todo_id, title or content, content, agent_output),
                daemon=True
            ).start()

        write_day_context('pipeline_done', f"{title}: {len(outputs)} agents", todo_id=todo_id)
    else:
        db.execute("UPDATE todos SET agent_status='done', updated_at=? WHERE id=?", (now, todo_id))
        db.commit()
        print(f"[pipeline] no pipeline: {todo_id}")

    progress_emit(todo_id, 'completed', 'done')

    # ── 微信回复：pipeline 完成后向发送方推送要点摘要 ──
    wx_user_id = _wx_reply_map.pop(todo_id, None)
    if wx_user_id:
        try:
            # 从 DB 读最新 title / agent_output
            _rdb = get_db()
            _row = _rdb.execute(
                "SELECT title, content, agent_output FROM todos WHERE id=?", (todo_id,)
            ).fetchone()
            _rdb.close()
            if _row:
                summary = _summarize_for_wechat(
                    _row['title'], _row['content'], _row['agent_output']
                )
                threading.Thread(target=_wx_send_reply, args=(wx_user_id, summary), daemon=True).start()
        except Exception as _e:
            print(f"[wx_reply] build summary failed: {_e}")

    db.close()

# ══════════════════════════════════════════════════════════
# HTTP Handler
# ══════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json(self, code, data):
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        if path == '/api/health':
            self._json(200, {'status': 'ok', 'db': DB_PATH, 'service': 'sath-bridge-full'})
        elif path == '/api/todos':
            db = get_db()
            rows = db.execute('SELECT * FROM todos ORDER BY created_at DESC').fetchall()
            db.close()
            self._json(200, [dict(r) for r in rows])
        elif path.startswith('/api/todos/'):
            tid = path.split('/')[-1]; db = get_db()
            row = db.execute('SELECT * FROM todos WHERE id=?', (tid,)).fetchone(); db.close()
            self._json(200, dict(row) if row else {'error': 'not found'})
        elif path == '/api/settings':
            db = get_db()
            rows = db.execute('SELECT * FROM settings').fetchall(); db.close()
            self._json(200, {r['key']: r['value'] for r in rows})
        elif path == '/api/context':
            ctx = get_activity_context()
            self._json(200, {'context': ctx or 'ActivityWatch not available'})
        elif path == '/api/agents':
            self._json(200, list_agents())
        elif path == '/api/reminders':
            self._json(200, check_reminders())
        elif path.startswith('/api/progress/'):
            tid = path.split('/')[-1]
            self._json(200, progress_get(tid))
        elif path.startswith('/api/progress_stream/'):
            # SSE 推送 — 照搬 SATH sath://pipeline-progress
            tid = path.split('/')[-1]
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            last_len = 0
            try:
                for _ in range(600):  # 最多 5 分钟
                    logs = progress_get(tid)
                    if len(logs) > last_len:
                        for log in logs[last_len:]:
                            data = json.dumps(log, ensure_ascii=False)
                            self.wfile.write(f"data: {data}\n\n".encode())
                            self.wfile.flush()
                        last_len = len(logs)
                        # 如果最后一条是 completed，结束流
                        if logs[-1].get('status') == 'done' and 'completed' in logs[-1].get('text', ''):
                            self.wfile.write(b"data: {\"done\":true}\n\n")
                            self.wfile.flush()
                            return
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == '/api/kb_config':
            self._json(200, get_kb_config())
        elif path.startswith('/api/open_privacy'):
            ptype = parse_qs(urlparse(self.path).query).get('type', [''])[0]
            urls = {
                'location': 'x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices',
                'accessibility': 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility',
                'automation': 'x-apple.systempreferences:com.apple.preference.security?Privacy_Automation',
            }
            url = urls.get(ptype)
            if url:
                subprocess.Popen(['open', url])
                self._json(200, {'ok': True})
            else:
                self._json(400, {'error': 'unknown type'})
        elif path == '/api/refresh_location':
            loc = refresh_location()
            self._json(200, loc or {'error': 'location unavailable'})
        elif path == '/api/memories':
            limit = int(qs.get('limit', ['50'])[0])
            db = get_db()
            rows = db.execute(
                "SELECT * FROM memories ORDER BY access_count DESC, created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            total = db.execute("SELECT COUNT(*) as n FROM memories").fetchone()['n']
            db.close()
            self._json(200, {'total': total, 'items': [dict(r) for r in rows]})
        elif path == '/api/memories/distill':
            threading.Thread(target=distill_memories, daemon=True).start()
            self._json(200, {'ok': True, 'msg': '记忆蒸馏已在后台启动'})
        elif path == '/api/resident/status':
            db = get_db()
            total_mems = db.execute("SELECT COUNT(*) as n FROM memories").fetchone()['n']
            upcoming = db.execute(
                "SELECT id, title, due_at FROM todos WHERE due_at IS NOT NULL "
                "AND due_at > ? AND classify_status='done' ORDER BY due_at ASC LIMIT 5",
                (datetime.now(timezone.utc).isoformat(),)
            ).fetchall()
            last_brief = db.execute(
                "SELECT created_at FROM resident_log WHERE action='morning_brief' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            db.close()
            self._json(200, {
                'memories_count': total_mems,
                'upcoming_tasks': [dict(r) for r in upcoming],
                'last_morning_brief': last_brief['created_at'] if last_brief else None,
                'scan_interval_seconds': 60,
            })
        elif path == '/api/focus':
            f = capture_focus()
            self._json(200, f or {'app': '', 'title': ''})
        elif path == '/api/wx_qrcode':
            # 代理 iLink 获取二维码
            try:
                req = urllib.request.Request('https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3')
                with urllib.request.urlopen(req, timeout=10) as r:
                    self._json(200, json.loads(r.read()))
            except Exception as e:
                self._json(500, {'error': str(e)})
        elif path.startswith('/api/wx_qr_status'):
            qrcode = parse_qs(urlparse(self.path).query).get('qrcode', [''])[0]
            try:
                req = urllib.request.Request(f'https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode)}')
                with urllib.request.urlopen(req, timeout=10) as r:
                    self._json(200, json.loads(r.read()))
            except Exception as e:
                self._json(500, {'error': str(e)})
        else:
            self._json(404, {'error': 'not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')

        # ── 用户位置（CoreLocation 推送）──
        if path == '/api/context/location':
            city    = body.get('city', '')
            region  = body.get('region', '')
            country = body.get('country', '')
            lat     = body.get('lat', 0)
            lng     = body.get('lng', 0)
            loc_val = json.dumps({'city': city, 'region': region, 'country': country,
                                  'lat': lat, 'lng': lng}, ensure_ascii=False)
            db = get_db()
            db.execute("INSERT INTO settings (key,value) VALUES ('location',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (loc_val,))
            db.commit(); db.close()
            print(f"[location] 已更新: {city}, {region}, {country} ({lat:.4f},{lng:.4f})")
            self._json(200, {'ok': True})

        # ── 知识库配置保存（kb_save 和 kb_config POST 均写入同一处）──
        elif path == '/api/kb_save' or path == '/api/kb_config':
            save_kb_config(body)
            self._json(200, {'ok': True})

        # ── 前端推送知识库扫描结果缓存（IMA / 外部源）──
        elif path == '/api/kb_scan':
            sources = body.get('sources', [])
            # 持久化到 settings 供后续 RAG 检索使用
            db = get_db()
            existing = db.execute("SELECT value FROM settings WHERE key='kb_scan_cache'").fetchone()
            cache = json.loads(existing['value']) if existing else []
            # 合并去重（按 source 字段）
            existing_names = {s.get('source') for s in cache}
            for s in sources:
                if s.get('source') not in existing_names:
                    cache.append(s)
            db.execute(
                "INSERT INTO settings (key,value) VALUES ('kb_scan_cache',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(cache, ensure_ascii=False),)
            )
            db.commit(); db.close()
            print(f"[kb_scan] cached {len(sources)} new sources, total={len(cache)}")
            self._json(200, {'ok': True, 'cached': len(sources), 'total': len(cache)})

        # ── 打开 Finder 窗口（POST，body 含 path）──
        elif path == '/api/open_finder':
            p = body.get('path', '')
            if p and os.path.exists(os.path.expanduser(p)):
                subprocess.Popen(['open', os.path.expanduser(p)])
                self._json(200, {'ok': True})
            else:
                self._json(200, {'ok': False, 'error': '路径不存在: ' + p})

        # ── 飞书 OAuth code 换 token ──
        elif path == '/api/feishu_exchange':
            code      = body.get('code', '')
            app_id    = body.get('app_id', '')
            app_secret = body.get('app_secret', '')
            if not code or not app_id or not app_secret:
                self._json(400, {'ok': False, 'error': '缺少参数'}); return
            try:
                token = _feishu_exchange_code(app_id, app_secret, code)
                self._json(200, {'ok': True, 'token': token})
            except Exception as e:
                self._json(200, {'ok': False, 'error': str(e)})

        # ── 知识库连通性测试（一次一个平台）──
        elif path == '/api/kb_test':
            kb_type = body.get('type', '')
            cfg = body.get('cfg', {})          # 前端直接把当前填写的配置传过来
            try:
                if kb_type == 'notion':
                    results = kb_scan_notion(cfg)
                elif kb_type == 'obsidian':
                    results = kb_scan_obsidian_kb(cfg)
                elif kb_type == 'yuque':
                    results = kb_scan_yuque(cfg)
                elif kb_type == 'local':
                    results = kb_scan_local(cfg)
                elif kb_type == 'wolai':
                    results = kb_scan_wolai(cfg)
                elif kb_type == 'shimo':
                    results = kb_scan_shimo(cfg)
                elif kb_type == 'ima':
                    results = kb_scan_ima(cfg)
                elif kb_type == 'feishu':
                    results = kb_scan_feishu(cfg)
                else:
                    self._json(400, {'ok': False, 'error': f'暂不支持: {kb_type}'}); return
                self._json(200, {'ok': True, 'docs': len(results[0].get('docs', [])) if results else 0})
            except Exception as e:
                self._json(200, {'ok': False, 'error': str(e)})

        # ── Obsidian 写入测试 ──
        elif path == '/api/obs_test':
            obs = get_setting('obsidian_config') or {}
            if not obs.get('vault_path'):
                self._json(200, {'ok': False, 'error': '未配置 Vault 路径'})
            else:
                try:
                    vault = os.path.expanduser(obs['vault_path'])
                    sub = obs.get('subfolder', 'SATH')
                    dirpath = os.path.join(vault, sub)
                    os.makedirs(dirpath, exist_ok=True)
                    test_path = os.path.join(dirpath, '_SATH_TEST.md')
                    with open(test_path, 'w') as f:
                        f.write(f'# SATH 写入测试\n\n测试时间: {datetime.now(timezone.utc).isoformat()}\n\n此文件可安全删除。')
                    self._json(200, {'ok': True, 'path': test_path})
                except Exception as e:
                    self._json(200, {'ok': False, 'error': str(e)})

        # ── Obsidian 全量同步：所有未完成任务写入 vault ──
        elif path == '/api/obs_sync':
            obs = get_setting('obsidian_config') or {}
            if not obs.get('vault_path'):
                self._json(200, {'ok': False, 'error': '未配置 Vault 路径'})
            else:
                db = get_db()
                rows = db.execute(
                    "SELECT id, content, title, agent_output, status FROM todos "
                    "WHERE status NOT IN ('done', 'archived')"
                ).fetchall()
                db.close()
                count = 0
                errors = []
                for r in rows:
                    title = r['title'] or r['content'] or r['id']
                    if r['agent_output']:
                        md = f"# {title}\n\n{r['agent_output']}"
                    else:
                        md = f"# {title}\n\n> {r['content']}\n"
                    result = obsidian_write(r['id'], title, md)
                    if result.startswith('[obsidian]'):
                        count += 1
                    else:
                        errors.append(result)
                if errors:
                    self._json(200, {'ok': False, 'synced': count, 'error': errors[0]})
                else:
                    self._json(200, {'ok': True, 'synced': count})

        elif path == '/api/ingest':
            content = body.get('content', '').strip()
            if not content: self._json(400, {'error': 'empty'}); return
            todo_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            db = get_db()
            db.execute("INSERT INTO todos (id, content, types, tags, classify_status, agent_status, origin, created_at, updated_at) VALUES (?, ?, '[]', '[]', 'pending', 'none', 'web', ?, ?)",
                       (todo_id, content, now, now))
            db.commit(); db.close()
            threading.Thread(target=ingest_pipeline, args=(todo_id, content), daemon=True).start()
            self._json(200, {'ok': True, 'id': todo_id, 'status': 'pipeline_started'})
        elif path == '/api/reclassify':
            # 照搬 SATH reclassify_todo: 只重跑 classify，不跑 agent
            tid = body.get('todo_id', '')
            if not tid: self._json(400, {'error': 'missing todo_id'}); return
            db = get_db()
            row = db.execute('SELECT content, context_snapshot FROM todos WHERE id=?', (tid,)).fetchone()
            db.close()
            if not row: self._json(404, {'error': 'todo not found'}); return
            def _reclassify(todo_id, content, ctx):
                progress_emit(todo_id, 'reclassify · thinking')
                db2 = get_db()
                db2.execute("UPDATE todos SET classify_status='pending', updated_at=? WHERE id=?",
                            (datetime.now(timezone.utc).isoformat(), todo_id))
                db2.commit(); db2.close()
                classified = classify_todo(content, ctx)
                if not classified:
                    progress_emit(todo_id, 'reclassify · failed', 'err')
                    return
                now = datetime.now(timezone.utc).isoformat()
                db3 = get_db()
                db3.execute("""UPDATE todos SET
                    types=?, confidence=?, title=COALESCE(?, title), tags=?,
                    due_at=COALESCE(?, due_at), priority=?,
                    classify_status='done', plan=?, updated_at=? WHERE id=?""",
                    (json.dumps([classified.get('category','other')], ensure_ascii=False),
                     classified.get('confidence',0.5), classified.get('title',''),
                     json.dumps(classified.get('tags',[]), ensure_ascii=False),
                     classified.get('due_at'), classified.get('priority',0),
                     json.dumps(classified, ensure_ascii=False) if classified.get('pipeline') else None,
                     now, todo_id))
                db3.commit(); db3.close()
                progress_emit(todo_id, f"reclassify · {classified.get('intent')} · done", 'done')
            threading.Thread(target=_reclassify, args=(tid, row['content'], row['context_snapshot']), daemon=True).start()
            self._json(200, {'ok': True, 'id': tid, 'status': 'reclassify_started'})

        elif path == '/api/run_agent':
            tid = body.get('todo_id', '')
            if not tid: self._json(400, {'error': 'missing todo_id'}); return
            db = get_db()
            row = db.execute('SELECT content FROM todos WHERE id=?', (tid,)).fetchone()
            db.close()
            if not row: self._json(404, {'error': 'todo not found'}); return
            threading.Thread(target=ingest_pipeline, args=(tid, row['content']), daemon=True).start()
            self._json(200, {'ok': True, 'id': tid, 'status': 'pipeline_restarted'})
        elif path == '/api/wx_poll':
            # 代理 iLink getupdates
            try:
                headers = {'Content-Type':'application/json','AuthorizationType':'ilink_bot_token','Authorization':'Bearer '+body.get('bot_token','')}
                data = json.dumps({'base_info':{'channel_version':'1.0.2'},'get_updates_buf':body.get('get_updates_buf','')}).encode()
                req = urllib.request.Request('https://ilinkai.weixin.qq.com/ilink/bot/getupdates', data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as r:
                    self._json(200, json.loads(r.read()))
            except Exception as e:
                self._json(200, {'msgs':[],'error':str(e)})
        elif path == '/api/wx_send':
            # 代理 iLink sendmessage
            try:
                headers = {'Content-Type':'application/json','AuthorizationType':'ilink_bot_token','Authorization':'Bearer '+body.get('bot_token','')}
                msg_body = {'base_info':{'channel_version':'1.0.2'},'msg':{
                    'to_user_id':body.get('to_user_id',''),'client_id':'sath-'+str(int(time.time()*1000)),
                    'message_type':2,'message_state':2,'context_token':body.get('context_token',''),
                    'item_list':[{'type':1,'text_item':{'text':body.get('text','')}}]
                }}
                data = json.dumps(msg_body).encode()
                req = urllib.request.Request('https://ilinkai.weixin.qq.com/ilink/bot/sendmessage', data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as r:
                    self._json(200, json.loads(r.read()))
            except Exception as e:
                self._json(500, {'error':str(e)})
        # ── 从任务详情发送完整 AI 内容到微信 ──────────────
        elif path == '/api/wx_send_content':
            tid = body.get('todo_id', '')
            bot_token    = body.get('bot_token', '')
            to_user_id   = body.get('to_user_id', '')
            context_token = body.get('context_token', '')
            if not tid: self._json(400, {'error': 'missing todo_id'}); return
            if not bot_token:
                self._json(400, {'error': '微信未配置，请先在设置中绑定微信'}); return
            # 读取任务内容
            db = get_db()
            row = db.execute(
                "SELECT title, content, agent_output FROM todos WHERE id=?", (tid,)
            ).fetchone()
            db.close()
            if not row or not row['agent_output']:
                self._json(400, {'error': '该任务暂无 AI 内容'}); return
            # 构造发送内容：标题 + 完整 agent_output
            title = row['title'] or row['content'] or ''
            agent_out = (row['agent_output'] or '').replace('\\n', '\n')
            # 去除 <think> 块
            agent_out = re.sub(r'<think>[\s\S]*?</think>', '', agent_out).strip()
            full_text = f"【{title[:40]}】\n\n{agent_out}"
            # 微信单条消息上限约 2000 字，超出分段发送
            MAX_CHUNK = 1800
            chunks = [full_text[i:i+MAX_CHUNK] for i in range(0, len(full_text), MAX_CHUNK)]
            try:
                for idx, chunk in enumerate(chunks):
                    suffix = f"\n（{idx+1}/{len(chunks)}）" if len(chunks) > 1 else ''
                    msg_body = {'base_info':{'channel_version':'1.0.2'},'msg':{
                        'to_user_id': to_user_id,
                        'client_id': f'sath-{int(time.time()*1000)}-{idx}',
                        'message_type': 2, 'message_state': 2,
                        'context_token': context_token,
                        'item_list': [{'type':1,'text_item':{'text': chunk + suffix}}]
                    }}
                    req = urllib.request.Request(
                        'https://ilinkai.weixin.qq.com/ilink/bot/sendmessage',
                        data=json.dumps(msg_body).encode(),
                        headers={'Content-Type':'application/json',
                                 'AuthorizationType':'ilink_bot_token',
                                 'Authorization':'Bearer '+bot_token}
                    )
                    urllib.request.urlopen(req, timeout=10)
                self._json(200, {'ok': True, 'chunks': len(chunks)})
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})

        elif path == '/api/feedback':
            # 提交反馈：{todo_id, diffs: {field: {ai: X, user: Y}}}
            tid = body.get('todo_id', '')
            diffs = body.get('diffs', {})
            if not tid or not diffs: self._json(400, {'error': 'missing todo_id or diffs'}); return
            submit_feedback(tid, diffs)
            self._json(200, {'ok': True, 'count': get_feedback_count()})

        elif path == '/api/feedback_history':
            # 查询最近的表态历史（每个 todo 只取最新一条）
            limit = body.get('limit', 50)
            db = get_db()
            rows = db.execute(
                '''SELECT f.todo_id, f.verdict, MAX(f.created_at) as created_at,
                          t.content
                   FROM feedback f
                   LEFT JOIN todos t ON t.id = f.todo_id
                   GROUP BY f.todo_id
                   ORDER BY created_at DESC LIMIT ?''', (limit,)
            ).fetchall()
            db.close()
            items = []
            for r in rows:
                content = r['content'] or ''
                title = content.split('\n')[0][:60] if content else '(已删除)'
                items.append({'todo_id': r['todo_id'],
                              'verdict': json.loads(r['verdict']) if r['verdict'] else {},
                              'title': title, 'created_at': r['created_at']})
            self._json(200, {'ok': True, 'items': items})

        elif path == '/api/feedback_clear':
            # 清除某 todo 的全部表态，放回队列
            tid = body.get('todo_id', '')
            if not tid: self._json(400, {'error': 'missing todo_id'}); return
            db = get_db()
            db.execute('DELETE FROM feedback WHERE todo_id = ?', (tid,))
            db.commit(); db.close()
            self._json(200, {'ok': True})
        elif path == '/api/kb_scan_now':
            # 立即扫描所有知识库，返回摘要（不触发蒸馏）
            def _do_scan():
                return kb_scan_all()
            try:
                sources = _do_scan()
                total_docs = sum(len(s.get('docs', [])) for s in sources)
                self._json(200, {'ok': True, 'sources': sources, 'total_docs': total_docs})
            except Exception as e:
                self._json(200, {'ok': False, 'error': str(e), 'sources': [], 'total_docs': 0})
        elif path == '/api/feedback_stats':
            # 反馈统计：意图准确率 + 内容有效率
            db = get_db()
            rows = db.execute("SELECT verdict FROM feedback ORDER BY created_at DESC LIMIT 500").fetchall()
            db.close()
            intent_accurate = intent_inaccurate = 0
            content_effective = content_ineffective = 0
            intent_only = content_only = both = 0
            for r in rows:
                try:
                    d = json.loads(r['verdict'])
                    has_acc = 'accurate' in d
                    has_eff = 'effective' in d
                    if has_acc and has_eff: both += 1
                    elif has_acc: intent_only += 1
                    elif has_eff: content_only += 1
                    if has_acc:
                        if d['accurate'].get('user') == 'true': intent_accurate += 1
                        else: intent_inaccurate += 1
                    if has_eff:
                        if d['effective'].get('user') == 'true': content_effective += 1
                        else: content_ineffective += 1
                except: pass
            total = len(rows)
            self._json(200, {
                'total': total,
                'intentAccuracy': {
                    'accurate': intent_accurate, 'inaccurate': intent_inaccurate,
                    'rate': round(intent_accurate / (intent_accurate + intent_inaccurate) * 100) if (intent_accurate + intent_inaccurate) else None
                },
                'contentEffectiveness': {
                    'effective': content_effective, 'ineffective': content_ineffective,
                    'rate': round(content_effective / (content_effective + content_ineffective) * 100) if (content_effective + content_ineffective) else None
                },
                'breakdown': {'both': both, 'intent_only': intent_only, 'content_only': content_only}
            })
        elif path == '/api/distill':
            # 触发蒸馏
            def _do_distill():
                result = distill_preferences()
                if result:
                    db2 = get_db()
                    db2.execute("INSERT INTO settings (key,value) VALUES ('last_distill_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (str(time.time()),))
                    db2.commit(); db2.close()
                    print(f"[distill] completed")
                else: print(f"[distill] no data or failed")
            threading.Thread(target=_do_distill, daemon=True).start()
            self._json(200, {'ok': True, 'status': 'distill_started'})
        elif path == '/api/todos':
            db = get_db()
            now = datetime.now(timezone.utc).isoformat()
            tid = body.get('id', str(uuid.uuid4()))
            db.execute('''INSERT OR REPLACE INTO todos (id,content,title,notes,status,priority,due_at,tags,types,agent_output,agent_status,classify_status,origin,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (tid, body.get('content',''), body.get('title'), body.get('notes'), body.get('status','open'),
                 body.get('priority',0), body.get('due_at'), json.dumps(body.get('tags',[])), json.dumps(body.get('types',[])),
                 body.get('agent_output'), body.get('agent_status','none'), body.get('classify_status','pending'),
                 body.get('origin','web'), body.get('created_at',now), now))
            db.commit(); db.close()
            self._json(200, {'ok': True, 'id': tid})
        # ── v2: ClawBot 微信 Webhook（接收用户微信消息）──────
        elif path == '/webhook/wechat':
            """
            ClawBot 官方接口（2026年3月灰度开放）。
            消息格式: {from, type, content, timestamp}
            """
            wechat_from = body.get('from', '')
            msg_type = body.get('type', 'text')
            content = body.get('content', '').strip()
            msg_ts = body.get('timestamp', int(time.time()))
            if not content:
                self._json(400, {'error': 'empty content'}); return
            # 多模态标准化（文字直接处理，图片/语音/链接转文字）
            normalized = content
            if msg_type == 'image':
                normalized = f"[图片] {content}"   # OCR 在此处理（MVP: 占位）
            elif msg_type == 'voice':
                normalized = f"[语音] {content}"   # ASR 在此处理（MVP: 占位）
            elif msg_type == 'file':
                normalized = f"[文件] {content}"
            # 直接 ingest（ClawBot 单条消息，不走 buffer pool 以便立即回复）
            todo_id = str(uuid.uuid4())
            now_iso = datetime.now(timezone.utc).isoformat()
            db = get_db()
            db.execute("INSERT INTO todos (id,content,types,tags,classify_status,agent_status,origin,created_at,updated_at) VALUES (?,?,'[]','[]','pending','none','wechat',?,?)",
                       (todo_id, normalized, now_iso, now_iso))
            db.commit(); db.close()
            # 记录发件人，pipeline 完成后回复
            if wechat_from:
                _wx_reply_map[todo_id] = wechat_from
            threading.Thread(target=ingest_pipeline, args=(todo_id, normalized), daemon=True).start()
            self._json(200, {'ok': True, 'id': todo_id, 'status': 'pipeline_started'})

        # ── v2: 缓冲池推入接口（支持 PC/网页端）──────────
        elif path == '/api/buffer_push':
            # 兼容前端发 text 或 content 两种字段名
            content = (body.get('content') or body.get('text') or '').strip()
            source = body.get('source', 'manual')
            flush_now = bool(body.get('flush_now', False))
            client_todo_id = body.get('client_todo_id', '')  # 前端乐观 ID，优先复用
            wx_user_id = body.get('wx_user_id', '')          # iLink 来源时传入，pipeline 完成后回复
            if not content: self._json(400, {'error': 'empty'}); return
            ctx_snap = capture_focus() or {}
            if _buffer_pool is not None and not flush_now:
                _buffer_pool.push(content, source=source, context_snapshot=ctx_snap)
                self._json(200, {'ok': True, 'status': 'buffered', 'buffer_open': _buffer_pool.is_open()})
            else:
                # flush_now=True 或 buffer_pool 未初始化：直接走 ingest pipeline
                todo_id = client_todo_id if client_todo_id else str(uuid.uuid4())
                now_iso = datetime.now(timezone.utc).isoformat()
                db = get_db()
                # 若前端已乐观写入同 ID，用 INSERT OR IGNORE 避免重复
                db.execute(
                    "INSERT OR IGNORE INTO todos (id,content,types,tags,classify_status,agent_status,origin,created_at,updated_at) "
                    "VALUES (?,?,'[]','[]','pending','none',?,?,?)",
                    (todo_id, content, source, now_iso, now_iso)
                )
                db.commit(); db.close()
                # iLink 微信来源：记录发件人，pipeline 完成后回复
                if wx_user_id and source == 'wechat':
                    _wx_reply_map[todo_id] = wx_user_id
                threading.Thread(target=ingest_pipeline, args=(todo_id, content), daemon=True).start()
                self._json(200, {'ok': True, 'todo_id': todo_id, 'status': 'pipeline_started'})

        # ── v2: 获取缓冲池待推送结果 ──────────────────────
        elif path == '/api/pending_results':
            if _brain is not None:
                results = _brain.get_pending_results()
                self._json(200, {'ok': True, 'results': results, 'count': len(results)})
            else:
                self._json(200, {'ok': True, 'results': [], 'count': 0})

        # ── v2: 8状态反馈矩阵（接入蒸馏系统）─────────────
        elif path == '/api/feedback_matrix':
            """
            8 状态标注。比旧 /api/feedback 更完整，直接触发热蒸馏。
            body: {todo_id, effective: bool|null, accurate: bool|null, regret: bool}
            """
            tid = body.get('todo_id', '')
            effective = body.get('effective')   # true/false/null
            accurate = body.get('accurate')     # true/false/null
            regret = bool(body.get('regret', False))
            if not tid: self._json(400, {'error': 'missing todo_id'}); return
            # 触发蒸馏层处理
            try:
                from sath_source.brain.distillation import process_feedback
                threading.Thread(
                    target=process_feedback,
                    args=(DB_PATH, tid, effective, accurate, regret),
                    daemon=True,
                ).start()
            except ImportError:
                # fallback：写入旧 feedback 表
                verdict_parts = []
                if effective is True: verdict_parts.append('effective')
                elif effective is False: verdict_parts.append('ineffective')
                if accurate is True: verdict_parts.append('accurate')
                elif accurate is False: verdict_parts.append('inaccurate')
                if regret: verdict_parts = ['regret']
                verdict = '+'.join(verdict_parts) if verdict_parts else 'unknown'
                db = get_db()
                db.execute("INSERT OR IGNORE INTO feedback (todo_id, verdict, raw, created_at) VALUES (?,?,?,?)",
                           (tid, verdict, json.dumps(body), datetime.now(timezone.utc).isoformat()))
                db.commit(); db.close()
            self._json(200, {'ok': True, 'state': '+'.join([
                'effective' if effective else ('ineffective' if effective is False else ''),
                'accurate' if accurate else ('inaccurate' if accurate is False else ''),
                'regret' if regret else '',
            ]).strip('+')})

        # ── v2: 后悔药（缓冲窗口内撤回）──────────────────
        elif path == '/api/regret':
            tid = body.get('todo_id', '')
            if not tid: self._json(400, {'error': 'missing todo_id'}); return
            # 尝试通过编排 Agent 处理
            reverted = False
            if _orchestrator is not None:
                reverted = _orchestrator.handle_regret(tid)
            if not reverted:
                # fallback：直接标记取消
                db = get_db()
                db.execute("UPDATE todos SET status='cancelled', updated_at=? WHERE id=?",
                           (datetime.now(timezone.utc).isoformat(), tid))
                db.commit(); db.close()
                reverted = True
            # 触发热蒸馏（后悔药是高质量错误信号）
            try:
                from sath_source.brain.distillation import process_feedback
                threading.Thread(
                    target=process_feedback,
                    args=(DB_PATH, tid, None, None, True),
                    daemon=True,
                ).start()
            except ImportError:
                pass
            self._json(200, {'ok': True, 'reverted': reverted, 'todo_id': tid})

        # ── v2: 用户模型快照（蒸馏层输出）────────────────
        elif path == '/api/user_model':
            try:
                from sath_source.brain.pipeline import UserModel
                from pathlib import Path as _Path
                um = UserModel(_Path(DB_PATH))
                snapshot = um.load_snapshot(include_active=True)
            except Exception as e:
                snapshot = {}
                print(f"[user_model] UserModel failed: {e}")
            # 补充冷蒸馏结果（user_preferences）
            cold = get_setting('user_preferences') or {}
            if isinstance(cold, str):
                try: cold = json.loads(cold)
                except: cold = {}
            if cold:
                snapshot['cold_distill'] = cold
            self._json(200, {'ok': True, 'snapshot': snapshot})

        # ── v2: 热蒸馏手动触发 ────────────────────────────
        elif path == '/api/distill_hot':
            # 手动触发热蒸馏：取最近一条已分类任务作为输入，无需前端传具体数据
            raw_input = body.get('raw_input', '')
            classified = body.get('classified', {})
            # 若前端未传数据，从 DB 取最近一条已分类任务
            if not raw_input or not classified:
                try:
                    _db = get_db()
                    _row = _db.execute(
                        "SELECT content, plan FROM todos WHERE classify_status='done' ORDER BY updated_at DESC LIMIT 1"
                    ).fetchone()
                    _db.close()
                    if _row:
                        raw_input = _row['content'] or ''
                        try: classified = json.loads(_row['plan'] or '{}')
                        except: classified = {}
                except Exception: pass
            try:
                from sath_source.brain.distillation import hot_distill
                from pathlib import Path as _Path
                threading.Thread(
                    target=hot_distill,
                    args=(_Path(DB_PATH), raw_input or '(manual trigger)', classified or {}, body.get('source','manual'), None),
                    daemon=True,
                ).start()
                self._json(200, {'ok': True, 'status': 'hot_distill_started'})
            except ImportError:
                self._json(200, {'ok': False, 'error': 'distillation module not available'})

        else:
            self._json(404, {'error': 'not found'})

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith('/api/settings/'):
            key = path.split('/')[-1]
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            body = json.loads(raw or '{}')
            # Accept both {"value": "..."} and a bare JSON string
            value = body.get('value', '') if isinstance(body, dict) else (body if isinstance(body, str) else '')
            db = get_db()
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            db.commit(); db.close()
            print(f"[bridge] settings updated: {key} = {value[:60]}")
            self._json(200, {'ok': True})
        elif path.startswith('/api/todos/'):
            tid = path.split('/')[-1]
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
            db = get_db(); now = datetime.now(timezone.utc).isoformat()
            sets, vals = [], []
            for k in ('content','title','notes','status','priority','due_at','tags','types','agent_output','agent_status'):
                if k in body:
                    v = body[k]
                    if k in ('tags','types') and isinstance(v, list): v = json.dumps(v)
                    sets.append(f'{k}=?'); vals.append(v)
            sets.append('updated_at=?'); vals.append(now); vals.append(tid)
            db.execute(f'UPDATE todos SET {",".join(sets)} WHERE id=?', vals)
            db.commit(); db.close()
            self._json(200, {'ok': True})
        else: self._json(404, {'error': 'not found'})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith('/api/todos/'):
            tid = path.split('/')[-1]; db = get_db()
            db.execute('DELETE FROM todos WHERE id=?', (tid,)); db.commit(); db.close()
            self._json(200, {'ok': True})
        elif path.startswith('/api/memories/'):
            mid = path.split('/')[-1]; db = get_db()
            db.execute('DELETE FROM memories WHERE id=?', (mid,)); db.commit(); db.close()
            self._json(200, {'ok': True})
        else: self._json(404, {'error': 'not found'})

    def log_message(self, fmt, *args): print(f"[bridge] {args[0]}")

# ══════════════════════════════════════════════════════════
# v2: 全局单例（缓冲池 / Brain / 编排Agent）
# ══════════════════════════════════════════════════════════

_buffer_pool = None    # BufferPool 实例
_brain = None          # SATHBrain 实例
_orchestrator = None   # OrchestratorAgent 实例
_wx_reply_map = {}     # todo_id → wx_user_id，pipeline 完成后回复微信


def _summarize_for_wechat(title, content, agent_output):
    """
    提炼微信回复要点，控制在 ~250 字内：
      第一行：✓ 标题
      后续：最多 3 条 ## 章节标题作为要点
      无 agent_output 时只回标题+意图确认
    """
    lines = []
    display = (title or content or '').split('\n')[0][:40]
    lines.append(f"✓ {display}")

    if agent_output:
        # 去掉 <think> 块
        clean = re.sub(r'<think>[\s\S]*?</think>', '', agent_output).strip()
        # 提取 ## 级别标题
        headings = re.findall(r'^#{1,3}\s+(.+)$', clean, re.MULTILINE)
        if headings:
            bullets = headings[:3]
            lines.append('')
            for h in bullets:
                lines.append(f'· {h.strip()[:50]}')
            remaining = len(headings) - len(bullets)
            if remaining > 0:
                lines.append(f'  …还有 {remaining} 项')
        else:
            # 无标题则取正文前两句
            sentences = re.split(r'[。！？\n]', clean)
            sentences = [s.strip() for s in sentences if len(s.strip()) > 4][:2]
            if sentences:
                lines.append('')
                for s in sentences:
                    lines.append(f'· {s[:60]}')

    return '\n'.join(lines)


def _wx_send_reply(user_id, text):
    """通过 iLink 发回微信消息（bot_token 从设置读取）。"""
    try:
        bot_token = get_setting('wx_bot_token') or ''
        if not bot_token:
            print(f"[wx_reply] bot_token 未配置，跳过回复 → {user_id}")
            return
        msg_body = {
            'base_info': {'channel_version': '1.0.2'},
            'msg': {
                'to_user_id': user_id,
                'client_id': 'sath-' + str(int(time.time() * 1000)),
                'message_type': 2, 'message_state': 2,
                'item_list': [{'type': 1, 'text_item': {'text': text}}]
            }
        }
        data = json.dumps(msg_body).encode()
        req = urllib.request.Request(
            'https://ilinkai.weixin.qq.com/ilink/bot/sendmessage', data=data,
            headers={'Content-Type': 'application/json',
                     'AuthorizationType': 'ilink_bot_token',
                     'Authorization': 'Bearer ' + bot_token}
        )
        urllib.request.urlopen(req, timeout=8)
        print(f"[wx_reply] sent to {user_id}: {text[:60]}")
    except Exception as e:
        print(f"[wx_reply] failed: {e}")


def _on_buffer_flush_bridge(fragments, source, context_snapshot):
    """
    缓冲池到期回调（bridge 版本）。
    聚合碎片 → ingest pipeline → 异步存储结果。
    """
    combined = " ".join(fragments)
    todo_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.execute(
        "INSERT INTO todos (id,content,types,tags,classify_status,agent_status,origin,created_at,updated_at) "
        "VALUES (?,?,'[]','[]','pending','none',?,?,?)",
        (todo_id, combined, source, now_iso, now_iso)
    )
    db.commit(); db.close()
    threading.Thread(target=ingest_pipeline, args=(todo_id, combined), daemon=True).start()
    print(f"[buffer] flushed {len(fragments)} fragments → todo {todo_id[:8]}")


def init_v2_components():
    """初始化 v2 核心组件（缓冲池、蒸馏调度、编排Agent）。"""
    global _buffer_pool, _brain, _orchestrator

    # 0. 确保 UserModel 所需的表存在（fixed_patterns 等）
    try:
        db = get_db()
        db.execute("""CREATE TABLE IF NOT EXISTS fixed_patterns (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.9,
            hit_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS recent_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_key TEXT NOT NULL,
            to_key TEXT NOT NULL,
            relation TEXT,
            created_at TEXT NOT NULL
        )""")
        # ── 记忆层：跨任务语义记忆 ──
        db.execute("""CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source_todo_id TEXT,
            relevance_keys TEXT,
            created_at TEXT NOT NULL,
            accessed_at TEXT,
            access_count INTEGER NOT NULL DEFAULT 0
        )""")
        # ── 常驻层：提醒日志（防重复推送）──
        db.execute("""CREATE TABLE IF NOT EXISTS resident_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            todo_id TEXT,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        db.commit(); db.close()
        print("[v2] fixed_patterns / recent_relations / memories / resident_log tables ensured")
    except Exception as _e:
        print(f"[v2] table init warning: {_e}")

    # 1. 缓冲池
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from sath_source.brain.pipeline import BufferPool, UserModel
        from pathlib import Path as _Path
        um = UserModel(_Path(DB_PATH))
        buffer_seconds = um.get_buffer_seconds()
        _buffer_pool = BufferPool(
            buffer_seconds=buffer_seconds,
            callback=_on_buffer_flush_bridge,
        )
        print(f"[v2] BufferPool initialized (buffer_seconds={buffer_seconds})")
    except ImportError as e:
        print(f"[v2] BufferPool not available: {e}")

    # 2. 编排 Agent
    try:
        from sath_source.brain.orchestrator import OrchestratorAgent
        from pathlib import Path as _Path

        def _push_to_wechat(user_id, message):
            """将编排 Agent 的推送转发到微信（ClawBot）。"""
            wx_cfg = get_setting('wx_config') or {}
            bot_token = wx_cfg.get('bot_token', '')
            if not bot_token: return
            text = message.get('action', '') + ('\n' + message.get('time', '') if message.get('time') else '')
            if message.get('regret_hint'):
                text += '\n' + message['regret_hint']
            # 调用本地 ClawBot API
            try:
                msg_body = {
                    'base_info': {'channel_version': '1.0.2'},
                    'msg': {
                        'to_user_id': user_id,
                        'client_id': 'sath-' + str(int(time.time() * 1000)),
                        'message_type': 2, 'message_state': 2,
                        'item_list': [{'type': 1, 'text_item': {'text': text}}]
                    }
                }
                data = json.dumps(msg_body).encode()
                req = urllib.request.Request(
                    'http://localhost:18011/api/send', data=data,
                    headers={'Content-Type': 'application/json',
                             'AuthorizationType': 'ilink_bot_token',
                             'Authorization': 'Bearer ' + bot_token}
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print(f"[v2] WeChat push failed: {e}")

        _orchestrator = OrchestratorAgent(db_path=_Path(DB_PATH), push_fn=_push_to_wechat)
        _orchestrator.start()
        print("[v2] OrchestratorAgent started")
    except ImportError as e:
        print(f"[v2] OrchestratorAgent not available: {e}")

    # 3. 冷蒸馏调度（每日凌晨3点）
    try:
        from sath_source.brain.distillation import schedule_cold_distill
        from pathlib import Path as _Path
        cfg = get_llm_config() or {}
        schedule_cold_distill(
            _Path(DB_PATH),
            hour=3,
            llm_provider=None if not cfg.get('api_key') else 'openai',
            llm_model=cfg.get('model', ''),
            api_key=cfg.get('api_key', ''),
        )
        print("[v2] Cold distillation scheduler started (daily 3am)")
    except ImportError as e:
        print(f"[v2] ColdDistill scheduler not available: {e}")


if __name__ == '__main__':
    if not os.path.exists(DB_PATH):
        print(f"ERROR: DB not found at {DB_PATH}"); sys.exit(1)
    print(f"SATH Bridge v2 on http://localhost:{PORT}")
    print(f"DB: {DB_PATH}")
    cfg = get_llm_config()
    print(f"LLM: {cfg.get('model','?')} @ {cfg.get('base_url','?')}" if cfg else "WARNING: no LLM config")
    ac = get_agent_config()
    print(f"Search: {ac.get('search_provider','none')} | max_turns: {ac.get('max_turns',15)}" if ac else "WARNING: no agent config")
    ctx = get_activity_context()
    print(f"ActivityWatch: {'connected' if ctx else 'not available'}")
    # 自动蒸馏 — 照搬 Nio List: 启动 30s 后检查，之后每小时检查，超 24h 未蒸馏则自动跑
    DISTILL_INTERVAL = 24 * 3600  # 24小时
    def auto_distill_loop():
        time.sleep(30)  # 启动 30s 后首次检查
        while True:
            try:
                db = get_db()
                row = db.execute("SELECT value FROM settings WHERE key='last_distill_at'").fetchone()
                db.close()
                last = float(row['value']) if row else 0
                if time.time() - last > DISTILL_INTERVAL:
                    fb_count = get_feedback_count()
                    if fb_count > 0 or last == 0:
                        print(f"[auto-distill] triggered (last={int(time.time()-last)}s ago, {fb_count} feedbacks)")
                        result = distill_preferences()
                        if result:
                            db2 = get_db()
                            db2.execute("INSERT INTO settings (key,value) VALUES ('last_distill_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                        (str(time.time()),))
                            db2.commit(); db2.close()
            except Exception as e:
                print(f"[auto-distill] error: {e}")
            time.sleep(3600)  # 每小时检查一次

    threading.Thread(target=auto_distill_loop, daemon=True).start()
    print("Auto-distill: enabled (24h interval)")

    # 焦点持续采集 — 轻量版 ActivityWatch：每 30 秒记录当前窗口
    _last_focus = [{'app': '', 'title': ''}]
    def focus_collector_loop():
        time.sleep(5)
        while True:
            try:
                enabled = get_setting('focus_collector_enabled')
                if enabled == False or enabled == 'false' or enabled == '"false"':
                    time.sleep(30)
                    continue
                f = capture_focus()
                if f and (f['app'] != _last_focus[0]['app'] or f['title'] != _last_focus[0]['title']):
                    _last_focus[0] = f
                    write_day_context('focus', f"{f['app']} — {f['title']}")
            except: pass
            time.sleep(30)

    threading.Thread(target=focus_collector_loop, daemon=True).start()
    print("Focus collector: enabled (30s interval)")

    # 常驻层 — 截止扫描 + 早报推送
    threading.Thread(target=resident_scanner_loop, daemon=True).start()
    print("Resident scanner: enabled (60s interval, due reminders + morning brief)")

    # v2: 初始化缓冲池 / 编排 Agent / 冷蒸馏调度
    init_v2_components()

    class ThreadedServer(ThreadingMixIn, HTTPServer): pass
    ThreadedServer(('127.0.0.1', PORT), Handler).serve_forever()
