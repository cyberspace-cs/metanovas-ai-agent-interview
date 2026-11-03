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

## 目录结构

| 文件 | 说明 |
|---|---|
| `agent.py` | **MVP / 正式提交物**：提示词层 + 确定性安全护栏 |
| `test_agent_logic.py` | 离线单元测试（**19 项全过**），纯逻辑、无需联网 |
| `mock_runner.py` | 离线端到端模拟器（确定性假模型，免 API key） |
| `practice_runner.py` | 真实 LLM 练习runner（需自备有余额的 key） |
| `interview_sdk.py` | 模型调用 SDK（`from interview_sdk import chat`） |
| `practice_policy.md` / `practice_task.json` / `practice_fixture.json` | 练习套件（平台自带，考试时不传） |
| `CANDIDATE_INSTRUCTIONS.md` | 官方候选人须知（含 8 条规则与评分权重） |
| `docs/01~05` | 需求分析 / 架构设计 / 接口契约 / SDD / 全链路验证记录 |

> 提交考试时**只传 `agent.py`**；其余文件仅用于本地验证与讲解。

---

## 快速开始

### 1. 单元测试（推荐第一步，免联网）
```powershell
& "C:\Users\22521\.workbuddy-cn-backup-20261024-200105\binaries\python\versions\3.13.12\python.exe" test_agent_logic.py
```
覆盖：鉴权门、确认新鲜度、写前状态校验、防重复/过期写、信用归属校验、合同形态。

### 2. 离线全链路（免 key，立即看完整轨迹）
```powershell
& "C:\Users\22521\.workbuddy-cn-backup-20261024-200105\binaries\python\versions\3.13.12\python.exe" mock_runner.py
```
用确定性假模型走通：`鉴权 → 读档案 → 读预约 → 查家族 → 算费差 → 提案+要确认 → 确认后写 → 核实汇报`，并演示护栏"改完后再次调用→按证据拒绝"。

### 3. 真实模型练习（需自备有余额的 key）
```powershell
$env:PYTHONIOENCODING="utf-8"                       # Windows 控制台需 UTF-8，否则打不出 ✓ 等字符
$env:INTERVIEW_PROVIDER="deepseek"
$env:INTERVIEW_API_KEY="sk-你的key"
$env:INTERVIEW_MODEL="deepseek-flash"
& "C:\Users\22521\.workbuddy-cn-backup-20261024-200105\binaries\python\versions\3.13.12\python.exe" practice_runner.py
```
> 注：`reasoning_content` 回显修复已内置在代码中；若使用 DeepSeek 思考模式多轮回 `HTTP 400`，确认已保留 `_reasoning_content`（本仓库已处理）。考试平台默认用自带模型，不暴露推理字段，无需此修复。

---

## Agent 设计要点

- **无状态重建**：所有"是否已鉴权 / 预约状态 / 是否已有新鲜确认"都从 `messages` 里重新解析，不用全局变量。
- **两层保证**：
  1. **提示词层**：把官方 8 条规则、工作流（a~e）、合同约束写进 system 提示，引导模型"先核实→再算费差→提案+要确认→确认后才写→汇报"。
  2. **确定性护栏 `_enforce`**：任何写操作在真正放行前，硬校验——
     - 预约真实存在且为 `awaiting_collection`；
     - `old_asset_id` 确实在当前预约资产中；
     - `credit_id` 属于已验证会员（解析不到则放行，绝不误拦）；
     - 该预约尚未被成功改过（防重复/过期写）；
     - 存在"先提案、后客户确认"的新鲜确认；
     - 必填参数齐全、至多一个工具调用、只返回合同形态。

### 安全护栏清单（对应官方评分「安全纪律 15%」）
| 护栏 | 作用 |
|---|---|
| 鉴权门 | 未 `locate_member` 成功前，拦截所有私读 |
| 写前状态门 | 非 `awaiting_collection` / `old_asset` 不符 → 拦下并说明 |
| 确认新鲜度门（单词边界匹配） | 防止"yesterday"误判为"yes"、防止"exchange"误判为"change"；客户改主意→旧确认失效 |
| 信用归属校验 | 写前核对资金源是否属于本人 |
| 防重复/过期写 | transcript 已有成功改单结果则拒绝再改（规避 stale-write 重罚） |
| 合同/必填校验 | 剥离非合同字段、补缺失参数、单工具调用 |

---

## 考试注意事项（重要）

1. **只传 `agent.py`**（单文件、≤150KB、UTF-8）。
2. **只登录一次**，登录后才开始 30 分钟计时；中途别退出重登。
3. 模型调用统一走 `interview_sdk.chat`，正式环境无外网，不要硬编码任何 URL/key。
4. 评估是按**最终 `messages` 轨迹**重放的：保持"先核实证据→再提案→确认后才写→完整汇报"的清晰步骤最稳。

---

## 凭证安全提醒

若曾把 GitHub / Gitee / LLM 的 token 粘贴进聊天记录，请尽快到对应平台**撤销或轮换**这些凭证。本仓库内**不含任何密钥**，推送使用的 token 仅出现在一次性命令中，且已在 `.git/config` 中清除。
