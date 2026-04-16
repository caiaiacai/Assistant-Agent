"""
SATH · 蒸馏系统 v1.0
======================
蒸馏的本质：行为模式压缩，不是记忆存储。

蒸馏不依赖单一事件的重复出现，而是从多元不同的行为中提炼底层规律：
  · 见李总前整理BP / 见王总前整理竞品分析 / 见孙总前确认财务数字
  → 从8个完全不同的人/场景中蒸馏出：见重要人物前的固定行为规律
  → 陈总第一次出现，系统立刻知道该做什么

实现层级：
  - 热蒸馏（实时）：高权重信号触发，轻量更新相关维度，秒级生效
  - 冷蒸馏（每日凌晨）：低权重信号批处理，全量重压缩用户模型
  - 8状态反馈矩阵：数据输入最高权重入口
"""

import json
import sqlite3
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sath.distillation")


# ══════════════════════════════════════════════════════════
# 8状态反馈矩阵 蒸馏动作映射
# ══════════════════════════════════════════════════════════

# 状态 → (意图层权重变化, 执行层权重变化, 蒸馏级别)
FEEDBACK_MATRIX = {
    "effective+accurate":    (+0.15, +0.15, "hot"),   # 最强正向：热蒸馏/双层加权/Skill提炼候选
    "effective+inaccurate":  (+0.05, -0.05, "hot"),   # 歪打正着：意图降权/执行保留
    "ineffective+accurate":  (-0.05, +0.05, "hot"),   # 意图对没用：意图保留/执行降权
    "ineffective+inaccurate": (-0.15, -0.15, "hot"),  # 双重失败：双重降权/写入错误案例库
    "effective":             (+0.05, 0.0,   "cold"),  # 弱正向：冷蒸馏批处理
    "ineffective":           (-0.05, 0.0,   "cold"),  # 弱负向：冷蒸馏批处理
    "accurate":              (+0.05, 0.0,   "cold"),  # 意图正向：冷蒸馏
    "inaccurate":            (-0.05, 0.0,   "cold"),  # 意图负向：冷蒸馏
    "regret":                (-0.10, -0.10, "hot"),   # 后悔药触发：错误驱动校准
}

# 数据输入权重（用于蒸馏优先级）
INPUT_WEIGHTS = {
    "8state_feedback": 1.0,       # 8状态标注（最高质量）
    "regret": 0.9,                # 后悔药触发（错误驱动）
    "manual_setting": 0.85,       # 主动设置（角色/偏好）
    "buffer_result": 0.70,        # 缓冲窗口结果（执行/取消/修改）
    "semantic_input": 0.55,       # 随手记语义推断
    "execution_accept": 0.35,     # 执行结果接受（弱正向）
    "passive_collect": 0.15,      # 被动采集（噪声多）
}


# ══════════════════════════════════════════════════════════
# 热蒸馏（实时，秒级生效）
# ══════════════════════════════════════════════════════════

def hot_distill(
    db_path: Path,
    raw_input: str,
    classified: dict,
    source: str = "manual",
    feedback_state: Optional[str] = None,
):
    """
    热蒸馏：高权重信号触发，轻量更新相关维度。

    调用时机：
    - 每段对话结束后（微蒸馏）
    - 8状态反馈标注后
    - 后悔药触发后
    """
    conn = sqlite3.connect(str(db_path))
    try:
        todos = classified.get("todos", [])
        for todo in todos:
            intent = todo.get("intent", "record")
            confidence = float(todo.get("confidence", 0.5))
            topology = todo.get("topology", {})
            scene_dims = todo.get("scene_dims", {})

            # 写入行为日志
            _write_behavior_log(conn, raw_input, todo, source, feedback_state)

            # 提取人物信息 → 更新关系网络
            person_dim = topology.get("person_dim")
            if person_dim and isinstance(person_dim, str) and len(person_dim) > 1:
                _update_relation(conn, person_dim, raw_input)

            # 提炼节奏参数（时间维度）
            time_dim = topology.get("time_dim")
            if time_dim and intent in ("task", "reminder"):
                _update_rhythm_hint(conn, intent, time_dim)

            # 强正向信号 → 固化层晋升候选
            if feedback_state == "effective+accurate" and confidence >= 0.8:
                _candidate_fixed_pattern(conn, todo, raw_input)

        conn.commit()
        logger.debug(f"[HotDistill] processed {len(todos)} todos from '{raw_input[:40]}'")

    except Exception as e:
        logger.error(f"[HotDistill] error: {e}")
    finally:
        conn.close()


def process_feedback(
    db_path: Path,
    todo_id: str,
    effective: Optional[bool],
    accurate: Optional[bool],
    regret: bool = False,
):
    """
    处理 8 状态反馈矩阵标注。
    这是整个系统最高质量的学习入口。
    用户不是在"评价AI"，而是在整理自己的卡片——标注行为本身对用户有价值。
    """
    # 确定状态键
    if regret:
        state_key = "regret"
    elif effective is not None and accurate is not None:
        e_str = "effective" if effective else "ineffective"
        a_str = "accurate" if accurate else "inaccurate"
        state_key = f"{e_str}+{a_str}"
    elif effective is not None:
        state_key = "effective" if effective else "ineffective"
    elif accurate is not None:
        state_key = "accurate" if accurate else "inaccurate"
    else:
        return  # 无效反馈，忽略

    matrix_action = FEEDBACK_MATRIX.get(state_key)
    if not matrix_action:
        return

    intent_weight_delta, exec_weight_delta, distill_level = matrix_action

    conn = sqlite3.connect(str(db_path))
    try:
        # 1. 更新 todo 状态
        if regret:
            conn.execute("UPDATE todos SET status='cancelled' WHERE id=?", (todo_id,))
        else:
            conn.execute("UPDATE todos SET status='done' WHERE id=?", (todo_id,))

        # 2. 更新行为日志的反馈字段
        conn.execute(
            "UPDATE behavior_log SET feedback=? WHERE action LIKE ?",
            (state_key, f'%{todo_id}%')
        )

        # 3. 更新置信度权重（简化版：按 intent 类型调整）
        row = conn.execute(
            "SELECT intent FROM behavior_log WHERE action LIKE ? LIMIT 1",
            (f'%{todo_id}%',)
        ).fetchone()

        if row:
            intent_type = row[0] if isinstance(row, (list, tuple)) else row["intent"] if hasattr(row, "keys") else "record"
            _update_confidence_weight(conn, intent_type, intent_weight_delta)

        # 4. 热蒸馏写入（高权重状态）
        if distill_level == "hot":
            _write_distill_event(conn, todo_id, state_key, intent_weight_delta, weight=INPUT_WEIGHTS["8state_feedback"])

        # 5. 双重失败 → 写入错误案例库
        if state_key == "ineffective+inaccurate":
            _write_error_case(conn, todo_id, state_key)

        # 6. 强正向 → Skill 提炼候选
        if state_key == "effective+accurate":
            _mark_skill_candidate(conn, todo_id)

        conn.commit()
        logger.info(f"[Feedback] processed todo={todo_id}, state={state_key}")

    except Exception as e:
        logger.error(f"[Feedback] error: {e}")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# 冷蒸馏（每日凌晨批处理）
# ══════════════════════════════════════════════════════════

def cold_distill(db_path: Path, llm_provider: str = None, llm_model: str = None, api_key: str = None):
    """
    冷蒸馏：每日凌晨批处理，全量重压缩用户模型。
    低权重信号批处理，不影响实时体验。
    """
    logger.info("[ColdDistill] 开始冷蒸馏批处理")
    conn = sqlite3.connect(str(db_path))
    try:
        # 1. 收集过去24小时的行为日志
        yesterday = _hours_ago(24)
        logs = conn.execute(
            "SELECT * FROM behavior_log WHERE created_at >= ? ORDER BY created_at DESC",
            (yesterday,)
        ).fetchall()

        if not logs:
            logger.info("[ColdDistill] 无新行为日志，跳过")
            return

        # 2. 聚合分析行为模式
        intent_freq = {}
        for log in logs:
            intent = log["intent"] if hasattr(log, "keys") else log[4]
            intent_freq[intent] = intent_freq.get(intent, 0) + 1

        # 3. 更新活跃层频率统计
        active_prefs = _load_json_setting(conn, "active_preferences") or {}
        for intent, count in intent_freq.items():
            key = f"daily_{intent}_count"
            active_prefs[key] = count
        active_prefs["last_cold_distill"] = datetime.now(timezone.utc).isoformat()
        _save_json_setting(conn, "active_preferences", active_prefs)

        # 4. 固化层晋升/降级检查
        _check_fixed_pattern_promotion(conn)
        _check_fixed_pattern_demotion(conn)

        # 5. LLM 深度蒸馏（有 LLM 配置时启用）
        if llm_provider and llm_model and len(logs) >= 5:
            _llm_deep_distill(conn, logs, llm_provider, llm_model, api_key)

        conn.commit()
        logger.info(f"[ColdDistill] 完成，处理 {len(logs)} 条行为日志")

    except Exception as e:
        logger.error(f"[ColdDistill] error: {e}")
    finally:
        conn.close()


def schedule_cold_distill(db_path: Path, hour: int = 3, **kwargs):
    """
    调度冷蒸馏（每日 hour 时执行）。
    返回调度线程，调用方负责保持引用。
    """
    import time as _time

    def _runner():
        while True:
            now = datetime.now()
            # 计算到下次凌晨 hour 点的秒数
            next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if next_run <= now:
                import datetime as dt
                next_run += dt.timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()
            logger.info(f"[ColdDistill] 下次执行: {next_run.isoformat()} (等待 {wait_seconds/3600:.1f}h)")
            _time.sleep(wait_seconds)
            cold_distill(db_path, **kwargs)

    t = threading.Thread(target=_runner, daemon=True, name="sath-cold-distill")
    t.start()
    return t


# ══════════════════════════════════════════════════════════
# 内部辅助函数
# ══════════════════════════════════════════════════════════

def _write_behavior_log(conn, raw_input: str, todo: dict, source: str, feedback: str = None):
    """写入行为日志。"""
    try:
        conn.execute(
            """INSERT OR IGNORE INTO behavior_log
               (raw_input, normalized, input_type, intent, confidence, decision, action, feedback, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                raw_input[:500],
                todo.get("title", "")[:200],
                source,
                todo.get("intent", "record"),
                float(todo.get("confidence", 0.5)),
                todo.get("permission_tier", "suggest"),
                json.dumps({
                    "todo_title": todo.get("title", ""),
                    "topology": todo.get("topology", {}),
                }, ensure_ascii=False)[:1000],
                feedback or "",
                datetime.now(timezone.utc).isoformat(),
            )
        )
    except Exception as e:
        logger.warning(f"[BehaviorLog] write failed: {e}")


def _update_relation(conn, person_name: str, context: str):
    """更新关系网络：人名出现则增加计数/更新 last_seen。"""
    try:
        existing = conn.execute(
            "SELECT id, seen_count FROM relations WHERE name=?", (person_name,)
        ).fetchone()
        now = datetime.now(timezone.utc).isoformat()
        if existing:
            conn.execute(
                "UPDATE relations SET seen_count=seen_count+1, last_seen=? WHERE name=?",
                (now, person_name)
            )
        else:
            conn.execute(
                "INSERT INTO relations (name, role, seen_count, last_seen, notes, created_at) "
                "VALUES (?, '', 1, ?, ?, ?)",
                (person_name, now, context[:200], now)
            )
    except Exception as e:
        logger.debug(f"[Relation] update failed: {e}")


def _update_rhythm_hint(conn, intent: str, time_dim: str):
    """根据时间维度更新节奏参数提示。"""
    try:
        params = _load_json_setting(conn, "rhythm_params") or {}
        hints = params.get("time_hints", {})
        hints[intent] = time_dim[:100]
        params["time_hints"] = hints
        _save_json_setting(conn, "rhythm_params", params)
    except Exception as e:
        logger.debug(f"[Rhythm] update failed: {e}")


def _candidate_fixed_pattern(conn, todo: dict, raw_input: str):
    """将强正向意图标记为固化层晋升候选。"""
    try:
        key = f"pattern_{todo.get('intent', 'task')}_{todo.get('title', '')[:20]}"
        conn.execute(
            """INSERT INTO fixed_patterns (key, value, confidence, hit_count, created_at, updated_at)
               VALUES (?, ?, 0.6, 1, strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'))
               ON CONFLICT(key) DO UPDATE SET
               hit_count=hit_count+1,
               confidence=MIN(0.99, confidence+0.05),
               updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
            (key, json.dumps({"title": todo.get("title"), "input_sample": raw_input[:100]}, ensure_ascii=False))
        )
    except Exception as e:
        logger.debug(f"[FixedLayer] candidate failed: {e}")


def _update_confidence_weight(conn, intent_type: str, delta: float):
    """更新意图类型的置信度权重（影响未来分类准确率）。"""
    try:
        key = f"confidence_weight_{intent_type}"
        existing = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        current = float(existing[0]) if existing else 0.5
        new_val = max(0.1, min(0.99, current + delta))
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(new_val))
        )
    except Exception as e:
        logger.debug(f"[ConfWeight] update failed: {e}")


def _write_distill_event(conn, todo_id: str, state: str, weight_delta: float, weight: float = 1.0):
    """写入蒸馏事件记录。"""
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS distill_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               todo_id TEXT,
               state TEXT,
               weight_delta REAL,
               input_weight REAL,
               created_at TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO distill_events (todo_id, state, weight_delta, input_weight, created_at) VALUES (?,?,?,?,?)",
            (todo_id, state, weight_delta, weight, datetime.now(timezone.utc).isoformat())
        )
    except Exception as e:
        logger.debug(f"[DistillEvent] write failed: {e}")


def _write_error_case(conn, todo_id: str, state: str):
    """双重失败 → 写入错误案例库，供意图层降权参考。"""
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS error_cases (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               todo_id TEXT,
               state TEXT,
               created_at TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO error_cases (todo_id, state, created_at) VALUES (?,?,?)",
            (todo_id, state, datetime.now(timezone.utc).isoformat())
        )
    except Exception as e:
        logger.debug(f"[ErrorCase] write failed: {e}")


def _mark_skill_candidate(conn, todo_id: str):
    """强正向意图 → 标记为 Skill 提炼候选。"""
    try:
        conn.execute(
            "UPDATE todos SET status='skill_candidate' WHERE id=? AND status='done'",
            (todo_id,)
        )
    except Exception as e:
        logger.debug(f"[SkillCandidate] mark failed: {e}")


def _check_fixed_pattern_promotion(conn):
    """固化层晋升：时间窗口内占比 > 阈值 且 持续跨度 > N 天。"""
    try:
        threshold_days = 7
        threshold_confidence = 0.85
        conn.execute(
            """UPDATE fixed_patterns SET
               confidence = MIN(0.99, confidence + 0.02)
               WHERE hit_count >= 5
               AND confidence < ?
               AND julianday('now') - julianday(created_at) >= ?""",
            (threshold_confidence, threshold_days)
        )
    except Exception as e:
        logger.debug(f"[FixedLayer] promotion check failed: {e}")


def _check_fixed_pattern_demotion(conn):
    """固化层降级：长期未命中则退回活跃层。"""
    try:
        conn.execute(
            """UPDATE fixed_patterns SET confidence = MAX(0.3, confidence - 0.03)
               WHERE julianday('now') - julianday(updated_at) > 30
               AND confidence < 0.90""",
        )
    except Exception as e:
        logger.debug(f"[FixedLayer] demotion check failed: {e}")


def _llm_deep_distill(conn, logs, provider: str, model: str, api_key: str):
    """
    LLM 深度蒸馏：从多条行为日志中提炼底层规律。
    异步后台跑，不占主流程资源。
    """
    try:
        # 构建蒸馏 prompt
        log_texts = []
        for log in logs[:20]:
            if hasattr(log, "keys"):
                log_texts.append(f"- {log['intent']}: {log['normalized'] or log['raw_input'][:80]}")
            else:
                log_texts.append(f"- {log[4]}: {log[1][:80]}")

        prompt = (
            "以下是用户最近24小时的行为记录摘要，请从中提炼2-3个底层行为规律或偏好模式（不是重复事件）：\n\n"
            + "\n".join(log_texts)
            + "\n\n请以 JSON 数组返回，每项格式：{\"pattern\": \"描述\", \"category\": \"类别\"}"
        )

        from ..prompts.intent_classifier import _parse_response
        import urllib.request
        import json as _json

        if provider == "openai":
            cfg = _load_json_setting(conn, "llm_config") or {}
            base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
            key = api_key or cfg.get("api_key", "")
            if not key:
                return
            body = _json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            }).encode()
            req = urllib.request.Request(
                f"{base_url}/chat/completions", data=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                result_text = _json.loads(r.read())["choices"][0]["message"]["content"]
        else:
            return  # MVP 阶段仅支持 openai 兼容 API

        patterns = _parse_response(result_text)
        if isinstance(patterns, list):
            for p in patterns[:3]:
                if isinstance(p, dict) and p.get("pattern"):
                    key = f"llm_distill_{p.get('category', 'general')}_{p['pattern'][:20]}"
                    conn.execute(
                        """INSERT INTO fixed_patterns (key, value, confidence, hit_count, created_at, updated_at)
                           VALUES (?, ?, 0.55, 1, strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                           ON CONFLICT(key) DO NOTHING""",
                        (key, _json.dumps(p, ensure_ascii=False))
                    )

        logger.info(f"[LLMDistill] 提炼 {len(patterns) if isinstance(patterns, list) else 0} 条行为规律")

    except Exception as e:
        logger.warning(f"[LLMDistill] deep distill failed (non-fatal): {e}")


# ── 工具函数 ──────────────────────────────────────────────

def _load_json_setting(conn, key: str) -> Optional[dict]:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row:
            v = row[0] if isinstance(row, (list, tuple)) else row["value"]
            return json.loads(v) if v else None
    except Exception:
        pass
    return None


def _save_json_setting(conn, key: str, value: dict):
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False))
        )
    except Exception as e:
        logger.debug(f"[Settings] save failed: {e}")


def _hours_ago(hours: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
