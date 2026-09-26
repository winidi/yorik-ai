# Tailscale for the all-in-one install

`serve.json` publishes Yorik (443), photos (8443) and documents (8444)
over HTTPS inside your tailnet. `serve-funnel.json` adds the public join
page for invites on 10000; switch to it once Funnel is allowed for this
machine in the Tailscale admin console (Access controls, node attribute
`funnel`): set `YORIK_TS_SERVE=serve-funnel.json` in `.env`, then
`docker compose up -d tailscale`.

HTTPS certificates must be enabled once for the tailnet:
https://login.tailscale.com/admin/dns (MagicDNS + HTTPS).
