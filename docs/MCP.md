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

`tools/list` returns:

- one tool per skill the owner may call, named like the skill, with an
  input schema generated from the `inputs` block of its `skill.md`;
- `skill_view(name)` — the full manifest (rules, examples). Agents should
  read it before the first call to a skill, exactly as the built-in loop does;
- `pending_confirm(pending_id)` and `pending_cancel(pending_id)` — the
  confirmation card, as tools;
- `whoami` — the account the token acts as.

Skills an admin disabled in Settings are absent from the list and refused
on call.

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
    "next": "pending_confirm(pending_id) after the human agreed, pending_cancel(pending_id) otherwise"
  }
}
```

The agent shows the preview to its human, then calls `pending_confirm`
or `pending_cancel`. Creates and updates are applied at once and may
return a `pending_id` too; `pending_cancel` undoes them. Pending rows
expire the same way they do for chat cards.

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
