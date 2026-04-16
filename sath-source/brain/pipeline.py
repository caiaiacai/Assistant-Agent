"""
SATH · 中枢大脑 · 主 Pipeline v2.0
=====================================
升级要点（对照 Assistant 架构 v1.7）：
  1. 缓冲池（BufferPool）—— 20s 聚合碎片流，而非逐条处理
  2. Skills 库检索 —— 命中 Skill 跳过拓扑分解，直接复用 pipeline
  3. 权限层 —— 三档判断（告知/建议/执行）+ 缓冲窗口
  4. 编排 Agent 模式 —— 子 Agent 不直接输出给用户
  5. 微蒸馏 —— 每段对话结束后提炼，原始对话丢弃
  6. 用户模型注入 —— 意图层注入快照，编排层注入完整快照
"""

import json
import sqlite3
import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List
from collections import deque

from ..sensor.activity_watch import summarize_window, persist_summary, ContextSummary
from ..prompts.intent_classifier import (
    ClassifierInput, classify_intent, rule_engine_classify
)
from ..executor.agent_queue import AgentScheduler, create_todo_with_agents, AgentType

logger = logging.getLogger("sath.brain")

DEFAULT_DB_PATH = Path.home() / ".sath" / "sath.db"
CONTEXT_WINDOW_MINUTES = 5
BUFFER_SECONDS_DEFAULT = 20         # 缓冲池默认等待时长
BUFFER_SECONDS_MAX = 60             # 最大等待时长
REGRET_WINDOW_DEFAULT = 300         # 后悔药窗口（秒）
MAX_AGENT_TURNS = 25                # 子 Agent 最大轮次（第24轮催促）
SKILL_SIMILARITY_THRESHOLD = 0.75   # Skills 复用相似度阈值
SKILL_SUCCESS_RATE_THRESHOLD = 0.60


# ══════════════════════════════════════════════════════════
# 缓冲池 (BufferPool)
# ══════════════════════════════════════════════════════════

class BufferPool:
    """
    碎片流聚合器。用户在同一上下文内连续输入的碎片，
    以流为单位聚合后整体送入意图层，而非逐条处理。

    20s 无新输入 → 关闭缓冲池 → 整体送入意图层
    缓冲时长后期从 rhythm_params 按用户打字习惯自适应。
    """

    def __init__(self, buffer_seconds: int = BUFFER_SECONDS_DEFAULT, callback=None):
        self.buffer_seconds = buffer_seconds
        self.callback = callback  # 缓冲池关闭时的回调 fn(fragments, source, context_snapshot)
        self._lock = threading.Lock()
        self._fragments: List[str] = []
        self._source: str = "manual"
        self._context_snapshot: dict = {}
        self._timer: Optional[threading.Timer] = None
        self._open: bool = False

    def push(self, text: str, source: str = "manual", context_snapshot: dict = None):
        """推入一条碎片，重置倒计时。"""
        with self._lock:
            if not self._open:
                self._fragments = []
                self._source = source
                self._context_snapshot = context_snapshot or {}
                self._open = True

            self._fragments.append(text)

            # 重置计时器
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.buffer_seconds, self._flush)
            self._timer.daemon = True
            self._timer.start()

        logger.debug(f"[BufferPool] +fragment ({len(self._fragments)} total): {text[:40]}")

    def _flush(self):
        """定时器触发：关闭缓冲池，整体送入意图层。"""
        with self._lock:
            if not self._open:
                return
            fragments = list(self._fragments)
            source = self._source
            context_snapshot = dict(self._context_snapshot)
            self._fragments = []
            self._open = False
            self._timer = None

        logger.info(f"[BufferPool] flush {len(fragments)} fragments → intent layer")
        if self.callback and fragments:
            try:
                self.callback(fragments, source, context_snapshot)
            except Exception as e:
                logger.error(f"[BufferPool] callback error: {e}")

    def flush_now(self):
        """立即刷新（用于测试或显式触发）。"""
        if self._timer:
            self._timer.cancel()
        self._flush()

    def is_open(self) -> bool:
        return self._open


# ══════════════════════════════════════════════════════════
# 权限层 (PermissionLayer)
# ══════════════════════════════════════════════════════════

class PermissionTier:
    INFORM  = "inform"   # 告知档：发现了，通知用户，不执行
    SUGGEST = "suggest"  # 建议档：帮用户想好方案，等用户点头
    EXECUTE = "execute"  # 执行档：直接办，缓冲窗口给用户后悔机会


# 永远需要用户确认的操作（无论执行过多少次）
ALWAYS_CONFIRM_ACTIONS = {
    "payment", "transfer", "delete", "deactivate",
    "send_email", "send_message", "broadcast", "post_public"
}


def determine_permission_tier(intent: str, topology: dict, user_model: dict) -> str:
    """
    三档权限判断。与场景和信任度动态绑定，不固定。
    订咖啡可能是执行档，订机票即使做了一百次也应该是建议档。
    """
    action_type = topology.get("action_type", "")
    cost_dim = topology.get("cost_dim")
    person_dim = topology.get("person_dim")
    confidence = float(topology.get("confidence", 0.5))

    # 强制建议档：高成本、对外发送、不可逆
    if cost_dim and _is_high_cost(cost_dim):
        return PermissionTier.SUGGEST
    if person_dim and "他人" in str(person_dim):
        return PermissionTier.SUGGEST
    if action_type in ALWAYS_CONFIRM_ACTIONS:
        return PermissionTier.SUGGEST

    # 纯记录/灵感 → 告知档
    if intent in ("record",) and confidence > 0.7:
        return PermissionTier.INFORM

    # 有历史成功记录的高频任务 → 执行档
    skill_hit_count = int(user_model.get("skill_hit_count", {}).get(intent, 0))
    if skill_hit_count >= 5 and confidence >= 0.75:
        return PermissionTier.EXECUTE

    # 默认建议档（安全兜底）
    if intent in ("task", "reminder") and confidence >= 0.7:
        return PermissionTier.EXECUTE
    return PermissionTier.SUGGEST


def _is_high_cost(cost_dim) -> bool:
    """判断成本维度是否属于高成本操作（需要建议档）。"""
    if not cost_dim:
        return False
    text = str(cost_dim).lower()
    return any(k in text for k in ["机票", "火车票", "高铁", "酒店", "大额", "转账", "支付"])


# ══════════════════════════════════════════════════════════
# Skills 库接口 (SkillsLibrary)
# ══════════════════════════════════════════════════════════

class SkillsLibrary:
    """
    程序性记忆——记录怎么做事。
    蒸馏层是陈述性记忆——了解这个人。
    两者共同构成"越用越懂你"的核心。
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def semantic_match(self, intent_text: str, intent_type: str, threshold: float = SKILL_SIMILARITY_THRESHOLD) -> Optional[dict]:
        """
        语义检索：意图识别完成后，向 Skills 库做语义检索。
        命中则直接复用 pipeline，跳过拓扑分解。
        MVP 阶段用关键词匹配代替向量检索。
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM skills WHERE success_rate >= ? ORDER BY hit_count DESC LIMIT 20",
                (SKILL_SUCCESS_RATE_THRESHOLD,)
            ).fetchall()
        except Exception:
            return None
        finally:
            conn.close()

        if not rows:
            return None

        # 简单关键词匹配（后期替换为 ChromaDB 向量检索）
        intent_words = set(intent_text.lower().split())
        best_score = 0.0
        best_skill = None

        for row in rows:
            trigger = str(row["trigger"] or "").lower()
            skill_words = set(trigger.split())
            if not skill_words:
                continue
            overlap = len(intent_words & skill_words) / max(len(skill_words), 1)
            if overlap > best_score and overlap >= threshold:
                best_score = overlap
                best_skill = dict(row)

        return best_skill

    def record_hit(self, skill_id: int, success: bool):
        """记录 Skill 复用结果，更新成功率。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """UPDATE skills SET
                   hit_count = hit_count + 1,
                   success_rate = ROUND(
                       (success_rate * hit_count + ?) / (hit_count + 1), 3
                   ),
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE id = ?""",
                (1.0 if success else 0.0, skill_id)
            )
            conn.commit()
        finally:
            conn.close()

    def auto_distill_skill(self, todo_id: str, trajectory: list, intent: str):
        """
        Skill 自动提炼（LLM 异步）。
        触发条件：执行成功 + pipeline steps > 1 + 同类意图出现过 N 次以上。
        """
        if len(trajectory) <= 1:
            return  # 无复杂度，不提炼
        # 实际使用时通过 AgentScheduler 异步提炼，此处预留接口
        logger.info(f"[Skills] 待提炼: todo={todo_id}, steps={len(trajectory)}, intent={intent}")


# ══════════════════════════════════════════════════════════
# 用户模型接口 (UserModel)
# ══════════════════════════════════════════════════════════

class UserModel:
    """
    用户模型三层结构：
    - 固化层（最高权重）：反复出现的刻板行为，需显著反向信号才能覆盖
    - 活跃层（中权重）：近期行为模式，有时间衰减
    - 归档层（低权重）：低频历史，按需向量检索
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def load_snapshot(self, include_active: bool = True) -> dict:
        """加载用户模型快照，注入意图层/编排层。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        snapshot = {}

        # 固化层
        try:
            rows = conn.execute(
                "SELECT key, value, confidence FROM fixed_patterns ORDER BY confidence DESC LIMIT 10"
            ).fetchall()
            snapshot["fixed_patterns"] = [
                {"key": r["key"], "value": json.loads(r["value"]) if _is_json(r["value"]) else r["value"],
                 "confidence": r["confidence"]}
                for r in rows
            ]
        except Exception:
            snapshot["fixed_patterns"] = []

        # 活跃层（近期偏好摘要）
        if include_active:
            try:
                rows = conn.execute(
                    "SELECT key, value FROM settings WHERE key='active_preferences'"
                ).fetchone()
                if rows:
                    snapshot["active_preferences"] = json.loads(rows["value"]) if _is_json(rows["value"]) else {}
            except Exception:
                snapshot["active_preferences"] = {}

        # 节奏参数
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='rhythm_params'").fetchone()
            if row:
                snapshot["rhythm_params"] = json.loads(row["value"]) if _is_json(row["value"]) else {}
        except Exception:
            snapshot["rhythm_params"] = {}

        # 近期人物关系
        try:
            rows = conn.execute(
                "SELECT name, role, org FROM relations ORDER BY last_seen DESC LIMIT 8"
            ).fetchall()
            snapshot["recent_relations"] = [dict(r) for r in rows]
        except Exception:
            snapshot["recent_relations"] = []

        conn.close()
        return snapshot

    def get_buffer_seconds(self) -> int:
        """从节奏参数获取个性化缓冲时长。"""
        snapshot = self.load_snapshot(include_active=False)
        rhythm = snapshot.get("rhythm_params", {})
        return int(rhythm.get("buffer_seconds", BUFFER_SECONDS_DEFAULT))

    def update_rhythm(self, buffer_seconds: int):
        """更新节奏参数（缓冲时长自适应）。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            existing = conn.execute(
                "SELECT value FROM settings WHERE key='rhythm_params'"
            ).fetchone()
            params = json.loads(existing["value"]) if existing and _is_json(existing["value"]) else {}
            params["buffer_seconds"] = buffer_seconds
            conn.execute(
                "INSERT INTO settings (key,value) VALUES ('rhythm_params',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(params),)
            )
            conn.commit()
        finally:
            conn.close()


def _is_json(s) -> bool:
    if not isinstance(s, str):
        return False
    try:
        json.loads(s)
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════
# 主 Pipeline (SATHBrain v2)
# ══════════════════════════════════════════════════════════

class SATHBrain:
    """
    SATH 中枢大脑 v2.0。

    架构升级：
    - 缓冲池聚合碎片 → 整体送入意图层
    - Skills 库检索 → 命中跳过拓扑分解
    - 权限层三档判断 → 执行档带缓冲窗口
    - 编排 Agent 作为唯一对话界面
    - 微蒸馏（每段对话结束后提炼）
    """

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        llm_provider: str = "anthropic",
        llm_model: str = "claude-sonnet-4-6",
        api_key: Optional[str] = None,
        max_workers: int = 4,
    ):
        self.db_path = Path(db_path)
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        self.api_key = api_key
        self.scheduler: Optional[AgentScheduler] = None
        self.max_workers = max_workers
        self._initialized = False

        # 子系统
        self.user_model = UserModel(self.db_path)
        self.skills_library = SkillsLibrary(self.db_path)

        # 缓冲池（初始化后根据用户节奏参数动态调整时长）
        self.buffer_pool = BufferPool(
            buffer_seconds=BUFFER_SECONDS_DEFAULT,
            callback=self._on_buffer_flush,
        )

        # 缓冲池刷新结果队列（异步回调 → 同步访问）
        self._pending_results: deque = deque(maxlen=100)
        self._results_lock = threading.Lock()

    def init(self):
        """初始化数据库 + 启动 Agent 调度器 + 校准缓冲时长。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        migration_sql = Path(__file__).parent.parent / "schema" / "migrations.sql"
        if migration_sql.exists():
            conn = sqlite3.connect(str(self.db_path))
            conn.executescript(migration_sql.read_text())
            conn.close()
            logger.info(f"Database initialized at {self.db_path}")

        self.scheduler = AgentScheduler(db_path=self.db_path, max_workers=self.max_workers)
        self.scheduler.start()

        # 从用户模型获取个性化缓冲时长
        user_buffer_seconds = self.user_model.get_buffer_seconds()
        self.buffer_pool.buffer_seconds = user_buffer_seconds
        logger.info(f"Buffer seconds calibrated: {user_buffer_seconds}s")

        self._initialized = True
        logger.info("SATH Brain v2 initialized")

    def shutdown(self):
        if self.buffer_pool.is_open():
            self.buffer_pool.flush_now()
        if self.scheduler:
            self.scheduler.stop()
        self._initialized = False
        logger.info("SATH Brain shutdown")

    # ── 主入口（带缓冲池）────────────────────────────────

    def push(self, raw_input: str, source: str = "manual", flush_now: bool = False) -> Optional[dict]:
        """
        主入口（缓冲池模式）。

        多数情况下直接 push，缓冲池到时间自动 flush。
        flush_now=True 用于调试或单条强制处理。
        """
        assert self._initialized, "Call init() first"

        # 采集触发上下文快照
        context_snapshot = self._capture_context_snapshot()

        if flush_now:
            # 立即处理，不进缓冲池
            return self._process_fragments([raw_input], source, context_snapshot)

        self.buffer_pool.push(raw_input, source=source, context_snapshot=context_snapshot)
        return None  # 结果异步通过回调或 pending_results 获取

    def _on_buffer_flush(self, fragments: List[str], source: str, context_snapshot: dict):
        """缓冲池到期回调：聚合碎片 → 完整处理流程。"""
        result = self._process_fragments(fragments, source, context_snapshot)
        with self._results_lock:
            self._pending_results.append(result)

    def get_pending_results(self) -> list:
        """获取所有待推送的处理结果（前端轮询或 SSE 推送）。"""
        with self._results_lock:
            results = list(self._pending_results)
            self._pending_results.clear()
            return results

    # ── 兼容旧接口（直接处理，不走缓冲池）──────────────

    def ingest(self, raw_input: str, source: str = "manual", fetch_context: bool = True) -> dict:
        """
        旧接口兼容：直接处理，不走缓冲池。
        推荐迁移到 push()。
        """
        assert self._initialized, "Call init() first"
        context_snapshot = {}
        if fetch_context:
            try:
                ctx = summarize_window(minutes=CONTEXT_WINDOW_MINUTES)
                persist_summary(ctx, self.db_path)
                context_snapshot = asdict(ctx)
            except Exception as e:
                logger.warning(f"Context fetch failed: {e}")
                context_snapshot = {"summary_text": "环境数据不可用"}

        return self._process_fragments([raw_input], source, context_snapshot)

    # ── 核心处理流程 ──────────────────────────────────────

    def _process_fragments(self, fragments: List[str], source: str, context_snapshot: dict) -> dict:
        """
        核心处理流程：
        1. 加载用户模型快照
        2. Skills 库检索（命中则跳过拓扑分解）
        3. 意图识别（大模型主路径 + 规则引擎兜底）
        4. 权限层判断（三档）
        5. 执行层（编排 Agent 调度）
        6. 微蒸馏（异步后台）
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        combined_input = " ".join(fragments) if len(fragments) > 1 else fragments[0]

        # Step 1: 加载用户模型快照
        user_model_snapshot = self.user_model.load_snapshot()
        persona = self._load_persona()

        # Step 2: Skills 库检索
        skill_hit = self.skills_library.semantic_match(combined_input, intent_type="")
        if skill_hit:
            logger.info(f"[Skills] 命中 skill: {skill_hit['name']}，跳过拓扑分解")
            classified = self._skill_to_classified(skill_hit, combined_input)
        else:
            # Step 3: 意图识别（LLM 主路径 + 规则引擎兜底）
            inp = ClassifierInput(
                user_input=combined_input,
                source=source,
                timestamp=timestamp,
                persona=persona,
                user_model=user_model_snapshot,
                context_summary=context_snapshot if isinstance(context_snapshot, dict) else asdict(context_snapshot),
                buffer_fragments=fragments if len(fragments) > 1 else None,
            )
            try:
                classified = classify_intent(
                    inp,
                    provider=self.llm_provider,
                    model=self.llm_model,
                    api_key=self.api_key,
                    timeout=20,
                )
            except Exception as e:
                logger.warning(f"LLM classification failed: {e}, using rule engine")
                classified = rule_engine_classify(combined_input)

        # Step 4: 权限层判断
        todos = classified.get("todos", [])
        for todo in todos:
            topology = todo.get("topology", {})
            topology["confidence"] = todo.get("confidence", 0.5)
            tier = determine_permission_tier(
                todo.get("intent", "record"),
                topology,
                user_model_snapshot,
            )
            todo["permission_tier"] = tier
            if tier == PermissionTier.EXECUTE:
                todo["regret_window_seconds"] = todo.get("regret_window_seconds", REGRET_WINDOW_DEFAULT)
                todo["regret_deadline"] = (
                    datetime.now(timezone.utc).timestamp() + todo["regret_window_seconds"]
                )

        # Step 5: 执行层（编排 Agent 调度）
        todo_ids = create_todo_with_agents(
            db_path=self.db_path,
            scheduler=self.scheduler,
            classified=classified,
            source=source,
            context_snapshot=context_snapshot,
        )

        result = {
            "todo_ids": todo_ids,
            "classified": classified,
            "context_summary": (
                context_snapshot.get("summary_text", "")
                if isinstance(context_snapshot, dict) else ""
            ),
            "fragment_count": len(fragments),
            "skill_hit": skill_hit["name"] if skill_hit else None,
            "timestamp": timestamp,
        }

        # Step 6: 微蒸馏（异步，不阻塞用户体验）
        threading.Thread(
            target=self._micro_distill,
            args=(combined_input, classified, source, context_snapshot),
            daemon=True,
        ).start()

        logger.info(f"Processed {len(fragments)} fragments → {len(todo_ids)} TODOs")
        return result

    # ── 微蒸馏 ───────────────────────────────────────────

    def _micro_distill(self, raw_input: str, classified: dict, source: str, context: dict):
        """
        微蒸馏：每段对话/处理结束后，提炼有价值的信息写入用户模型。
        临时上下文用完即丢，下次注入的是蒸馏快照，不是历史对话。
        """
        try:
            from .distillation import hot_distill
            hot_distill(
                db_path=self.db_path,
                raw_input=raw_input,
                classified=classified,
                source=source,
            )
        except ImportError:
            # distillation 模块尚未创建时的优雅降级
            self._basic_behavior_log(raw_input, classified, source)
        except Exception as e:
            logger.warning(f"[Distillation] micro-distill failed (non-fatal): {e}")

    def _basic_behavior_log(self, raw_input: str, classified: dict, source: str):
        """基础行为日志写入（distillation 模块可用前的兜底）。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            todos = classified.get("todos", [])
            for t in todos:
                conn.execute(
                    """INSERT INTO behavior_log
                       (raw_input, normalized, input_type, intent, confidence, decision, action, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        raw_input,
                        t.get("title", ""),
                        source,
                        t.get("intent", "record"),
                        t.get("confidence", 0.5),
                        "direct_execute" if t.get("permission_tier") == PermissionTier.EXECUTE else "suggest",
                        json.dumps({"todo_title": t.get("title", "")}, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(),
                    )
                )
            conn.commit()
        except Exception as e:
            logger.warning(f"[BehaviorLog] write failed: {e}")
        finally:
            conn.close()

    # ── 辅助方法 ─────────────────────────────────────────

    def _capture_context_snapshot(self) -> dict:
        """采集触发上下文快照（窗口/浏览器/剪贴板，前后5s）。"""
        try:
            ctx = summarize_window(minutes=CONTEXT_WINDOW_MINUTES)
            persist_summary(ctx, self.db_path)
            return asdict(ctx)
        except Exception:
            return {"summary_text": "无环境数据"}

    def _load_persona(self) -> dict:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM persona LIMIT 1").fetchone()
        conn.close()
        return dict(row) if row else {}

    def _skill_to_classified(self, skill: dict, user_input: str) -> dict:
        """将命中的 Skill 转换为 classified 格式。"""
        pipeline = json.loads(skill["pipeline"]) if _is_json(skill.get("pipeline", "")) else []
        return {
            "todos": [{
                "title": user_input[:30],
                "body": f"复用 Skill: {skill['name']}",
                "intent": "task",
                "priority": 2,
                "confidence": 0.85,
                "confidence_basis": f"命中 Skill '{skill['name']}' (成功率 {skill.get('success_rate', 0):.0%})",
                "tags": ["skill-reuse"],
                "due_at": None,
                "topology": {k: None for k in ["time_dim","location_dim","person_dim","cost_dim","priority_dim","status_dim"]},
                "scene_dims": {"type": "skill", "fields": {"skill_id": skill["id"]}},
                "info_gain_needed": False,
                "agent_hint": {"type": "execute", "query": json.dumps(pipeline, ensure_ascii=False)},
                "regret_window_seconds": REGRET_WINDOW_DEFAULT,
                "_skill_id": skill["id"],
            }],
            "buffer_meta": {"fragment_count": 1, "aggregated": False, "skill_hit": True},
        }

    # ── 公共工具方法 ──────────────────────────────────────

    def set_persona(self, role: str, domains: list, tools: list,
                    preferences: dict = None, work_address: str = None,
                    home_address: str = None, work_style: str = None):
        conn = sqlite3.connect(str(self.db_path))
        existing = conn.execute("SELECT id FROM persona LIMIT 1").fetchone()
        data = (
            role,
            json.dumps(domains, ensure_ascii=False),
            json.dumps(tools, ensure_ascii=False),
            json.dumps(preferences or {}, ensure_ascii=False),
            work_address or "",
            home_address or "",
            work_style or "",
        )
        if existing:
            conn.execute(
                """UPDATE persona SET role=?, domains=?, tools=?, preferences=?,
                   work_address=?, home_address=?, work_style=?,
                   updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                   WHERE id=?""",
                data + (existing[0],)
            )
        else:
            conn.execute(
                "INSERT INTO persona (role, domains, tools, preferences, work_address, home_address, work_style) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", data
            )
        conn.commit()
        conn.close()
        # 写入固化层
        self._write_fixed_pattern("角色", role, confidence=0.99)
        if work_address:
            self._write_fixed_pattern("工作地址", work_address, confidence=0.99)
        if home_address:
            self._write_fixed_pattern("家庭地址", home_address, confidence=0.99)

    def _write_fixed_pattern(self, key: str, value: str, confidence: float = 0.9):
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """INSERT INTO fixed_patterns (key, value, confidence, hit_count, created_at, updated_at)
                   VALUES (?, ?, ?, 0, strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, confidence=excluded.confidence,
                   updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
                (key, value, confidence)
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"[FixedLayer] write failed: {e}")
        finally:
            conn.close()

    def handle_feedback(self, todo_id: str, effective: Optional[bool], accurate: Optional[bool],
                        regret: bool = False):
        """
        接收 8 状态反馈矩阵标注。
        触发热蒸馏更新用户模型。
        """
        try:
            from .distillation import process_feedback
            process_feedback(
                db_path=self.db_path,
                todo_id=todo_id,
                effective=effective,
                accurate=accurate,
                regret=regret,
            )
        except ImportError:
            logger.warning("[Feedback] distillation module not available, logging only")
            self._log_feedback(todo_id, effective, accurate, regret)

    def _log_feedback(self, todo_id: str, effective, accurate, regret):
        conn = sqlite3.connect(str(self.db_path))
        try:
            state = _encode_feedback_state(effective, accurate, regret)
            conn.execute(
                "UPDATE behavior_log SET feedback=? WHERE id=? OR action LIKE ?",
                (state, todo_id, f'%"{todo_id}"%')
            )
            conn.execute(
                "UPDATE todos SET status=? WHERE id=?",
                ("cancelled" if regret else "done", todo_id)
            )
            conn.commit()
        finally:
            conn.close()

    def get_todos(self, status: str = None, limit: int = 50) -> list:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        if status:
            rows = conn.execute(
                "SELECT * FROM todos WHERE status=? ORDER BY priority DESC, created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM todos WHERE status NOT IN ('archived','cancelled') "
                "ORDER BY priority DESC, created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def search_todos(self, query: str, limit: int = 20) -> list:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT todos.* FROM todos_fts
                   JOIN todos ON todos.rowid = todos_fts.rowid
                   WHERE todos_fts MATCH ? ORDER BY rank LIMIT ?""",
                (query, limit)
            ).fetchall()
        except Exception:
            rows = conn.execute(
                "SELECT * FROM todos WHERE content LIKE ? OR title LIKE ? LIMIT ?",
                (f"%{query}%", f"%{query}%", limit)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]


def _encode_feedback_state(effective, accurate, regret) -> str:
    if regret:
        return "regret"
    if effective is not None and accurate is not None:
        parts = []
        parts.append("effective" if effective else "ineffective")
        parts.append("accurate" if accurate else "inaccurate")
        return "+".join(parts)
    if effective is not None:
        return "effective" if effective else "ineffective"
    if accurate is not None:
        return "accurate" if accurate else "inaccurate"
    return "unknown"
