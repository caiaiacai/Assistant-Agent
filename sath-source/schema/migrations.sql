-- ============================================================
-- SATH · 感知型智能待办中枢
-- Core Data Schema · SQLite
-- Dieter Rams: "Less, but better"
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ────────────────────────────────────────────────────────────
-- 1. 静态画像 (Static Persona)
--    用户的身份底色，意图判定的锚点
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS persona (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    role        TEXT NOT NULL,                     -- "跨境电商技术负责人"
    domains     TEXT NOT NULL DEFAULT '[]',        -- JSON: ["shopify", "logistics", "payments"]
    tools       TEXT NOT NULL DEFAULT '[]',        -- JSON: ["cursor", "figma", "lark"]
    preferences TEXT NOT NULL DEFAULT '{}',        -- JSON: 风格偏好、语言、时区
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ────────────────────────────────────────────────────────────
-- 2. 智能 TODO 对象 (核心实体)
--    秒级创建 → 异步增强 → 多端同步
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS todo (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),

    -- 基础字段 (用户可见)
    title           TEXT NOT NULL,
    body            TEXT,                          -- Markdown 富文本
    status          TEXT NOT NULL DEFAULT 'inbox'
                    CHECK (status IN ('inbox','active','waiting','done','archived')),
    priority        INTEGER NOT NULL DEFAULT 0     -- 0=none, 1=low, 2=mid, 3=high, 4=urgent
                    CHECK (priority BETWEEN 0 AND 4),
    due_at          TEXT,                          -- ISO 8601
    tags            TEXT NOT NULL DEFAULT '[]',    -- JSON array

    -- 意图分类 (AI 判定)
    intent          TEXT NOT NULL DEFAULT 'record'
                    CHECK (intent IN ('research','task','record','reminder')),
    confidence      REAL DEFAULT 0.0,             -- AI 判定置信度 0.0~1.0

    -- 环境快照 (创建时冻结)
    context_snapshot TEXT NOT NULL DEFAULT '{}',   -- JSON: 创建时的环境摘要
    source          TEXT NOT NULL DEFAULT 'manual'
                    CHECK (source IN ('manual','wechat','lark','hotkey','api','sensor')),
    source_id       TEXT,                          -- 外部来源 ID (防重复)

    -- AI 增强字段 (异步回填)
    enrichment      TEXT NOT NULL DEFAULT '{}',    -- JSON: AI 产出的增强内容
    enrichment_status TEXT NOT NULL DEFAULT 'none'
                    CHECK (enrichment_status IN ('none','pending','running','done','failed')),

    -- 关联
    project_id      TEXT REFERENCES project(id) ON DELETE SET NULL,
    parent_id       TEXT REFERENCES todo(id) ON DELETE SET NULL,

    -- 同步
    sync_state      TEXT NOT NULL DEFAULT '{}',   -- JSON: {"apple": {...}, "lark": {...}, "notion": {...}}
    source_tag      TEXT NOT NULL DEFAULT 'sath', -- 防循环标记
    version         INTEGER NOT NULL DEFAULT 1,   -- 乐观锁

    -- 时间戳
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at    TEXT
);

-- ────────────────────────────────────────────────────────────
-- 3. 项目 (自动/手动归类)
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    name        TEXT NOT NULL,
    color       TEXT DEFAULT '#64748b',           -- Dieter Rams 克制的灰
    icon        TEXT DEFAULT 'folder',
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ────────────────────────────────────────────────────────────
-- 4. 环境日志 (DayContext 时间线)
--    感知器采集 → 摘要 → 注入 Planner
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS context_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    event_type  TEXT NOT NULL
                CHECK (event_type IN ('app_switch','url_visit','focus_element','screenshot','git_branch','idle')),
    app_name    TEXT,
    window_title TEXT,
    url         TEXT,
    content     TEXT,                              -- Accessibility 文本 / OCR 结果
    metadata    TEXT NOT NULL DEFAULT '{}',         -- JSON: 额外字段
    duration_ms INTEGER DEFAULT 0                  -- 停留时长
);

-- ────────────────────────────────────────────────────────────
-- 5. 环境摘要 (5 分钟粒度的压缩快照)
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS context_summary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    summary     TEXT NOT NULL,                     -- LLM 生成的自然语言摘要
    top_apps    TEXT NOT NULL DEFAULT '[]',        -- JSON: [{"app": "Cursor", "pct": 0.6}, ...]
    top_urls    TEXT NOT NULL DEFAULT '[]',        -- JSON
    mood        TEXT DEFAULT 'neutral',            -- 推断的工作状态
    raw_event_ids TEXT NOT NULL DEFAULT '[]',      -- JSON: 关联原始事件
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ────────────────────────────────────────────────────────────
-- 6. Agent 任务队列 (影子执行)
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_task (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    todo_id     TEXT NOT NULL REFERENCES todo(id) ON DELETE CASCADE,
    agent_type  TEXT NOT NULL
                CHECK (agent_type IN ('research','summarize','remind','execute','sync')),
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','done','failed','cancelled')),
    priority    INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL DEFAULT '{}',        -- JSON: Agent 输入参数
    result      TEXT,                              -- JSON: Agent 输出
    error       TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    started_at  TEXT,
    finished_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ────────────────────────────────────────────────────────────
-- 7. 同步日志 (防循环 + 审计)
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id     TEXT NOT NULL REFERENCES todo(id) ON DELETE CASCADE,
    target      TEXT NOT NULL CHECK (target IN ('apple','lark','notion')),
    direction   TEXT NOT NULL CHECK (direction IN ('push','pull')),
    status      TEXT NOT NULL CHECK (status IN ('ok','conflict','error')),
    remote_id   TEXT,
    payload     TEXT,                              -- JSON: 发送/接收的数据
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ────────────────────────────────────────────────────────────
-- 8. 反馈记录 (成长值来源之一)
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id     TEXT REFERENCES todo(id) ON DELETE SET NULL,
    rating      INTEGER CHECK (rating BETWEEN -1 AND 1),  -- -1=差, 0=中, 1=好
    comment     TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ────────────────────────────────────────────────────────────
-- 9. 成长值 (AI 等级系统)
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS growth_score (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dimension   TEXT NOT NULL,                     -- "persona_completeness", "feedback_count", "context_depth"...
    value       REAL NOT NULL DEFAULT 0.0,
    source      TEXT NOT NULL,                     -- "explicit" | "implicit"
    note        TEXT,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ════════════════════════════════════════════════════════════
-- Indexes (查询性能)
-- ════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_todo_status       ON todo(status);
CREATE INDEX IF NOT EXISTS idx_todo_intent       ON todo(intent);
CREATE INDEX IF NOT EXISTS idx_todo_project      ON todo(project_id);
CREATE INDEX IF NOT EXISTS idx_todo_created      ON todo(created_at);
CREATE INDEX IF NOT EXISTS idx_todo_due          ON todo(due_at) WHERE due_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_todo_enrichment   ON todo(enrichment_status) WHERE enrichment_status != 'done';
CREATE INDEX IF NOT EXISTS idx_todo_source       ON todo(source, source_id);

CREATE INDEX IF NOT EXISTS idx_context_event_ts  ON context_event(timestamp);
CREATE INDEX IF NOT EXISTS idx_context_event_type ON context_event(event_type);

CREATE INDEX IF NOT EXISTS idx_agent_task_status ON agent_task(status, priority);
CREATE INDEX IF NOT EXISTS idx_agent_task_todo   ON agent_task(todo_id);

CREATE INDEX IF NOT EXISTS idx_sync_log_todo     ON sync_log(todo_id, target);

-- FTS5 全文搜索
CREATE VIRTUAL TABLE IF NOT EXISTS todo_fts USING fts5(
    title, body, tags,
    content='todo',
    content_rowid='rowid'
);

-- 触发器：todo 插入/更新时同步 FTS
CREATE TRIGGER IF NOT EXISTS todo_fts_insert AFTER INSERT ON todo BEGIN
    INSERT INTO todo_fts(rowid, title, body, tags) VALUES (new.rowid, new.title, new.body, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS todo_fts_update AFTER UPDATE ON todo BEGIN
    INSERT INTO todo_fts(todo_fts, rowid, title, body, tags) VALUES ('delete', old.rowid, old.title, old.body, old.tags);
    INSERT INTO todo_fts(rowid, title, body, tags) VALUES (new.rowid, new.title, new.body, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS todo_fts_delete AFTER DELETE ON todo BEGIN
    INSERT INTO todo_fts(todo_fts, rowid, title, body, tags) VALUES ('delete', old.rowid, old.title, old.body, old.tags);
END;

-- 触发器：updated_at 自动更新
CREATE TRIGGER IF NOT EXISTS todo_updated AFTER UPDATE ON todo BEGIN
    UPDATE todo SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = new.id;
END;
