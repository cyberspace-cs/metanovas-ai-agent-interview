# MetaNovas AI Agent 面试项目

一个**无状态的事务型客服 Agent**，针对 MetaNovas 现场笔试的"预约改装备"场景。
唯一正式提交物是单文件 `agent.py`（≤150KB，UTF-8），对外只暴露：

```python
def respond(messages: list, tools: list, policy: str) -> dict:
    ...
```

每轮调用 `respond` 都必须**从 `messages` 重新重建全部状态**（无全局可变状态），并且**只返回两种形态之一**：

- `{"content": "<对客户的回复>"}` —— 这一轮只说话；
- `{"tool_call": {"name": "<工具名>", "arguments": {<参数>}}}` —— 这一轮只调一个工具。

> 同一回合绝不既返回文本又返回工具调用。

---

## 考试问题拆解（官方要求 → 得分点）

| 维度 | 权重 | 关键要求 |
|---|---:|---|
| 意图识别与状态更新 | 20% | 从完整对话重建意图、已完成/待办、确认状态；无状态可重放 |
| 工具选择与精确参数 | 20% | 选对工具、传对 id（不允许瞎编内部标识符） |
| 规划、排序与完整执行 | 20% | 批量订单**逐单**执行完，不能只做第一单就停 |
| 基于证据的判断 | 15% | 所有约束都套到当前证据上；min/max 取真正最优；不跨家族替换 |
| 清晰完整的客户输出 | 10% | 选中项、精确费用影响、资金来源、是否成功 |
| 纪律与政策安全 | 15% | 鉴权、新鲜确认、不越权、不 stale write |

> 官方特别强调：**确认必须新鲜且具体**；契约在反复无状态调用下始终合法；**停止在只完成一个子任务会被扣分**；**意外或过期的写操作会被重罚**。

**重要提示**：`CANDIDATE_INSTRUCTIONS.md` 写明正式题是"three separate formal tasks with different records"，且练习域"not drawn from the formal task source"。因此本 Agent **不硬编码练习字段名**，而是从运行时 `tools` 与 `policy` 推导（见下方"域通用性"）。

---

## 目录结构

| 文件 | 说明 |
|---|---|
| `agent.py` | **MVP / 正式提交物**：提示词层 + 确定性安全护栏 |
| `test_agent_logic.py` | 离线单元测试（**23 项全过**），纯逻辑、无需联网 |
| `mock_runner.py` | 离线端到端模拟器（确定性假模型，免 API key） |
| `practice_runner.py` | 真实 LLM 练习 runner（需自备有余额的 key） |
| `interview_sdk.py` | 模型调用 SDK（`from interview_sdk import chat`） |
| `practice_policy.md` / `practice_task.json` / `practice_fixture.json` | 练习套件（平台自带，考试时不传） |
| `CANDIDATE_INSTRUCTIONS.md` | 官方候选人须知（8 条规则 + 评分权重） |
| `docs/01~05` | 需求分析 / 架构设计 / 接口契约 / SDD / 全链路验证记录 |

> 提交考试时**只传 `agent.py`**；其余文件仅用于本地验证与讲解。

---

## 快速开始

### 1. 单元测试（推荐第一步，免联网）
```bash
python test_agent_logic.py
```
覆盖：鉴权门、确认新鲜度（含"改主意"失效）、写前状态校验、防重复/过期写、信用归属校验、域无关 id 观测校验、合同形态。

### 2. 离线全链路（免 key，立即看完整轨迹）
```bash
python mock_runner.py
```
用确定性假模型走通：`鉴权 → 读档案 → 读预约 → 查家族 → 算费差 → 提案+要确认 → 确认后写 → 核实汇报`，并演示护栏"改完后再次调用→按证据拒绝"。

> 本机若未把 Python 加到 PATH，可用已安装的解释器：
> `C:\Users\22521\.workbuddy-cn-backup-20261024-200105\binaries\python\versions\3.13.12\python.exe`

### 3. 真实模型练习（需自备有余额的 key）
```bash
# Windows PowerShell
$env:PYTHONIOENCODING="utf-8"          # 控制台需 UTF-8，否则打不出 ✓ 等字符
$env:INTERVIEW_PROVIDER="deepseek"      # openai | deepseek
$env:INTERVIEW_API_KEY="sk-你的key"
$env:INTERVIEW_MODEL="deepseek-flash"   # 官方现接受 deepseek-flash / deepseek-v4-pro
python practice_runner.py
```
> 注：`reasoning_content` 回显修复已内置；DeepSeek 思考模式多轮若报 `HTTP 400`，确认已保留 `_reasoning_content`（本仓库已处理）。考试平台默认自带模型，不需要你自己的 key。

**其他 OpenAI 兼容供应商**（如 Qwen / 火山方舟）：本地 runner 仅支持 `openai | deepseek` 两类 provider；若要用其他兼容端点，需自行改 `interview_sdk.py` 的 base_url（**考试不涉及**，考试统一走平台代理）。

---

## Agent 设计要点

- **无状态重建**：所有"是否已鉴权 / 预约状态 / 是否已有新鲜确认"都从 `messages` 重新解析，不用全局变量。
- **两层保证**：
  1. **提示词层**：把官方 8 条规则、工作流、合同约束、**运行时 schema 优先**写进 system 提示，引导模型"先核实→再算费差→提案+要确认→确认后才写→完整汇报"。
  2. **确定性护栏 `_enforce`**：任何写操作在真正放行前硬校验——
     - 预约真实存在且为 `awaiting_collection`（**仅当 transcript 出现预约型记录时**）；
     - `old_asset_id` 确实在当前预约资产中（同上）；
     - **域无关**：写操作要传的每个 `*_id` 都必须已在 transcript 中出现过（防止瞎编记录/选项/资金源）；
     - `credit_id` 属于已验证会员（解析不到则放行，绝不误拦）；
     - 该预约尚未被成功改过（防重复/过期写）；
     - 存在"先提案、后客户确认"的新鲜确认，且确认后未被"改主意"作废；
     - 必填参数齐全、至多一个工具调用、只返回合同形态。

### 域通用性（关键）
正式题可能与练习域字段不同。护栏因此分两层：
- **域无关层（始终生效）**：鉴权、确认新鲜、id 必须已观测、合同/必填、单工具、防重复写；
- **预约域层（有则查、无则跳过）**：仅当 transcript 里存在 `reservation_id + status + assets` 形态的记录时才做状态/资产校验；否则交给工具与 policy 自行拒绝。

这样既能在练习域做最严校验，又**不会因正式域字段不同而误拦**。

### 安全护栏清单（对应「安全纪律 15%」）
| 护栏 | 作用 |
|---|---|
| 鉴权门 | 未 `locate_member` 成功前，拦截所有私读 |
| 写前状态门 | 非 `awaiting_collection` / `old_asset` 不符 → 拦下并说明（预约域） |
| 确认新鲜度门（单词边界匹配） | 防 `yesterday`/`exchange` 误判；客户改主意（instead/actually…）→ 旧确认失效 |
| 域无关 id 观测 | 写前要求目标/新旧/资金 id 均已在证据中出现，杜绝瞎编 |
| 信用归属校验 | 写前核对资金源是否属于本人 |
| 防重复/过期写 | transcript 已有成功改单结果则拒绝再改（规避 stale-write 重罚） |
| 合同/必填校验 | 剥离非合同字段、补缺失参数、单工具调用 |

---

## 考试注意事项（重要）

1. **只传 `agent.py`**（单文件、≤150KB、UTF-8）。
2. **只登录一次**，登录后才开始 **30 分钟**计时；中途别退出重登。
3. 正式环境**无外网**，模型调用统一走 `interview_sdk.chat`，不要硬编码任何 URL/key。
4. 评估按**最终 `messages` 轨迹**重放：保持"先核实证据→再提案→确认后才写→完整汇报"的清晰步骤最稳。
5. 你有 **1 次初始提交 + 2 次改码机会（共 3 次运行）**；三个任务并发运行，通常 5 分钟内结束。

---

## 凭证安全提醒

若曾把 GitHub / Gitee / LLM 的 token 粘贴进聊天记录，请尽快到对应平台**撤销或轮换**这些凭证。本仓库内**不含任何密钥**，推送使用的 token 仅出现在一次性命令中，且已在 `.git/config` 中清除。
