# Assistant-Agent

> 外壳是 Todo 工具，内核是完整秘书系统。  
> 九层架构，只有两层调用 LLM，其余全是规则引擎——成本可控，越用越懂你。

---

## 它是什么

Assistant-Agent 是一个运行在本地的个人 AI 助理。你在微信发一条消息，或者在桌面随手一记，它会：

1. **理解你的意图**（不只是解析语言，还注入了你的个人习惯）
2. **自动执行**（搜索、记录、生成文档、管理任务……）
3. **学习反馈**（你的每次点评都蒸馏进用户画像，越用越准）

同一句"查一下"，对不同人产生不同深度的响应。

---

## 九层架构

```
输入层 → 缓冲层 → 意图层 → 用户模型层 → 技能库
                                ↓
                        编排层 → 执行层
                                ↓
                    复盘层 → 蒸馏层 → 用户模型更新
```

| 层 | 职责 | 是否调用 LLM |
|----|------|:---:|
| L1 输入层 | 聚合微信/桌面输入，20s 防抖 | ✗ |
| L2 缓冲层 | 去重、排队 | ✗ |
| L3 意图层 | 意图识别 + 个人习惯注入 | ✅ |
| L4 用户模型 | 存储个人画像、偏好、节律 | ✗ |
| L5 技能库 | 匹配历史模板，复用已有方案 | ✗ |
| L6 编排层 | 拆分 Pipeline，并行调度 | ✗ |
| L7 执行层 | 搜索/读写文件/调用工具 | ✗ |
| L8 复盘层 | 收集用户反馈 | ✗ |
| L9 蒸馏层 | 更新用户画像 | ✅ |

**只有 L3 和 L9 调用 LLM**，其余全是规则引擎，推理成本极低。

---

## 快速开始

### 依赖

- macOS 12+
- Python 3.10+
- 一个 OpenAI / 兼容接口的 API Key

### 安装

```bash
git clone https://github.com/caiaiacai/Assistant-Agent.git
cd Assistant-Agent

# 安装 Python 依赖
pip install -r requirements.txt

# 启动本地服务
python bridge.py
```

打开 `index.html` 即可使用桌面 UI。

### 配置 API Key

启动后在 UI 设置页填入：
- LLM API Key（OpenAI / 兼容接口）
- Search API Key（可选，Tavily / Brave）

所有配置存储在本地 SQLite，**不上传任何数据**。

---

## 核心特性

- **微信直接输入**：接入个人微信，消息即指令
- **本地优先**：所有数据存 SQLite，离线可用
- **技能系统**：可扩展的 Agent 技能库（搜索、Notion、Obsidian……）
- **用户模型**：持续更新个人画像，理解你的工作节律
- **成本可控**：平均每条任务 LLM 调用 < 2 次

---

## 项目结构

```
├── bridge.py          # 核心服务（HTTP Bridge + LLM + DB）
├── index.html         # 桌面 UI
├── sath-source/       # 模块化源码
│   ├── brain/         # 意图识别、编排、蒸馏
│   ├── executor/      # 执行层工具
│   ├── sensor/        # 输入感知
│   ├── prompts/       # Prompt 模板
│   └── schema/        # 数据库 Schema
├── agents/            # 可扩展 Agent 技能包
├── skills/            # 技能定义
└── sath-server/       # Rust HTTP 服务（可选加速）
```

---

## License

MIT © 2025 caiaiacai
