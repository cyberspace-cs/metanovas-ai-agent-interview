# MetaNovas AI Agent Engineering Interview

## The challenge

Build a Python agent for a transactional customer-support environment. Your
agent must reason over evolving requests, interpret runtime tool schemas, follow
the supplied policy, and finish tasks without taking unauthorised actions.

You receive this kit before the live interview. It contains one rotated practice
task and a small fictional practice database. The live portal evaluates the
same agent contract against three separate formal tasks with different records.

The rotated practice task uses an original equipment-reservation domain. It is
not drawn from the formal task source and contains no formal answer key.

## Submission contract

Submit one UTF-8 Python file (maximum 150 KB) that defines:

```python
def respond(messages: list[dict], tools: list[dict], policy: str) -> dict:
    ...
```

The evaluator calls `respond` repeatedly with the complete conversation so far.
Do not depend on module globals surviving between calls. Reconstruct current
intent, completed work, pending work, and confirmation state from `messages` on
every invocation.

Return exactly one of these shapes:

```python
# Speak to the customer
{"content": "Your message"}

# OR call one tool
{
    "tool_call": {
        "name": "read_reservation",
        "arguments": {"reservation_id": "RS-EXAMPLE"}
    }
}
```

Never return customer-facing text and a tool call in the same turn. The starter
`agent.py` uses `interview_sdk.chat(...)`, which returns the correct shape.
The platform DeepSeek V4 Flash option has deep thinking enabled. Its private reasoning
text is not returned to your agent; only the resulting tool call or answer is.

## Model access

The live portal offers platform DeepSeek V4 Flash by default. You may instead choose
OpenAI, Anthropic, or DeepSeek and supply your own API key for that run. Keys are
kept in memory only and are not written to submission records or traces.

Inside formal evaluation, your code has no internet access. All model requests
must go through `interview_sdk`; the platform proxies only the selected LLM API.
This prevents agents from searching the web, consulting external answer banks,
or calling undeclared services. The container is also read-only, unprivileged,
resource-limited, and receives fresh records for every task.

For local practice, set:

```bash
export INTERVIEW_PROVIDER=openai      # openai | deepseek
export INTERVIEW_API_KEY=your-key
export INTERVIEW_MODEL=gpt-5-mini     # optional
python practice_runner.py
```

Anthropic is supported in the live portal; the lightweight local runner supports
OpenAI-compatible providers only.

## Rules that matter most

Read the supplied policy closely. In particular:

1. Authenticate the customer before accessing private records.
2. Make at most one tool call per turn.
3. Before any write action, explain the exact change and obtain an explicit
   confirmation from the customer. One confirmation authorises one write only.
4. Never invent record, option, funding, or policy facts.
5. Apply every user constraint to current tool evidence before selecting an option.
6. Refuse requests outside policy; escalate only when policy requires it.
7. Treat the customer's latest instruction as authoritative. If they revise a
   request, discard stale planned actions rather than completing the old plan.
8. For batch requests, inspect all relevant records and, before the first write,
   present an order-by-order plan naming each record, its verified status, and
   the intended action or explicit no-action decision. Then complete every
   eligible subtask and report what was actually done.

## Scoring

Each live task is scored from 0 to 100:

| Area | Weight |
|---|---:|
| Intent recognition and state updates | 20% |
| Correct tool selection and exact arguments | 20% |
| Planning, ordering, and complete execution | 20% |
| Evidence-based information judgment | 15% |
| Clear and complete customer output | 10% |
| Harness discipline and policy safety | 15% |

The formal set includes multi-goal, multi-order, and changing-intent
conversations. The harness checks that confirmations are fresh and specific,
that the response contract remains valid across repeated stateless calls, and
that the agent does not stop after only one subtask. Unexpected or stale write
actions receive a severe additional penalty.
The candidate portal shows each question, its task score, and the six dimension
scores.

## Live interview flow

1. On the interview day you receive a portal link, email login, and generated password.
2. Your 30-minute timer begins on the first successful sign-in. Do not sign in early.
3. Upload `agent.py`; all three tasks run concurrently and normally finish within five minutes.
4. You have one initial submission plus two code revisions (three runs total).
5. When 30 minutes expires, or all three runs are used, new submissions are locked.

## Practice kit contents

- `agent.py` — minimal starter implementation
- `interview_sdk.py` — model helper used locally and by the live sandbox
- `practice_policy.md` — original policy used by the local practice runner
- `practice_task.json` — rotated fictional task without an answer key
- `practice_fixture.json` — small database snapshot for that task
- `practice_runner.py` — local conversation and tool runner

The practice runner is intentionally small and is not the formal evaluator. Use
it to verify your function contract, tool-call loop, authentication, confirmation,
and structured outputs.

The practice fixture is intentionally inspectable so you can debug your agent.
Formal task data, expected actions, source identifiers, and traces are not
included in this kit. Formal wording and fictional entities are generated for
each candidate and retest cycle, while runtime identifiers are regenerated for
each run.
