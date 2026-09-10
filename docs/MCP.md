# Yorik as an MCP server

Yorik exposes its skills registry over the [Model Context Protocol](https://modelcontextprotocol.io)
so an outside agent can use Yorik the way the built-in assistant does:
same skills, same permissions, same confirmation cards.

Typical callers: a stronger agent on the same network (Hermes on a
workstation with a large model), Claude Code, or a script that uses the
official MCP SDK.

## Endpoint

```
POST http://<yorik-host>:8000/mcp
Authorization: Bearer yk_…
```

Transport is Streamable HTTP in its JSON flavour: one JSON-RPC message
per request, no server-initiated stream. `GET /mcp` answers 405. That
is enough for every client that speaks Streamable HTTP; SSE-only clients
are not supported.

## Tokens

A personal API token is bound to one Yorik account. It is created under
**Settings → You → API tokens** and shown once. Yorik stores only its
hash. Revoking a token in the same card ends every session that used it.

What the token can do is exactly what its owner can do in chat. The
skills read the owner's role and workspace scoping from the same
`SkillContext` the chat path builds, so an agent connected with a
member's token sees a member's tools and a member's data.

A token also works on the REST API (`Authorization: Bearer yk_…` on any
`/api/*` route), with one exception: a token cannot mint further
tokens. That stays a browser action.

## Tools

Two surfaces, chosen by the URL.

**Full (default, `/mcp`)** lists one tool per skill with the complete
input schema generated from its `skill.md`, plus `skill_view`,
`pending_confirm`, `pending_cancel`, `notify`, `whoami`. Hermes keeps MCP
schemas behind a describe/call bridge and fetches only the one it needs,
so 65 tools cost it nothing per turn.

**Compact (`/mcp?tools=compact`)** mirrors Yorik's own two-step loop for
clients that put every tool schema into the prompt (about 11 KB instead
of 64 KB):

- `invoke_skill(name, args)` — runs a skill. Its description carries the
  index of every skill the owner may use: name, one line, argument names
  (`*` = required). Agents call it directly; a wrong argument name is
  answered with the valid keys. `YORIK_MCP_REQUIRE_VIEW=1` in config.env
  adds a read-first gate (the first call to a skill is refused until its
  manifest was read) for weaker models — off by default because it costs
  two extra agent rounds per skill.
- `skill_view(name)` — the full manifest (rules, examples, argument
  details) for skills with rules worth reading before the call.
- `list_skills` — the index again.
- `pending_confirm`, `pending_cancel`, `notify`, `whoami`.

Skills an admin disabled in Settings are absent in both modes and refused
on call. Skills tagged `no-mcp` (like `ask_agent`) never appear.

## Confirmations

Yorik never deletes on an agent's say-so. A destructive skill stages the
deletion and the tool result carries a `pending_confirmation` block:

```json
{
  "result": {"pending": true, "pending_id": "…", "event": {"title": "Zahnarzt", "…": "…"}},
  "pending_confirmation": {
    "pending_id": "…",
    "skill": "delete_calendar_event",
    "preview": {"mode": "confirm_before", "…": "…"},
    "next": "Nothing is deleted yet. The owner confirms this in the Yorik app (notification bell); pending_confirm is not allowed for this account. pending_cancel(pending_id) withdraws it."
  }
}
```

By default the agent cannot run it. The staged deletion appears as a
card in the owner's notification bell in the Yorik app, with Delete and
Keep buttons; `pending_confirm` over MCP answers with an error that says
so, and `pending_cancel` withdraws the request. The agent's job is to
tell its human that the card is waiting.

A user who trusts their agent can flip **Settings → You → Beta safety →
Let agents confirm deletions**. Then `pending_confirm(pending_id)` runs
the deletion, and the agent is expected to ask first. The switch is a
browser-session action; a token cannot set it.

Creates and updates are applied at once and may return a `pending_id`
too; `pending_cancel` undoes them. Pending rows expire after an hour,
the same way chat cards do.

## Errors

- Wrong or missing token → HTTP 401 with `WWW-Authenticate: Bearer`.
- Tool the owner may not call, or no such tool → JSON-RPC error `-32602`.
- A skill that refuses or fails (bad arguments, ownership gate, business
  rule) → a normal result with `isError: true` and the skill's message as
  text. The message is the same one the built-in assistant gets, including
  the argument suggestions.

## Hermes

```yaml
mcp_servers:
  yorik:
    url: "http://127.0.0.1:8000/mcp"
    headers:
      Authorization: "Bearer yk_…"
    timeout: 120
```

Put the token in the config only for the account Hermes should act as.
A second Hermes profile for another household member gets its own token.

## Claude Code

```
claude mcp add --transport http yorik http://127.0.0.1:8000/mcp --header "Authorization: Bearer yk_…"
```

## Smoke test

```
curl -s -X POST http://127.0.0.1:8000/mcp -H "Authorization: Bearer yk_…" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"whoami","arguments":{}}}'
```

## What is not there yet

- No server-initiated stream (no progress notifications, no `listChanged`).
- No OAuth; tokens are the only credential.
- No resources or prompts, only tools.

## Reporting back: `notify`

Long-running work on the agent's side (a research job, a cron result)
ends with `notify(title, body, url)`. The message lands in the token
owner's bell and is pushed to their phone; nobody can notify anyone else.
Together with `ask_agent` this makes Yorik's own app the channel to the
agent, no messenger required.

## The other direction: Yorik asks the agent

Household members who talk to Yorik's own chat (phone, tablet, kiosk)
can reach the strong agent too. One skill, `ask_agent`, hands a question
to an OpenAI-shaped chat endpoint and relays the finished answer. The
reference is Hermes' API server (`hermes gateway` platform `api_server`,
port 8642).

**Each person has their own agent.** Settings → You → **My agent**
stores the URL, a name and the key on the profile; what a person asks
goes to that person's machine and nowhere else. Somebody without an
agent gets told so and Yorik answers as best it can. The household
agent in `config.env` is used for people without their own only when
the admin switches that on:

```
HOMEOS_AGENT_URL=http://127.0.0.1:8642/v1     # the household agent (optional)
HOMEOS_AGENT_KEY=<API_SERVER_KEY from ~/.hermes/.env>
HOMEOS_AGENT_NAME=Hermes
HOMEOS_AGENT_SHARED=0                         # 1: members without their own agent use this one
HOMEOS_AGENT_REASONING=none                   # thinking level for questions from Yorik
```

A Hermes on another PC is reachable over Tailscale
(`http://<tailscale-name>:8642/v1`); Hermes' API server binds to
127.0.0.1 by default, so either bind it to the Tailscale address or
put `tailscale serve` in front.

Yorik's assistant uses it for what Yorik does not hold: web research,
files and notes on the workstation, coding, long reasoning. The agent
keeps one session per Yorik user and conversation (`X-Hermes-Session-Id`),
so follow-ups stay in context. When the workstation is off, the skill
reports that and Yorik says so instead of guessing.

`ask_agent` is deliberately not offered over MCP: an agent cannot ask
Yorik to ask the agent.

