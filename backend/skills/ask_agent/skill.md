---
name: ask_agent
description: Hand a question to the household's strong agent (Hermes on the workstation) and return its finished answer.
when_to_use: |
  Use it for anything Yorik does not hold itself: web research, files and notes on the workstation, coding, long reasoning, "ask Hermes".
  Do not use it for calendar, tasks, contacts, documents, photos, bills, letters or email; those are Yorik's own skills.
  Pass the user's question as written, in their language, plus a one-line `context` only when the conversation adds something the agent cannot know.
  The call takes 10 to 60 seconds; tell the user you are asking the agent, then relay the `answer` as it is.
  If the result has `error`, say the agent is not reachable right now and offer to try again later; do not invent an answer.
when_not_to_use: |
  Anything about the household's own data. Anything the user can answer in one line themselves.
inputs:
  question:
    type: string
    required: true
    description: The user's question or task for the agent, verbatim.
  context:
    type: string
    required: false
    description: One or two sentences of conversation context the agent needs (optional).
outputs:
  answer:
    type: string
    description: The agent's reply, plain text. Relay it; do not summarise it away.
  agent:
    type: string
    description: Display name of the agent that answered.
  error:
    type: string
    description: Set instead of `answer` when the agent could not be reached or refused.
cost: 10–60 s, one request to the agent's API
permissions: [admin, member]
side_effects: Sends the question (and context) to the configured agent endpoint.
tags: [agent, delegation, no-mcp]
category: system
---

# ask_agent

Yorik is the household's data server; the strong agent lives elsewhere
(Hermes on the workstation, reachable over the LAN or the Headscale
overlay). This skill is the one door in that direction: one question in,
one finished answer out. No tool forest, no streaming, no state in Yorik.

The agent keeps its own session per Yorik user and conversation
(`X-Hermes-Session-Id`), so follow-up questions in the same chat land in
the same agent session.

Configured in `config.env`: `HOMEOS_AGENT_URL` (e.g. `http://127.0.0.1:8642/v1`),
`HOMEOS_AGENT_KEY`, optional `HOMEOS_AGENT_MODEL` (default `hermes-agent`),
`HOMEOS_AGENT_NAME` (default `Hermes`), `HOMEOS_AGENT_TIMEOUT` seconds (default 150),
`HOMEOS_AGENT_REASONING` (default `none`: the agent answers without thinking,
which is 4-5x fewer tokens for a chat-sized question; `medium` or `high` for
deliberate reasoning).
Without a URL the skill returns `error` and Yorik says so.

Not exposed over MCP (tag `no-mcp`): an agent must not be able to ask
Yorik to ask the agent.
