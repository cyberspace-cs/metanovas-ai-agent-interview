# 04 软件设计文档（SDD）

## 1. 模块与函数清单（agent.py）

| 函数 | 职责 | 输入 | 输出 |
|---|---|---|---|
| `respond` | 入口：拼 system → 调 chat → 过护栏 | `messages, tools, policy` | `{"content"}` 或 `{"tool_call"}` |
| `_build_system_prompt` | 注入政策+8规则+工作流+合同 | `policy` | system 消息 dict |
| `_tool_required` | 解析各工具必填参数 | `tools` | `{工具名: {必填字段}}` |
| `_is_write` / `_is_private_read` | 按名称启发式判定写/私读 | `name` | bool |
| `_is_authenticated` | 从 transcript 判是否已鉴权 | `messages` | bool |
| `_confirmation_present` | 判"新鲜确认"（提案后才有客户确认） | `messages` | bool |
| `_find_reservation` | 取某预约的最新已读结果 | `messages, reservation_id` | dict / None |
| `_id_observed` | 判某 id 是否已在 transcript 中出现（域无关） | `messages, value` | bool |
| `_enforce` | 确定性护栏 + 合同校验 | `result, messages, tools` | 合规的返回 dict |

## 2. 护栏算法详述

### 2.1 鉴权门 `_is_authenticated`
扫描 `role=="tool"` 的结果，解析 JSON；若得到字符串且以 `MB-` 开头（或含 `member_id`），
视为 `locate_member` 成功 → 已鉴权。否则私读类工具被拦截，改为请客户提供全名+6位码。

### 2.2 写前状态门（在 `_enforce` 内联）
对写操作依次校验，任一不满足即改为拒绝文本：
1. **域无关（始终生效）**：写操作要传的每个 `*_id` 都必须已在 transcript（工具结果或客户消息）中出现过——防止瞎编目标/新旧值/资金源；若目标预约未读，直接要求先读。
2. **预约域（仅当 transcript 出现 `reservation_id + status + assets` 形态记录时才查）**：
   - `status == "awaiting_collection"`（否则用已验证事实拒绝）；
   - `old_asset_id` 确实在当前预约 `assets` 中（防错改/跨资产）。
3. 其他域字段不同则跳过步骤 2，交由写工具与 policy 自行拒绝，避免误拦。

### 2.3 确认新鲜度门 `_confirmation_present`
- 匹配使用**单词边界正则**：助理消息含提案标记（`propose/change/replace/swap/cancel/...`）的最新下标 `P`；用户消息含确认标记（`yes/confirm/proceed/approve/...`）的最新下标 `C`。
- 单词边界避免误判：`yesterday` 不会算作 `yes` 确认，`exchange` 不会算作 `change` 提案。
- 当且仅当 `C > P` 时返回 True。
- **效果**：客户改主意→新提案使 `P` 后移→旧确认失效，必须重新要确认（规则7）。
- 批量：一次计划提案 + 一次确认，`C>P` 在后续多单执行期间持续成立（执行状态语不含提案标记）。

### 2.4 合同与必填校验
- 至多一个 `tool_call`（模型本身返回单调用；护栏只取其一）。
- 必填参数缺失 → 改为询问缺失项。
- 最终只返回 `{"content"}` 或 `{"tool_call"}`，剥离任何非合同字段（如 `_reasoning_content`）。

### 2.5 加固护栏（新增）
- **写操作识别更稳**：`_is_write` 在精确名 `change_reserved_asset` 之外，按前缀
  （`change/update/cancel/create/delete/modify/remove/add/set/replace/swap/move/
  transfer/assign/edit/alter/revise/adjust`）兜底识别，避免正式题若重命名写工具时漏掉确认门。
- **信用额度归属校验**：写前检查 `credit_id` 是否属于已验证会员
  （`read_member_profile` 返回的 `credits`）；不属于则拦下请确认。解析不到则放行
  （fail-open，绝不误拦）。
- **防重复/过期写**：若 transcript 中已存在该预约的成功改单结果（状态不再是
  `awaiting_collection`），直接拒绝再改，规避"stale/duplicate write"重罚。
- **规则7 改主意失效**：确认后若客户又说 `instead/actually/rather/never mind/...`
  （且该句本身不是确认），旧确认立即失效，必须重新提案并重新确认。
- **域无关 id 观测**：见 §2.2；跨域安全的核心，不依赖具体字段名。

## 3. 边界情况处理

| 情况 | 处理 |
|---|---|
| 未鉴权就私读 | 拦下，请验证身份 |
| 预约非 awaiting_collection | 拦下，用事实拒绝 |
| old_asset 与当前不符 | 拦下，说明只可替换真实预留资产 |
| 写操作但无新鲜确认 | 拦下，请明确确认 |
| 必填参数缺失 | 拦下，问缺失项 |
| 模型返回空内容 | 兜底为澄清问句 |
| 客户改主意 | 新提案使旧确认失效，重算 |
| 确认后客户又改主意 | 旧确认失效，重新提案+确认（规则7） |
| 正式域字段与练习不同 | 跳过预约专属校验，仅做域无关校验 |
| 信用额度不属于本人 | 拦下，请确认自身信用 |
| 预约已改后再写 | 拦下，说明已改不可再改（防过期/重复写） |
| 批量 | 列计划→一次确认→逐单写→汇总 |

## 4. 可测试性设计
- 全部状态解析与护栏为**纯函数**（仅依赖入参 `messages/tools`），可单测。
- 模型调用收敛到 `chat` 一个入口，可注入 mock 做离线 E2E（`mock_runner.py`）。
- 已落地单测 `test_agent_logic.py`（23 项全过），覆盖上述边界与新增护栏。

## 5. 风险与缓解
- **确认词匹配**（平台确认话术不含 my 标记词）→ 写被拦。缓解：标记词集合较宽 + 单词边界匹配；若实测卡在"要确认却未改"，放宽 `_confirmation_present` 的确认词。
- **模型不提案直接写** → 护栏拦截，改为请确认（安全优先）。
- **跨家族/不可用选项** → 提示词 + 工具执行器双重拒绝。
- **信用/重复写** → 已加护栏：写前校验信用归属、已改预约拒绝再写（fail-open 解析不到时放行，绝不误拦正常流程）。

## 6. 文件清单（交付）
- `agent.py` —— **MVP / 正式提交物**
- `test_agent_logic.py` —— 单元 TDD（护栏逻辑）
- `mock_runner.py` —— 离线 E2E（假模型，免费验证全链路）
- `docs/01~04` —— 需求/架构/接口/SDD
- 不提交：`interview_sdk.py`、`practice_*.py`、`practice_*.json`（平台自带）
