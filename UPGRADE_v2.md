# Sath-ui 1.0 → 2.0 升级说明

> 升级日期：2026-04-16  
> 升级方向：从"能用的待办助手"升级为"主动执行的秘书 Agent 系统"

---

## 一、核心理念变化

| 维度 | v1.0 | v2.0 |
|------|------|------|
| 交互模型 | 问答式对话 | 记录→执行（不打断用户） |
| 信息聚合 | 每条输入独立处理 | 20s 缓冲池聚合碎片意图 |
| 信息缺口 | 询问用户 | 从用户模型推断，先执行后告知 |
| Agent 输出 | 直接回复用户 | 全部经编排 Agent 统一推送 |
| 学习方式 | 静态偏好 | 实时热蒸馏 + 每日冷蒸馏 |

---

## 二、改动文件清单

### 2.1 新建文件

**`sath-source/brain/distillation.py`**  
完整的蒸馏系统。包含：
- `hot_distill()` — 每次交互后实时运行，记录行为日志、更新关系网络、候选固化模式
- `process_feedback()` — 8 状态反馈矩阵（有效/无效 × 准确/不准确 = 4 组合 + regret），更新置信权重
- `cold_distill()` — 每日凌晨 3 点批处理：汇总意图频率、升降档固化模式、可选 LLM 深度蒸馏
- `schedule_cold_distill()` — 守护线程，自动在指定小时触发冷蒸馏

**`sath-source/brain/orchestrator.py`**  
编排 Agent（系统唯一对用户界面）。包含：
- `OrchestratorAgent` — 接收分类结果，按权限层路由（执行/建议/告知）
- `_fill_missing_info()` — 拓扑缺口自动从用户模型补全，不询问用户
- `_trigger_info_gain()` — 背景信息增益任务（灵感/策略类意图自动触发网络搜索）
- `handle_regret()` — 后悔药：在 regret_deadline 内撤销 pending_actions
- `agent_turn_guard()` — 25 轮保护：第 24 轮提示收尾，第 25 轮强制停止
- `_heartbeat_loop()` — 60s 心跳：扫描即将到期的提醒、info_gain 结果就绪

---

### 2.2 重写文件

**`sath-source/brain/pipeline.py`** (v2)  
新增核心类：
- `BufferPool` — 20s 碎片聚合器，threading.Timer 驱动，到期后触发回调
- `PermissionTier` — 三档权限（告知/建议/执行）动态绑定
- `SkillsLibrary` — 技能库语义匹配（MVP 关键词版，预留 ChromaDB 向量替换接口）
- `UserModel` — 用户模型快照（固化层/活跃层/归档层），节律参数读取
- `SATHBrain` (v2) — 新增 `push()` 缓冲池模式，保留 `ingest()` 兼容旧调用

**`sath-source/prompts/intent_classifier.py`** (v2)  
新增能力：
- `build_user_model_block()` — 用户模型注入提示词（固化模式 + 活跃偏好 + 节律参数）
- `ClassifierInput` — 扩展 `user_model`、`buffer_fragments` 字段
- `rule_engine_classify()` — LLM 不可用时的规则引擎兜底（置信度 0.35–0.45）
- 输出格式扩展：`topology`（6 维拓扑）、`scene_dims`、`info_gain_needed`、`confidence_basis`、`regret_window_seconds`
- 系统提示词升级：加入拓扑分解指令、置信度原则（任何置信度都执行，不询问）

---

### 2.3 追加修改

**`bridge.py`** (v2 扩展，原有接口零改动)  
新增全局组件：
- `_buffer_pool` / `_brain` / `_orchestrator` — v2 单例
- `_on_buffer_flush_bridge()` — 缓冲池到期回调
- `init_v2_components()` — 启动时初始化三组件（BufferPool + OrchestratorAgent + 冷蒸馏调度）

新增 API 端点：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/webhook/wechat` | POST | 微信 ClawBot 官方 webhook（兼容旧 iLink，并存） |
| `/api/buffer_push` | POST | 推入缓冲池（或 flush_now 直接处理） |
| `/api/pending_results` | GET | 拉取缓冲池异步结果 |
| `/api/feedback_matrix` | POST | 8 状态反馈（effective + accurate + regret） |
| `/api/regret` | POST | 后悔药：todo_id 在窗口内撤销 |
| `/api/user_model` | GET | 读取用户模型快照 |
| `/api/distill_hot` | POST | 手动触发热蒸馏 |

**`agents/*/*/SOUL.md`** (196 个文件)  
全部追加 `## Orchestration Protocol (v2)` 节，规定：
- 所有子 Agent 以 JSON 格式向编排 Agent 汇报，禁止直接向用户输出
- 遇到信息缺口不询问用户，标注 confidence 后继续执行
- 禁止自行发送微信/邮件/日历邀请，须经编排 Agent 权限层审核

---

## 三、关于微信接口

原有微信功能（iLink Bot）**未做任何改动，仍然正常**。

新增的 `/webhook/wechat` 是为对接微信官方 **ClawBot**（2026年3月开放灰测）的额外入口，字段映射如下：

```
ClawBot webhook → from_user_wxid, msg_item_list[].text_item.text
iLink（原有）   → user_id, messages[].content
```

两个 webhook 可并存，互不干扰。如需启用 ClawBot，只需在微信开放平台将 webhook 地址指向 `/webhook/wechat` 即可。

---

## 四、数据库新增表

以下表在 v2 组件首次运行时自动创建（如不存在）：

| 表名 | 用途 |
|------|------|
| `behavior_log` | 每次交互的行为记录（意图类型、来源、置信度、反馈状态） |
| `distill_events` | 蒸馏事件日志（冷/热、触发原因） |
| `error_cases` | 错误案例（confidence 低 + 无效反馈触发归档） |
| `skill_candidates` | 技能库候选（高频成功路径自动提炼） |
| `info_gain_tasks` | 主动信息增益任务队列（后台搜索） |
| `pending_actions` | 待执行动作（用于后悔药窗口管理） |
| `relation_network` | 关系网络（人物提及频次和亲密度） |

---

## 五、向后兼容

- `SATHBrain.ingest()` 接口保留，现有调用不受影响
- 原有 `/api/ingest`、`/api/todos`、`/api/settings` 等端点零改动
- iLink 微信 webhook 路径零改动
- 所有新端点均以独立 `elif` 分支添加，不影响现有路由

---

## 六、启动流程（v2）

```
python bridge.py
  ↓
打印 DB / LLM / Search 配置
  ↓
启动 auto_distill_loop（24h 检查）
  ↓
启动 focus_collector_loop（30s 采集焦点）
  ↓
init_v2_components()
  ├─ BufferPool 初始化（从用户节律参数读取 buffer_seconds）
  ├─ OrchestratorAgent 启动（含心跳线程）
  └─ 冷蒸馏调度器启动（每日 03:00）
  ↓
HTTP 服务器开始监听 127.0.0.1:PORT
```
