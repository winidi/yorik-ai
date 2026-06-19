# Caddy reverse proxy for Yorik tenants

Each tenant Yorik binds to `127.0.0.1:<allocated-port>` (8001, 8002,
…). To expose them on the web under tidy subdomains, point Caddy at
the per-tenant snippets that `scripts/create-tenant.sh` drops into
`infra/caddy/tenants/<name>.caddy`.

## Operator setup (one-time)

In your main `Caddyfile` (typically `/etc/caddy/Caddyfile`):

```caddy
# Host Yorik on the apex domain.
yorik.example.com {
    reverse_proxy 127.0.0.1:8000
}

# Per-tenant subdomains — picked up automatically as you create
# tenants. The wildcard cert (or per-cert ACME) is your call.
import /home/isee/yorikai/yorik-ai/infra/caddy/tenants/*.caddy
```

Then reload Caddy:

```bash
sudo systemctl reload caddy
```

## Per-tenant snippet shape

`create-tenant.sh` writes a file like this for tenant `mom` (port
8001):

```caddy
mom.yorik.example.com {
    reverse_proxy 127.0.0.1:8001
}
```

The root domain comes from `YORIK_TENANT_ROOT` in `config.env`
(defaults to `localhost` so dev installs Just Work — browsers
resolve `*.localhost` to 127.0.0.1). Set it to your real domain
before creating tenants you want to expose.

## Reloading after tenant changes

Caddy doesn't auto-reload when a file is added/removed. After each
`create-tenant.sh` or `drop-tenant.sh`, reload:

```bash
sudo systemctl reload caddy
```

`scripts/create-tenant.sh` prints this command at the end as a
reminder.

## Removing a tenant

`scripts/drop-tenant.sh` deletes the per-tenant snippet so a
subsequent Caddy reload removes the route. Until the reload runs,
the subdomain still answers but proxies to a dead port — clients
get a 502.

## Without Caddy

The Yorik install works without Caddy — tenants are reachable at
`http://<host>:<port>/` (e.g. `http://192.168.1.5:8001/`). Caddy is
the polish layer, not a hard dependency.
