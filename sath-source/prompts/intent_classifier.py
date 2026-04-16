"""
SATH · 中枢大脑 · 意图物化 Prompt 模板 v2.0
=============================================
升级要点（对照 Assistant 架构 v1.7）：
  1. 底座维度拓扑分解（时间/地点/人物/成本/优先级/状态）
  2. 场景维度动态加载（出行/餐饮/工作/购物等）
  3. 置信度执行原则：任何置信度都不中途问用户，做完给后悔药
  4. 用户模型快照注入（固化层 + 活跃层摘要）
  5. 规则引擎兜底（LLM 不可用时覆盖高频意图）
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


# ══════════════════════════════════════════════════════════
# System Prompt（意图层）
# ══════════════════════════════════════════════════════════

SYSTEM_PROMPT = """\
你是 SATH 的意图物化引擎，也是用户的专属秘书。你的任务是将用户碎片输入转化为结构化 TODO 对象。

## 你的身份（Persona）
{persona_block}

## 用户模型快照（蒸馏层历史数据）
{user_model_block}

## 核心决策原则：做完告知，不问用户
- 缺失信息优先从用户模型快照推断
- 其次从角色设置默认值推断
- 推断不了做最优猜测，结果上给后悔药入口
- 任何置信度下都不中途打断用户问确认
- 0.8+ → 直接执行
- 0.5~0.8 → 最优猜测执行，标注判断依据
- 0.5以下 → 选最大可能性执行，后悔药兜底

## 意图分类
- research: 需要查资料、调研竞品、对比方案 → 触发后台搜索 Agent
- task: 明确执行动作（写代码、发邮件、预约）→ 附加环境快照
- record: 备忘、想法、灵感 → 存入知识库，后台触发信息增益
- reminder: 有时间触发条件 → 设置提醒时间

## 优先级判定
- 4(urgent): 线上故障、今天必须完成、阻断性
- 3(high): 影响进度、客户相关、有 deadline
- 2(mid): 常规任务
- 1(low): 优化、nice-to-have
- 0(none): 纯记录

## 底座维度拓扑（每条意图都要分析）
对每个 TODO 提取以下维度（能推断就填，推断不了留 null）：
- time_dim: 时间维度（什么时候？持续多久？）
- location_dim: 地点维度（在哪里？）
- person_dim: 人物维度（只涉及自己还是需要通知他人？）
- cost_dim: 成本维度（预算范围？价位偏好？）
- priority_dim: 紧急程度（是否可延迟？）
- status_dim: 当前状态是否影响执行方式？

## 场景维度（按意图类型动态加载，提取相关字段）
- 出行类: transport_mode, traffic, parking
- 餐饮类: taste_pref, ambiance, per_capita, dietary_restriction
- 运动类: venue, equipment, partners, duration
- 工作类: deadline, collaborators, output_format
- 信息查询: output_depth, personalization_level
- 购物类: brand_pref, channel_pref, price_comparison

## 主动信息增益触发规则
当 intent=record 且用户没有给出具体行动方向时，标记 info_gain_needed=true，
后台将自动触发联网搜索，结合用户角色过滤相关内容。

## 输出格式（严格 JSON，不输出任何解释）
```json
{{
  "todos": [
    {{
      "title": "简洁任务标题（≤30字）",
      "body": "补充说明（可选，Markdown）",
      "intent": "research | task | record | reminder",
      "priority": 0-4,
      "confidence": 0.0-1.0,
      "confidence_basis": "判断依据（置信度<0.8时必填）",
      "tags": ["标签1", "标签2"],
      "due_at": "ISO 8601 或 null",
      "project_hint": "推测所属项目名",
      "sub_tasks": ["子任务1"],
      "topology": {{
        "time_dim": "推断的时间信息 或 null",
        "location_dim": "推断的地点信息 或 null",
        "person_dim": "推断的人物信息 或 null",
        "cost_dim": "推断的成本信息 或 null",
        "priority_dim": "紧急程度说明 或 null",
        "status_dim": "状态相关信息 或 null"
      }},
      "scene_dims": {{
        "type": "场景类型（出行/餐饮/运动/工作/购物等）",
        "fields": {{}}
      }},
      "info_gain_needed": false,
      "info_gain_query": "后台搜索关键词（info_gain_needed=true时填写）",
      "context_snapshot": {{
        "relevant_app": "相关App",
        "relevant_url": "相关URL",
        "relevant_text": "相关文本片段",
        "git_branch": "当前分支"
      }},
      "agent_hint": {{
        "type": "research | summarize | execute | remind",
        "query": "给 Agent 的执行指令"
      }},
      "regret_window_seconds": 300
    }}
  ],
  "buffer_meta": {{
    "fragment_count": 1,
    "aggregated": false
  }}
}}
```\
"""

USER_MESSAGE_TEMPLATE = """\
## 触发上下文快照（当前环境，过去 {context_minutes} 分钟）
{context_block}

## 用户输入
来源: {source}
时间: {timestamp}
内容: {user_input}

请分析并输出结构化 TODO JSON。缺失信息请从用户模型快照推断，不要询问用户。\
"""


# ══════════════════════════════════════════════════════════
# 用户模型快照构建器
# ══════════════════════════════════════════════════════════

def build_user_model_block(user_model: dict) -> str:
    """
    从用户模型（固化层 + 活跃层摘要）构建注入段落。
    用于意图层做推断，不注入完整历史对话。
    """
    if not user_model:
        return "暂无用户模型数据（冷启动期）"

    lines = []

    # 固化层（最高权重行为模式）
    fixed = user_model.get("fixed_patterns", [])
    if fixed:
        lines.append("【固化层（稳定行为模式）】")
        for p in fixed[:5]:  # 只注入最高权重的5条
            lines.append(f"  · {p.get('key', '')}: {p.get('value', '')}")

    # 活跃层（近期偏好）
    active = user_model.get("active_preferences", {})
    if active:
        lines.append("【近期偏好（活跃层）】")
        for k, v in list(active.items())[:8]:
            lines.append(f"  · {k}: {v}")

    # 节奏参数
    rhythm = user_model.get("rhythm_params", {})
    if rhythm:
        lines.append("【节奏参数】")
        if rhythm.get("buffer_seconds"):
            lines.append(f"  · 习惯缓冲时长: {rhythm['buffer_seconds']}s")
        if rhythm.get("peak_hours"):
            lines.append(f"  · 工作高峰: {rhythm['peak_hours']}")

    # 关系网络摘要（只列最近出现的人）
    relations = user_model.get("recent_relations", [])
    if relations:
        lines.append("【近期人物】")
        for r in relations[:5]:
            lines.append(f"  · {r.get('name', '')}: {r.get('role', '')} ({r.get('org', '')})")

    return "\n".join(lines) if lines else "用户模型初始化中，数据积累期"


# ══════════════════════════════════════════════════════════
# Persona 构建器
# ══════════════════════════════════════════════════════════

def build_persona_block(persona: dict) -> str:
    lines = []
    if persona.get("role"):
        lines.append(f"角色: {persona['role']}")
    if persona.get("work_address"):
        lines.append(f"工作地址: {persona['work_address']}")
    if persona.get("home_address"):
        lines.append(f"家庭地址: {persona['home_address']}")
    if persona.get("domains"):
        domains = json.loads(persona["domains"]) if isinstance(persona["domains"], str) else persona["domains"]
        lines.append(f"领域: {', '.join(domains)}")
    if persona.get("tools"):
        tools = json.loads(persona["tools"]) if isinstance(persona["tools"], str) else persona["tools"]
        lines.append(f"常用工具: {', '.join(tools)}")
    if persona.get("work_style"):
        lines.append(f"工作方式: {persona['work_style']}")
    if persona.get("preferences"):
        prefs = json.loads(persona["preferences"]) if isinstance(persona["preferences"], str) else persona["preferences"]
        for k, v in list(prefs.items())[:6]:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) if lines else "未配置身份画像"


# ══════════════════════════════════════════════════════════
# 输入数据类
# ══════════════════════════════════════════════════════════

@dataclass
class ClassifierInput:
    user_input: str
    source: str = "manual"              # manual | wechat | lark | hotkey | api
    timestamp: str = ""
    persona: Optional[dict] = None
    user_model: Optional[dict] = None   # 新增：用户模型快照（固化层+活跃层）
    context_summary: Optional[dict] = None
    context_minutes: int = 5
    buffer_fragments: Optional[list] = None  # 新增：缓冲池聚合的多条碎片


def build_messages(inp: ClassifierInput) -> list[dict]:
    persona_block = build_persona_block(inp.persona or {})
    user_model_block = build_user_model_block(inp.user_model or {})
    system_content = SYSTEM_PROMPT.format(
        persona_block=persona_block,
        user_model_block=user_model_block,
    )

    if inp.context_summary:
        context_block = inp.context_summary.get("summary_text", "无环境数据")
    else:
        context_block = "无环境数据（ActivityWatch 未运行或无近期活动）"

    # 如果有多条缓冲碎片，合并展示
    if inp.buffer_fragments and len(inp.buffer_fragments) > 1:
        fragments_text = "\n".join([f"  [{i+1}] {f}" for i, f in enumerate(inp.buffer_fragments)])
        user_input_display = f"（以下为 {len(inp.buffer_fragments)} 条聚合碎片，请整体理解意图）\n{fragments_text}"
    else:
        user_input_display = inp.user_input

    ts = inp.timestamp or datetime.now(timezone.utc).isoformat()
    user_content = USER_MESSAGE_TEMPLATE.format(
        context_minutes=inp.context_minutes,
        context_block=context_block,
        source=inp.source,
        timestamp=ts,
        user_input=user_input_display,
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


# ══════════════════════════════════════════════════════════
# 规则引擎兜底（LLM 不可用时覆盖高频意图）
# ══════════════════════════════════════════════════════════

_RULE_PATTERNS = [
    # (正则, intent, priority_offset, title_template)
    (r"提醒|remind|记得|别忘了|到时候", "reminder", 2, "提醒: {input}"),
    (r"搜索|查一下|调研|找一下|了解|竞品", "research", 2, "调研: {input}"),
    (r"记录|备忘|笔记|灵感|想法|点子", "record", 0, "记录: {input}"),
    (r"发邮件|发消息|联系|通知|回复", "task", 2, "联系: {input}"),
    (r"买|订|预约|预订|purchase|book", "task", 2, "预约: {input}"),
    (r"写|做|完成|fix|修|改|优化", "task", 2, "任务: {input}"),
]

def rule_engine_classify(user_input: str) -> dict:
    """
    规则引擎兜底。LLM 超时/报错时调用，覆盖高频常见意图。
    保证系统不瘫痪，但精度远低于 LLM 主路径。
    """
    text_lower = user_input.lower()

    # 时间提取
    due_at = None
    time_match = re.search(r"(\d{1,2})[点:：](\d{0,2})", user_input)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        now = datetime.now()
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due < now:
            due = due.replace(day=due.day + 1)
        due_at = due.isoformat()

    # 意图匹配
    for pattern, intent, prio, title_tmpl in _RULE_PATTERNS:
        if re.search(pattern, user_input, re.IGNORECASE):
            title = title_tmpl.format(input=user_input[:25])
            return {
                "todos": [{
                    "title": title,
                    "body": user_input,
                    "intent": intent,
                    "priority": prio,
                    "confidence": 0.45,
                    "confidence_basis": "规则引擎兜底（LLM不可用）",
                    "tags": ["auto"],
                    "due_at": due_at,
                    "topology": {
                        "time_dim": due_at,
                        "location_dim": None,
                        "person_dim": None,
                        "cost_dim": None,
                        "priority_dim": None,
                        "status_dim": None,
                    },
                    "scene_dims": {"type": "通用", "fields": {}},
                    "info_gain_needed": intent == "record",
                    "agent_hint": {"type": intent, "query": user_input},
                    "regret_window_seconds": 300,
                }],
                "buffer_meta": {"fragment_count": 1, "aggregated": False},
                "_rule_engine": True,
            }

    # 默认 record
    return {
        "todos": [{
            "title": user_input[:30],
            "body": user_input,
            "intent": "record",
            "priority": 0,
            "confidence": 0.35,
            "confidence_basis": "规则引擎兜底默认分类",
            "tags": ["inbox"],
            "due_at": None,
            "topology": {k: None for k in ["time_dim","location_dim","person_dim","cost_dim","priority_dim","status_dim"]},
            "scene_dims": {"type": "通用", "fields": {}},
            "info_gain_needed": True,
            "agent_hint": {"type": "record", "query": user_input},
            "regret_window_seconds": 300,
        }],
        "buffer_meta": {"fragment_count": 1, "aggregated": False},
        "_rule_engine": True,
    }


# ══════════════════════════════════════════════════════════
# LLM 调用（多 provider）
# ══════════════════════════════════════════════════════════

def classify_intent(
    inp: ClassifierInput,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    api_key: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """
    意图分类主入口。
    主路径：LLM（准确，快）
    兜底：规则引擎（稳定，不依赖外部服务）
    """
    messages = build_messages(inp)
    try:
        if provider == "anthropic":
            result = _call_anthropic(messages, model, api_key, timeout)
        elif provider == "openai":
            result = _call_openai(messages, model, api_key, timeout)
        elif provider == "local":
            result = _call_local(messages, model, timeout)
        else:
            raise ValueError(f"Unknown provider: {provider}")

        # 后处理：补全缺失字段
        result = _post_process(result, inp)
        return result

    except Exception as e:
        import logging
        logging.getLogger("sath.intent").warning(
            f"LLM intent classification failed ({e}), falling back to rule engine"
        )
        return rule_engine_classify(inp.user_input)


def _call_anthropic(messages, model, api_key, timeout=30):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    system_msg = messages[0]["content"]
    user_msgs = [{"role": m["role"], "content": m["content"]} for m in messages[1:]]
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_msg,
        messages=user_msgs,
    )
    return _parse_response(response.content[0].text)


def _call_openai(messages, model, api_key, timeout=30):
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.3,
        response_format={"type": "json_object"},
        timeout=timeout,
    )
    return _parse_response(response.choices[0].message.content)


def _call_local(messages, model, timeout=60):
    import httpx
    response = httpx.post(
        "http://localhost:11434/api/chat",
        json={"model": model, "messages": messages, "stream": False, "format": "json"},
        timeout=timeout,
    )
    return _parse_response(response.json()["message"]["content"])


def _parse_response(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```json\s*(.*?)\s*```", raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
    start = raw_text.find("{")
    end = raw_text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(raw_text[start:end])
        except Exception:
            pass
    return {"todos": [], "error": "Failed to parse LLM response", "raw": raw_text[:500]}


def _post_process(result: dict, inp: ClassifierInput) -> dict:
    """补全缺失字段，确保下游处理不出错"""
    todos = result.get("todos", [])
    for t in todos:
        # 确保 topology 字段存在
        if "topology" not in t:
            t["topology"] = {k: None for k in
                             ["time_dim","location_dim","person_dim","cost_dim","priority_dim","status_dim"]}
        # 确保 scene_dims 字段存在
        if "scene_dims" not in t:
            t["scene_dims"] = {"type": "通用", "fields": {}}
        # 确保 info_gain_needed 字段存在
        if "info_gain_needed" not in t:
            t["info_gain_needed"] = t.get("intent") == "record" and not t.get("agent_hint", {}).get("query")
        # 确保 regret_window_seconds
        if "regret_window_seconds" not in t:
            t["regret_window_seconds"] = 300
        # 确保 confidence_basis
        if "confidence_basis" not in t:
            t["confidence_basis"] = ""
    if "buffer_meta" not in result:
        result["buffer_meta"] = {
            "fragment_count": len(inp.buffer_fragments or [inp.user_input]),
            "aggregated": bool(inp.buffer_fragments and len(inp.buffer_fragments) > 1),
        }
    return result


# ══════════════════════════════════════════════════════════
# 示例场景（对照升级效果）
# ══════════════════════════════════════════════════════════

EXAMPLE_SCENARIOS = [
    {
        "name": "极简输入 + 蒸馏层推断（升级核心场景）",
        "input": "陈总明天10点",
        "source": "wechat",
        "context": {
            "summary_text": "[触发上下文]\n当前窗口: 高榕资本官网\n剪贴板: 陈总名片"
        },
        "user_model": {
            "fixed_patterns": [
                {"key": "见投资人准备", "value": "整理BP+确认财务数字+联系律师"},
                {"key": "陈总", "value": "高榕资本，关注数据，喜欢看LTV/CAC"},
            ],
            "active_preferences": {"meeting_buffer_min": "30", "preferred_venue": "公司会议室"},
        },
        "persona": {"role": "创业公司CEO", "domains": ["融资", "产品"], "tools": ["飞书", "notion"]},
    },
    {
        "name": "灵感记录 + 主动信息增益触发",
        "input": "工具传播这个思路值得研究",
        "source": "hotkey",
        "context": {"summary_text": "[触发上下文]\n当前浏览器: Twitter/X 某独立开发者账号"},
        "user_model": {
            "fixed_patterns": [{"key": "身份", "value": "独立开发者，主做工具类产品"}],
        },
        "persona": {"role": "独立开发者", "domains": ["工具", "增长"], "tools": ["Notion"]},
    },
    {
        "name": "缓冲池聚合多条碎片",
        "input": "见面",
        "source": "wechat",
        "buffer_fragments": ["谈", "付款方式", "免租期", "分期付款", "押一付三"],
        "context": {"summary_text": "[触发上下文]\n当前App: WeChat 房屋中介群"},
        "user_model": {"fixed_patterns": [{"key": "住所", "value": "上海徐汇区"}]},
        "persona": {"role": "产品经理", "domains": ["租房", "合同"], "tools": ["微信"]},
    },
]


def run_examples(provider: str = "anthropic", model: str = "claude-sonnet-4-6"):
    for i, s in enumerate(EXAMPLE_SCENARIOS, 1):
        print(f"\n{'='*60}")
        print(f"场景 {i}: {s['name']}")
        print(f"输入: {s['input']}")
        print(f"{'='*60}")
        inp = ClassifierInput(
            user_input=s["input"],
            source=s.get("source", "manual"),
            context_summary=s.get("context"),
            persona=s.get("persona"),
            user_model=s.get("user_model"),
            buffer_fragments=s.get("buffer_fragments"),
        )
        try:
            result = classify_intent(inp, provider=provider, model=model)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"[跳过] {e}")


if __name__ == "__main__":
    import sys
    provider = sys.argv[1] if len(sys.argv) > 1 else "anthropic"
    model = sys.argv[2] if len(sys.argv) > 2 else "claude-sonnet-4-6"
    run_examples(provider, model)
