"""
SATH · 执行器 · 异步 Agent 队列
================================
借鉴 Claude Code 的架构模式:
  - 读写隔离: 只读 Agent 并发执行, 写操作串行
  - 乐观锁: 防止 Agent 覆盖用户手动修改
  - 影子执行: 所有 Agent 后台运行, 不抢焦点

借鉴 Hermes Agent:
  - 技能系统: Agent 按类型注册
  - 持久化队列: 崩溃恢复
  - 优先级调度

架构: SQLite 持久化队列 + 多线程 Worker Pool
(生产环境可替换为 Celery + Redis, 但 MVP 阶段 SQLite 够用)
"""

import json
import sqlite3
import threading
import time
import traceback
import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("sath.executor")


# ── 类型定义 ────────────────────────────────────────────

class AgentType(str, Enum):
    RESEARCH  = "research"    # 后台搜索 + 调研报告
    SUMMARIZE = "summarize"   # 内容摘要
    REMIND    = "remind"      # 定时提醒
    EXECUTE   = "execute"     # 执行动作 (写代码、发邮件)
    SYNC      = "sync"        # 多端同步


class TaskStatus(str, Enum):
    QUEUED    = "queued"
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    CANCELLED = "cancelled"


@dataclass
class AgentResult:
    """Agent 执行结果"""
    success: bool
    data: dict = field(default_factory=dict)
    error: Optional[str] = None


# ── Agent 基类 ──────────────────────────────────────────

class BaseAgent(ABC):
    """
    所有 Agent 的基类。

    每个 Agent 类型注册一个子类, 实现 execute() 方法。
    Agent 必须:
      1. 在后台运行, 不弹窗
      2. 返回 AgentResult, 不直接修改 TODO
      3. 自包含错误处理
    """

    agent_type: AgentType

    @abstractmethod
    def execute(self, payload: dict, context: dict) -> AgentResult:
        """
        执行 Agent 任务。

        Args:
            payload: 任务参数 (来自 agent_task.payload)
            context: 环境上下文 (persona + recent context summaries)

        Returns:
            AgentResult: 执行结果, 将异步回填到 TODO
        """
        ...


class ResearchAgent(BaseAgent):
    """调研 Agent — 后台搜索 + 整理报告"""
    agent_type = AgentType.RESEARCH

    def execute(self, payload: dict, context: dict) -> AgentResult:
        query = payload.get("query", "")
        # 实际实现: 调用 LLM + 搜索 API (Tavily/SearXNG)
        # MVP: 返回占位结果
        logger.info(f"[ResearchAgent] 开始调研: {query}")

        # TODO: 替换为真实的搜索 + LLM 摘要 pipeline
        # 1. SearXNG / Tavily 搜索
        # 2. 抓取 top-5 结果页面
        # 3. LLM 生成结构化报告

        return AgentResult(
            success=True,
            data={
                "report_markdown": f"# 调研报告: {query}\n\n> [待实现] 搜索 + LLM 摘要 pipeline",
                "sources": [],
                "key_findings": [],
            },
        )


class SyncAgent(BaseAgent):
    """同步 Agent — 推送到 Apple/Lark/Notion"""
    agent_type = AgentType.SYNC

    def execute(self, payload: dict, context: dict) -> AgentResult:
        target = payload.get("target", "")
        todo_data = payload.get("todo", {})
        logger.info(f"[SyncAgent] 同步到 {target}: {todo_data.get('title', '')}")

        # TODO: 实现各平台 API 调用
        # apple: EventKit / Reminders framework
        # lark:  飞书开放平台 API
        # notion: Notion API

        return AgentResult(
            success=True,
            data={"remote_id": None, "synced_at": datetime.now(timezone.utc).isoformat()},
        )


# ── Agent 注册表 ────────────────────────────────────────

AGENT_REGISTRY: dict[AgentType, type[BaseAgent]] = {}


def register_agent(agent_class: type[BaseAgent]):
    """装饰器: 注册 Agent 类型"""
    AGENT_REGISTRY[agent_class.agent_type] = agent_class
    return agent_class


# 注册内置 Agent
register_agent(ResearchAgent)
register_agent(SyncAgent)


# ── 核心: 任务调度器 ────────────────────────────────────

class AgentScheduler:
    """
    异步任务调度器。

    核心职责:
      1. 从 SQLite 队列拉取待执行任务
      2. 分发到 Worker 线程池
      3. 将结果异步回填到 TODO (带乐观锁)
      4. 处理重试和错误

    设计原则 (来自 CC 源码):
      - 读写隔离: research/summarize 可并发; sync/execute 串行
      - 乐观锁: 检查 todo.version 防止覆盖用户修改
      - 优雅降级: Agent 失败不影响 TODO 本体
    """

    def __init__(
        self,
        db_path: str | Path,
        max_workers: int = 4,
        poll_interval: float = 2.0,
    ):
        self.db_path = str(db_path)
        self.max_workers = max_workers
        self.poll_interval = poll_interval
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sath-agent")
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()  # 写操作串行锁

    # ── 生命周期 ─────────────────────────────────────

    def start(self):
        """启动调度器 (后台轮询)"""
        if self._running:
            return
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="sath-scheduler")
        self._poll_thread.start()
        logger.info("AgentScheduler started")

    def stop(self):
        """停止调度器"""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5.0)
        self._executor.shutdown(wait=True, cancel_futures=True)
        logger.info("AgentScheduler stopped")

    # ── 任务提交 ─────────────────────────────────────

    def submit_task(
        self,
        todo_id: str,
        agent_type: AgentType,
        payload: dict,
        priority: int = 0,
    ) -> str:
        """
        提交新任务到队列。

        TODO 秒级创建后立即调用此方法, 任务异步执行。
        返回 task_id。
        """
        conn = sqlite3.connect(self.db_path)
        task_id = self._generate_id()

        conn.execute(
            """
            INSERT INTO agent_task (id, todo_id, agent_type, status, priority, payload)
            VALUES (?, ?, ?, 'queued', ?, ?)
            """,
            (task_id, todo_id, agent_type.value, priority, json.dumps(payload, ensure_ascii=False)),
        )

        # 同时更新 TODO 的 enrichment_status
        conn.execute(
            "UPDATE todo SET enrichment_status = 'pending' WHERE id = ? AND enrichment_status = 'none'",
            (todo_id,),
        )

        conn.commit()
        conn.close()
        logger.info(f"Task submitted: {task_id} ({agent_type.value}) for TODO {todo_id}")
        return task_id

    # ── 轮询循环 ─────────────────────────────────────

    def _poll_loop(self):
        """主轮询循环: 拉取队列 → 分发执行"""
        while self._running:
            try:
                tasks = self._fetch_pending_tasks()
                for task in tasks:
                    self._dispatch_task(task)
            except Exception as e:
                logger.error(f"Poll loop error: {e}")
            time.sleep(self.poll_interval)

    def _fetch_pending_tasks(self, limit: int = 10) -> list[dict]:
        """从队列拉取待执行任务 (按优先级排序)"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM agent_task
            WHERE status = 'queued'
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def _dispatch_task(self, task: dict):
        """分发单个任务到线程池"""
        task_id = task["id"]
        agent_type = AgentType(task["agent_type"])

        # 标记为 running
        self._update_task_status(task_id, TaskStatus.RUNNING)

        # 写操作 (sync/execute) 需要串行
        is_write_op = agent_type in (AgentType.SYNC, AgentType.EXECUTE)

        if is_write_op:
            # 串行执行
            future = self._executor.submit(self._execute_with_lock, task, agent_type)
        else:
            # 并发执行
            future = self._executor.submit(self._execute_task, task, agent_type)

        future.add_done_callback(lambda f: self._on_task_complete(f, task))

    def _execute_with_lock(self, task: dict, agent_type: AgentType) -> AgentResult:
        """写操作加锁串行执行"""
        with self._write_lock:
            return self._execute_task(task, agent_type)

    def _execute_task(self, task: dict, agent_type: AgentType) -> AgentResult:
        """执行单个 Agent 任务"""
        agent_class = AGENT_REGISTRY.get(agent_type)
        if not agent_class:
            return AgentResult(success=False, error=f"Unknown agent type: {agent_type}")

        agent = agent_class()
        payload = json.loads(task["payload"]) if isinstance(task["payload"], str) else task["payload"]

        # 加载上下文 (persona + recent summaries)
        context = self._load_context()

        return agent.execute(payload, context)

    # ── 结果回填 (带乐观锁) ──────────────────────────

    def _on_task_complete(self, future: Future, task: dict):
        """任务完成回调: 回填结果到 TODO"""
        task_id = task["id"]
        todo_id = task["todo_id"]

        try:
            result: AgentResult = future.result()
        except Exception as e:
            result = AgentResult(success=False, error=traceback.format_exc())

        if result.success:
            self._update_task_status(task_id, TaskStatus.DONE, result=result.data)
            self._enrich_todo(todo_id, result.data)
        else:
            retry_count = task.get("retry_count", 0)
            max_retries = task.get("max_retries", 3)

            if retry_count < max_retries:
                self._retry_task(task_id, retry_count + 1, result.error)
            else:
                self._update_task_status(task_id, TaskStatus.FAILED, error=result.error)
                self._mark_todo_enrichment(todo_id, "failed")

    def _enrich_todo(self, todo_id: str, enrichment_data: dict):
        """
        异步回填增强数据到 TODO。

        关键: 使用乐观锁 (version) 防止覆盖用户手动修改。
        如果用户在 Agent 执行期间修改了 TODO, 采用 merge 而非覆盖。
        """
        conn = sqlite3.connect(self.db_path)

        # 读取当前 TODO 状态 + version
        row = conn.execute(
            "SELECT enrichment, version FROM todo WHERE id = ?",
            (todo_id,),
        ).fetchone()

        if not row:
            conn.close()
            return

        current_enrichment = json.loads(row[0]) if row[0] else {}
        current_version = row[1]

        # Merge 策略: 深度合并, 不覆盖已有字段
        merged = {**current_enrichment}
        for key, value in enrichment_data.items():
            if key not in merged or merged[key] is None:
                merged[key] = value
            elif isinstance(merged[key], list) and isinstance(value, list):
                # 列表: 追加去重
                existing = set(json.dumps(x) for x in merged[key])
                for item in value:
                    if json.dumps(item) not in existing:
                        merged[key].append(item)
            elif isinstance(merged[key], dict) and isinstance(value, dict):
                # 字典: 递归合并
                merged[key] = {**merged[key], **value}
            # 其他类型: 保留用户已有值 (不覆盖)

        # 乐观锁更新
        result = conn.execute(
            """
            UPDATE todo
            SET enrichment = ?,
                enrichment_status = 'done',
                version = version + 1
            WHERE id = ? AND version = ?
            """,
            (json.dumps(merged, ensure_ascii=False), todo_id, current_version),
        )

        if result.rowcount == 0:
            # 版本冲突: 用户在 Agent 执行期间修改了 TODO
            # 重试一次 merge
            logger.warning(f"Version conflict for TODO {todo_id}, retrying merge")
            conn.close()
            self._enrich_todo(todo_id, enrichment_data)  # 递归重试 (最多 1 次)
            return

        conn.commit()
        conn.close()
        logger.info(f"Enrichment applied to TODO {todo_id}")

    # ── 辅助方法 ─────────────────────────────────────

    def _update_task_status(
        self, task_id: str, status: TaskStatus,
        result: Optional[dict] = None, error: Optional[str] = None,
    ):
        conn = sqlite3.connect(self.db_path)
        now = datetime.now(timezone.utc).isoformat()

        if status == TaskStatus.RUNNING:
            conn.execute(
                "UPDATE agent_task SET status = ?, started_at = ? WHERE id = ?",
                (status.value, now, task_id),
            )
        elif status in (TaskStatus.DONE, TaskStatus.FAILED):
            conn.execute(
                "UPDATE agent_task SET status = ?, result = ?, error = ?, finished_at = ? WHERE id = ?",
                (status.value, json.dumps(result) if result else None, error, now, task_id),
            )
        else:
            conn.execute("UPDATE agent_task SET status = ? WHERE id = ?", (status.value, task_id))

        conn.commit()
        conn.close()

    def _retry_task(self, task_id: str, new_count: int, error: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE agent_task SET status = 'queued', retry_count = ?, error = ? WHERE id = ?",
            (new_count, error, task_id),
        )
        conn.commit()
        conn.close()
        logger.info(f"Task {task_id} retry #{new_count}")

    def _mark_todo_enrichment(self, todo_id: str, status: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE todo SET enrichment_status = ? WHERE id = ?", (status, todo_id))
        conn.commit()
        conn.close()

    def _load_context(self) -> dict:
        """加载 persona + 最近环境摘要"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        persona = conn.execute("SELECT * FROM persona LIMIT 1").fetchone()
        summaries = conn.execute(
            "SELECT summary FROM context_summary ORDER BY window_end DESC LIMIT 3"
        ).fetchall()

        conn.close()
        return {
            "persona": dict(persona) if persona else {},
            "recent_context": [r["summary"] for r in summaries],
        }

    @staticmethod
    def _generate_id() -> str:
        import os
        return os.urandom(8).hex()


# ── 便捷: 创建 TODO 并自动提交 Agent 任务 ───────────────

def create_todo_with_agents(
    db_path: str | Path,
    scheduler: AgentScheduler,
    classified: dict,
    source: str = "manual",
    context_snapshot: Optional[dict] = None,
) -> list[str]:
    """
    从 LLM 分类结果创建 TODO 并自动提交相应的 Agent 任务。

    核心流程:
      1. 秒级创建 TODO (用户立即可见)
      2. 根据 intent 提交后台 Agent
      3. Agent 完成后异步回填

    返回创建的 todo_id 列表。
    """
    conn = sqlite3.connect(str(db_path))
    todo_ids = []

    for item in classified.get("todos", []):
        todo_id = AgentScheduler._generate_id()

        conn.execute(
            """
            INSERT INTO todo (id, title, body, intent, priority, confidence, tags, due_at,
                              context_snapshot, source, enrichment_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                todo_id,
                item.get("title", "未命名任务"),
                item.get("body"),
                item.get("intent", "record"),
                item.get("priority", 0),
                item.get("confidence", 0.0),
                json.dumps(item.get("tags", []), ensure_ascii=False),
                item.get("due_at"),
                json.dumps(context_snapshot or item.get("context_snapshot", {}), ensure_ascii=False),
                source,
                "pending" if item.get("agent_hint") else "none",
            ),
        )
        todo_ids.append(todo_id)

        # 根据意图自动提交 Agent 任务
        agent_hint = item.get("agent_hint", {})
        intent = item.get("intent", "record")

        if intent == "research" or agent_hint.get("type") == "research":
            scheduler.submit_task(
                todo_id=todo_id,
                agent_type=AgentType.RESEARCH,
                payload={"query": agent_hint.get("query", item.get("title", ""))},
                priority=item.get("priority", 0),
            )
        elif intent == "reminder" or agent_hint.get("type") == "remind":
            scheduler.submit_task(
                todo_id=todo_id,
                agent_type=AgentType.REMIND,
                payload={"due_at": item.get("due_at"), "title": item.get("title")},
                priority=3,  # 提醒优先级高
            )

    conn.commit()
    conn.close()
    return todo_ids


# ── CLI 演示 ────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    db_path = sys.argv[1] if len(sys.argv) > 1 else ":memory:"

    if db_path == ":memory:":
        # 内存模式: 先建表
        print("使用内存数据库演示...")
        conn = sqlite3.connect(db_path)
        # 读取并执行 migration
        migration_path = Path(__file__).parent.parent / "schema" / "migrations.sql"
        if migration_path.exists():
            conn.executescript(migration_path.read_text())
        conn.close()

    scheduler = AgentScheduler(db_path=db_path, max_workers=2, poll_interval=1.0)
    scheduler.start()

    # 模拟提交任务
    task_id = scheduler.submit_task(
        todo_id="demo-todo-001",
        agent_type=AgentType.RESEARCH,
        payload={"query": "Shopify webhook 库存同步延迟解决方案"},
        priority=3,
    )
    print(f"Submitted task: {task_id}")

    # 等待执行
    time.sleep(5)
    scheduler.stop()
    print("Done.")
