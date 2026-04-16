"""
SATH · 编排 Agent v1.0
========================
核心定位：用户唯一的对话界面。

新结构（真秘书思维）：
  用户
  ↕ （唯一通道）
  编排 Agent（秘书）
  ↕ （内部调度）
  子 Agent 们（执行层，对用户透明）

设计原则：
  1. 做完告知，不问用户 —— 缺失信息用蒸馏数据填或做最优猜测
  2. 常驻机制 —— 心跳扫描，主动感知环境，不只是被动等待
  3. 无状态 —— 蒸馏层承担记忆，编排 Agent 每次注入快照
  4. 25轮保底 —— 第24轮催促，第25轮强制收回
  5. 主动信息增益 —— 灵感/策略类意图自动后台搜索

推送格式（严格三字段）：
  - 做了什么（一句话）
  - 时间
  - 后悔药入口
  不返回：解释/询问/展开聊天/多余的确认
"""

import json
import sqlite3
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger("sath.orchestrator")

MAX_AGENT_TURNS = 25
TURN_PROMPT_THRESHOLD = 24
HEARTBEAT_INTERVAL = 60         # 心跳扫描间隔（秒）
INFO_GAIN_DELAY_SECONDS = 30    # 主动信息增益推送延迟（秒）


# ══════════════════════════════════════════════════════════
# 推送消息构建器（严格三字段）
# ══════════════════════════════════════════════════════════

def build_push_message(
    action_summary: str,
    time_info: str = None,
    regret_window_seconds: int = 300,
    todo_id: str = None,
    tier: str = "execute",
) -> dict:
    """
    构建推送消息。严格限制三个字段：
    - 做了什么
    - 时间
    - 后悔药入口

    不返回：解释、询问、展开聊天、多余确认。
    """
    msg = {
        "action": action_summary,
        "time": time_info or datetime.now(timezone.utc).strftime("%m月%d日 %H:%M"),
        "regret_token": todo_id or "",
        "regret_hint": f"{regret_window_seconds // 60}分钟内回复「取消」可撤回" if tier == "execute" else None,
    }
    return msg


def format_push_text(msg: dict) -> str:
    """将推送消息格式化为文字（微信/通知推送）。"""
    parts = [msg["action"]]
    if msg.get("time"):
        parts.append(msg["time"])
    if msg.get("regret_hint"):
        parts.append(msg["regret_hint"])
    return "\n".join(parts)


# ══════════════════════════════════════════════════════════
# 编排 Agent
# ══════════════════════════════════════════════════════════

class OrchestratorAgent:
    """
    编排 Agent —— 用户的唯一秘书界面。

    职责：
    - 接收来自 Pipeline 的 classified 意图结果
    - 调度子 Agent 执行（子 Agent 永远不直接输出给用户）
    - 收回所有子 Agent 结果
    - 自己决定怎么告诉用户（严格三字段格式）
    - 自己决定需不需要问用户（原则上不问，用蒸馏数据填补）
    - 心跳扫描（日历/截止/异常信号）
    """

    def __init__(
        self,
        db_path: Path,
        push_fn=None,                   # fn(user_id, message: dict)，推送给用户的函数
        llm_call_fn=None,               # fn(messages) → str，调用 LLM
    ):
        self.db_path = db_path
        self.push_fn = push_fn or _noop_push
        self.llm_call_fn = llm_call_fn
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._running = False
        self._sub_agents: Dict[str, dict] = {}   # todo_id → agent_state

    def start(self):
        """启动编排 Agent（含常驻心跳）。"""
        self._running = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="sath-orchestrator-heartbeat"
        )
        self._heartbeat_thread.start()
        logger.info("[Orchestrator] started (heartbeat active)")

    def stop(self):
        self._running = False
        logger.info("[Orchestrator] stopped")

    # ── 主处理入口 ─────────────────────────────────────────

    def orchestrate(
        self,
        classified: dict,
        user_model_snapshot: dict,
        user_id: str = "default",
    ) -> dict:
        """
        编排入口：接收 classified 意图，完成处理后推送给用户。
        子 Agent 不直接输出，结果全部汇聚到这里。
        """
        todos = classified.get("todos", [])
        results = []

        for todo in todos:
            result = self._handle_todo(todo, user_model_snapshot, user_id)
            results.append(result)

        return {"status": "orchestrated", "results": results, "count": len(results)}

    def _handle_todo(self, todo: dict, user_model: dict, user_id: str) -> dict:
        """处理单个 TODO 的完整编排流程。"""
        intent = todo.get("intent", "record")
        tier = todo.get("permission_tier", "suggest")
        confidence = float(todo.get("confidence", 0.5))
        todo_id = todo.get("id", _generate_id())

        # 填补缺失信息（蒸馏数据优先，不询问用户）
        enriched = self._fill_missing_info(todo, user_model)

        # 主动信息增益（灵感/策略类，后台触发）
        if enriched.get("info_gain_needed") and intent in ("record", "research"):
            threading.Thread(
                target=self._trigger_info_gain,
                args=(enriched, user_id),
                daemon=True,
            ).start()

        # 根据权限档决定行动
        if tier == "execute":
            return self._execute_and_notify(enriched, user_model, user_id, todo_id)
        elif tier == "suggest":
            return self._suggest_to_user(enriched, user_id, todo_id)
        else:  # inform
            return self._inform_user(enriched, user_id, todo_id)

    # ── 执行档（直接办，缓冲窗口后悔）────────────────────

    def _execute_and_notify(self, todo: dict, user_model: dict, user_id: str, todo_id: str) -> dict:
        """执行档：直接执行，推送结果，给后悔窗口。"""
        action_summary = self._summarize_action(todo, user_model)
        regret_seconds = int(todo.get("regret_window_seconds", 300))

        msg = build_push_message(
            action_summary=action_summary,
            time_info=_format_time_info(todo),
            regret_window_seconds=regret_seconds,
            todo_id=todo_id,
            tier="execute",
        )
        self.push_fn(user_id, msg)
        self._save_pending_action(todo_id, todo, regret_seconds)

        logger.info(f"[Orchestrator] execute → push: {action_summary[:60]}")
        return {"todo_id": todo_id, "action": "executed", "message": msg}

    # ── 建议档（方案准备好，等用户点头）─────────────────

    def _suggest_to_user(self, todo: dict, user_id: str, todo_id: str) -> dict:
        """建议档：帮用户想好方案，推送方案，等确认。"""
        title = todo.get("title", "")
        intent = todo.get("intent", "task")
        confidence_basis = todo.get("confidence_basis", "")

        action_summary = f"[待确认] {title}"
        if confidence_basis:
            action_summary += f"（{confidence_basis}）"

        msg = build_push_message(
            action_summary=action_summary,
            time_info=_format_time_info(todo),
            regret_window_seconds=0,
            todo_id=todo_id,
            tier="suggest",
        )
        msg["action_type"] = "suggest"
        msg["confirm_token"] = todo_id
        self.push_fn(user_id, msg)

        logger.info(f"[Orchestrator] suggest → push: {title[:60]}")
        return {"todo_id": todo_id, "action": "suggested", "message": msg}

    # ── 告知档（发现了，通知一声）────────────────────────

    def _inform_user(self, todo: dict, user_id: str, todo_id: str) -> dict:
        """告知档：发现了，通知用户，不执行。"""
        title = todo.get("title", "")
        msg = build_push_message(
            action_summary=f"[已记录] {title}",
            time_info=_format_time_info(todo),
            regret_window_seconds=0,
            todo_id=todo_id,
            tier="inform",
        )
        msg["action_type"] = "inform"
        self.push_fn(user_id, msg)

        logger.info(f"[Orchestrator] inform → push: {title[:60]}")
        return {"todo_id": todo_id, "action": "informed", "message": msg}

    # ── 填补缺失信息（蒸馏数据 > 角色默认值 > 最优猜测）──

    def _fill_missing_info(self, todo: dict, user_model: dict) -> dict:
        """用蒸馏数据填补 TODO 缺失维度，不询问用户。"""
        topology = todo.get("topology", {})
        fixed_patterns = {p["key"]: p["value"] for p in user_model.get("fixed_patterns", [])}
        active = user_model.get("active_preferences", {})

        # 地点维度
        if not topology.get("location_dim"):
            work_addr = fixed_patterns.get("工作地址") or active.get("default_location")
            if work_addr:
                topology["location_dim"] = work_addr
                todo["_location_inferred"] = True

        # 人物维度 → 尝试从关系网络找
        if topology.get("person_dim"):
            person_name = str(topology["person_dim"])
            relations = user_model.get("recent_relations", [])
            for r in relations:
                if r.get("name") and person_name in r["name"]:
                    topology["person_context"] = r
                    break

        # 成本维度
        if not topology.get("cost_dim"):
            default_budget = fixed_patterns.get("默认预算") or active.get("default_budget")
            if default_budget:
                topology["cost_dim"] = default_budget

        todo["topology"] = topology
        return todo

    # ── 主动信息增益（灵感/策略类）────────────────────────

    def _trigger_info_gain(self, todo: dict, user_id: str):
        """
        主动信息增益：用户记录灵感/策略时，后台自动触发联网搜索。
        不立刻打扰用户，等最佳时机推送（下次打开 / 当天整理时间）。
        """
        query = todo.get("info_gain_query") or todo.get("title", "")
        if not query:
            return

        time.sleep(INFO_GAIN_DELAY_SECONDS)  # 等一会，避免立刻打扰

        logger.info(f"[InfoGain] 后台搜索: {query[:60]}")
        # 实际实现：调用 tool_web_search → 整理结果 → 延迟推送
        # MVP 阶段：仅记录搜索意图到数据库
        self._save_info_gain_task(query, todo.get("id", ""), user_id)

    def _save_info_gain_task(self, query: str, todo_id: str, user_id: str):
        """将信息增益搜索任务写入数据库，由 Agent 队列异步处理。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS info_gain_tasks (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   query TEXT,
                   todo_id TEXT,
                   user_id TEXT,
                   status TEXT DEFAULT 'pending',
                   result TEXT,
                   created_at TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO info_gain_tasks (query, todo_id, user_id, status, created_at) VALUES (?,?,?,'pending',?)",
                (query, todo_id, user_id, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
        except Exception as e:
            logger.debug(f"[InfoGain] save task failed: {e}")
        finally:
            conn.close()

    # ── 动作摘要生成 ──────────────────────────────────────

    def _summarize_action(self, todo: dict, user_model: dict) -> str:
        """
        生成一句话执行摘要（不超过50字）。
        优先使用 title，用拓扑维度补充关键信息。
        """
        title = todo.get("title", "")
        topology = todo.get("topology", {})
        time_dim = topology.get("time_dim")
        location_dim = topology.get("location_dim")
        person_dim = topology.get("person_dim")

        parts = [title]
        if person_dim:
            parts.append(str(person_dim)[:15])
        if location_dim:
            parts.append(str(location_dim)[:15])

        summary = " · ".join(p for p in parts if p)
        return summary[:60]

    # ── 存储待执行动作（缓冲窗口期）──────────────────────

    def _save_pending_action(self, todo_id: str, todo: dict, regret_seconds: int):
        """将待执行动作写入数据库（缓冲窗口期内可撤回）。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            deadline = (datetime.now(timezone.utc) + timedelta(seconds=regret_seconds)).isoformat()
            conn.execute(
                """CREATE TABLE IF NOT EXISTS pending_actions (
                   id TEXT PRIMARY KEY,
                   todo_data TEXT,
                   regret_deadline TEXT,
                   status TEXT DEFAULT 'pending',
                   created_at TEXT
                )"""
            )
            conn.execute(
                "INSERT OR REPLACE INTO pending_actions (id, todo_data, regret_deadline, status, created_at) VALUES (?,?,?,'pending',?)",
                (todo_id, json.dumps(todo, ensure_ascii=False), deadline, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
        except Exception as e:
            logger.debug(f"[PendingAction] save failed: {e}")
        finally:
            conn.close()

    def handle_regret(self, todo_id: str) -> bool:
        """处理后悔药：缓冲窗口内取消。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                "SELECT regret_deadline, status FROM pending_actions WHERE id=?",
                (todo_id,)
            ).fetchone()
            if not row:
                return False
            deadline_str = row[0] if isinstance(row, (list, tuple)) else row["regret_deadline"]
            status = row[1] if isinstance(row, (list, tuple)) else row["status"]
            if status != "pending":
                return False
            deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > deadline:
                return False  # 后悔窗口已关闭
            conn.execute("UPDATE pending_actions SET status='cancelled' WHERE id=?", (todo_id,))
            conn.execute("UPDATE todos SET status='cancelled' WHERE id=?", (todo_id,))
            conn.commit()
            logger.info(f"[Orchestrator] regret applied: todo={todo_id}")
            return True
        except Exception as e:
            logger.error(f"[Orchestrator] regret failed: {e}")
            return False
        finally:
            conn.close()

    # ── 常驻心跳（主动感知环境）──────────────────────────

    def _heartbeat_loop(self):
        """
        常驻心跳：定时扫描日历/截止日期/未完成任务/异常信号。
        常驻 ≠ 什么都推，优先级过滤是核心约束。
        """
        while self._running:
            try:
                self._scan_proactive_signals()
            except Exception as e:
                logger.warning(f"[Heartbeat] scan error: {e}")
            time.sleep(HEARTBEAT_INTERVAL)

    def _scan_proactive_signals(self):
        """扫描主动触发信号。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        now = datetime.now(timezone.utc)

        try:
            # 1. 即将到期的待办（2小时内）
            upcoming_deadline = (now + timedelta(hours=2)).isoformat()
            overdue_rows = conn.execute(
                """SELECT id, content, title, due_at FROM todos
                   WHERE status='pending' AND due_at IS NOT NULL AND due_at <= ?
                   AND COALESCE(reminder_fired, 0) = 0""",
                (upcoming_deadline,)
            ).fetchall()

            for row in overdue_rows:
                urgency = "high" if row["due_at"] <= now.isoformat() else "medium"
                if urgency == "high":
                    self.push_fn("default", build_push_message(
                        action_summary=f"⚠️ 即将到期: {row['title'] or row['content'][:30]}",
                        time_info=row["due_at"][:16],
                        regret_window_seconds=0,
                        todo_id=str(row["id"]),
                        tier="inform",
                    ))
                    conn.execute(
                        "UPDATE todos SET reminder_fired=1 WHERE id=?",
                        (row["id"],)
                    )

            # 2. 信息增益任务处理（有结果则推送）
            gain_rows = conn.execute(
                "SELECT id, query, result FROM info_gain_tasks WHERE status='done' LIMIT 3"
            ).fetchall()
            for row in gain_rows:
                if row["result"]:
                    self.push_fn("default", {
                        "action": f"「{row['query'][:20]}」已为你找到相关内容",
                        "detail": row["result"][:300],
                        "type": "info_gain",
                    })
                    conn.execute(
                        "UPDATE info_gain_tasks SET status='pushed' WHERE id=?",
                        (row["id"],)
                    )

            conn.commit()

        except Exception as e:
            logger.debug(f"[Heartbeat] scan detail failed: {e}")
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════
# 25轮保底机制（子 Agent 执行层）
# ══════════════════════════════════════════════════════════

def agent_turn_guard(turn: int, max_turns: int = MAX_AGENT_TURNS) -> Optional[str]:
    """
    25轮保底机制。
    第24轮：触发催促消息注入 system。
    第25轮：强制收回。
    """
    if turn >= max_turns:
        return "__FORCE_STOP__"
    if turn == TURN_PROMPT_THRESHOLD:
        return "请在下一轮给出最终结果，即使不完整。不要再请求更多信息或工具调用。"
    return None


def run_agent_with_turn_guard(
    agent_fn,                    # fn(messages, turn) → (response, done)
    initial_messages: list,
    max_turns: int = MAX_AGENT_TURNS,
    todo_id: str = None,
) -> dict:
    """
    带25轮保底的 Agent 执行包装器。
    子 Agent 的所有执行都应通过此函数，确保不无限循环。
    """
    messages = list(initial_messages)
    turn = 0
    partial_result = None

    while turn < max_turns:
        # 检查轮次保底
        guard_msg = agent_turn_guard(turn, max_turns)
        if guard_msg == "__FORCE_STOP__":
            logger.warning(f"[TurnGuard] todo={todo_id} hit max turns, forcing stop")
            return {
                "success": False,
                "result": partial_result,
                "reason": "max_turns_exceeded",
                "turns": turn,
            }
        if guard_msg:
            # 第24轮：注入催促
            messages.append({"role": "system", "content": guard_msg})

        try:
            response, done = agent_fn(messages, turn)
            partial_result = response
            if done:
                return {"success": True, "result": response, "turns": turn + 1}
            messages.append({"role": "assistant", "content": str(response)})
            turn += 1
        except Exception as e:
            logger.error(f"[TurnGuard] turn {turn} failed: {e}")
            return {"success": False, "result": partial_result, "reason": str(e), "turns": turn}

    return {"success": False, "result": partial_result, "reason": "loop_ended", "turns": turn}


# ══════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════

def _format_time_info(todo: dict) -> str:
    """格式化时间信息用于推送。"""
    due_at = todo.get("due_at") or todo.get("topology", {}).get("time_dim")
    if due_at and isinstance(due_at, str) and len(due_at) >= 10:
        try:
            dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
            return dt.strftime("%m月%d日 %H:%M")
        except Exception:
            return str(due_at)[:16]
    return datetime.now(timezone.utc).strftime("%m月%d日 %H:%M")


def _generate_id() -> str:
    import uuid
    return str(uuid.uuid4())[:8]


def _noop_push(user_id: str, message: dict):
    """无操作推送（测试用）。"""
    logger.debug(f"[Push→{user_id}] {message.get('action', '')[:60]}")
