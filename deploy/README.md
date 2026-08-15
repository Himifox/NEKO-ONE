# NEKO Public Room — Debian 12 deployment

The supported first-release target is the Debian 12 VPS that already hosts
`pardofelis.wiki` through 1Panel-managed OpenResty. NEKO adds two loopback-only
Python services and reuses that public edge; do not start a second public Nginx.

1. Install Debian packages `ca-certificates`, `curl`, `python3.11`,
   `python3.11-venv`, `postgresql`, `postgresql-client` and `apache2-utils`.
   Install `uv`, create the `neko` system user, and copy this
   tree to `/opt/neko-one`. Run `sudo -u neko uv sync --locked --no-dev` from
   that directory so deployment uses the committed lock file.
2. Keep PostgreSQL on loopback, create a dedicated non-superuser role and an
   owned database, then verify that TCP 5432 is not reachable from the public
   network. Do not reuse the `postgres` role in `NEKO_PUBLIC_DATABASE_URL`.
3. Copy `.env.public.example` to `/etc/neko-public.env`, replace every secret and
   domain, set `NEKO_PUBLIC_MIN_FREE_MIB` to the disk headroom required by the
   local retention/backup policy, set the percent-encoded PostgreSQL URL, then
   set owner `root:neko` and mode `0640`.
4. Keep the existing private model/TTS configuration under the service user's
   application data directory. Do not copy keys into the web root.
   Install only a model with proven Web publication rights under
   `/var/lib/neko-public/live2d/<name>/`, then set both `NEKO_PUBLIC_LIVE2D_*`
   values. No character model or default voice ships in this repository.
5. Install the two systemd units, run `systemctl daemon-reload`, then enable and
   start `neko-memory` and `neko-public`.
6. Create an edge-only administrator credential
   with `sudo htpasswd -c /etc/nginx/neko-admin.htpasswd neko-admin`. Use a
   different password from `NEKO_PUBLIC_ADMIN_PASSWORD`, set owner
   `root:<openresty-worker-group>` and mode `0640`, and never add this file to
   the repository or a web root.
7. Create `neko.pardofelis.wiki` in 1Panel, issue its TLS certificate, then merge
   the supplied OpenResty/Nginx declarations into the existing `http` context.
   Preserve both `auth_basic` directives for `/admin` and `/api/v1/admin/`, keep
   the platform's existing default-host rejection, run the OpenResty
   configuration check, and only then reload it.
8. Back up PostgreSQL, `/var/lib/neko-public`, Memory data and private config
   every day. Test `pg_restore` plus the file restore on a separate host.

One safe local database bootstrap is:

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE neko_public LOGIN PASSWORD 'replace-with-a-generated-password';
CREATE DATABASE neko_public OWNER neko_public ENCODING 'UTF8' TEMPLATE template0;
REVOKE ALL ON DATABASE neko_public FROM PUBLIC;
GRANT CONNECT ON DATABASE neko_public TO neko_public;
SQL
```

The example password is a placeholder. Generate a unique value, percent-encode
reserved URL characters, and store it only in `/etc/neko-public.env` and the
approved secret/backup system. On Debian, confirm `listen_addresses` remains
`localhost` unless a separately firewalled private database network is used.

The service automatically applies the finite retention policy configured by
`NEKO_PUBLIC_*_RETENTION_*`. Persisted values changed in `/admin` override the
environment defaults. Retention removes online data; it does not remove copies
from backups, so define and test a separate encrypted-backup expiry policy.

Expected network surface:

- Public: TCP 443 (and optional 80 redirect).
- Loopback only: public API `127.0.0.1:48911`, PostgreSQL `127.0.0.1:5432`, and
  Memory Service on its configured memory port.
- `/admin`, `/admin-assets/*` and `/api/v1/admin/*` require the independent
  OpenResty Basic Auth credential before the application login is reachable.
- Never expose PostgreSQL, the Memory port, TTS upstream credentials, admin
  cookie secret, database password or either administrator password.
- PostgreSQL is a required dependency; loss of the primary business database
  makes readiness fail. Redis is not used in the first release.
- Memory is a weak dependency: an outage degrades recall and writes but must not
  stop `neko-public`.

Use `http://127.0.0.1:48911/api/v1/health/ready` for the same-host monitor or
load balancer and `/api/v1/health/live` only to detect a running process. The
readiness probe returns 503 when the PostgreSQL connection/schema or rollbackable
write, local disk headroom, room initialization, or private LLM/Persona
configuration fails.
Memory, TTS and Live2D remain reported degradations and do not block the text
room. Nginx denies public access to the detailed readiness response.

The first release links from `pardofelis-web` to the NEKO subdomain and keeps
`frame-ancestors 'none'`. If iframe embedding is considered later, replace it
in both policies with exactly `https://pardofelis.wiki` and revalidate Cookie,
audio and clickjacking behavior. Never use `*`.

Run destructive deterministic database verification only against a dedicated
throwaway database whose name contains `verify`, never against the production
database. It requires the explicit reset gate:

```bash
sudo -u neko -H bash
cd /opt/neko-one
export NEKO_PUBLIC_DATABASE_URL='postgresql://neko_verify:...@127.0.0.1/neko_verify'
export NEKO_POSTGRES_RESTORE_URL='postgresql://neko_verify:...@127.0.0.1/neko_restore_verify'
export NEKO_VERIFY_ALLOW_DATABASE_RESET=1
uv run --locked python scripts/verify_public_room.py
uv run --locked python scripts/verify_postgres_migration.py
uv run --locked python scripts/verify_memory_runtime.py
uv run --locked python scripts/check_public_boundary.py
uv run --locked python scripts/verify_deployment_security.py
uv run --locked python scripts/verify_room_capacity.py
uv run --locked python scripts/verify_backup_restore.py
uv run --locked python scripts/verify_public_assets.py
uv run --locked python scripts/verify_provider_acceptance.py
unset NEKO_VERIFY_ALLOW_DATABASE_RESET NEKO_POSTGRES_RESTORE_URL
exit
```

After restoring `/etc/neko-public.env`, production checks use its real DSN but
must not set `NEKO_VERIFY_ALLOW_DATABASE_RESET`. Start the services and inspect
the loopback readiness endpoint instead.

The deployment-security script validates the example files, while the Debian
CI also installs the distribution Nginx package and runs `nginx -t` with an
ephemeral certificate and empty throwaway credential file. Production
acceptance still requires `nginx -t` on the actual host, an external port scan,
HTTP/WSS checks from outside the VPS, and confirmation that `/admin` returns
`401` without the edge credential before the application login page is served.

The capacity command above is the short deterministic baseline. Run the 30
minute profiles and 24 hour soak from
[`docs/operations/capacity-and-soak.md`](../docs/operations/capacity-and-soak.md)
before public acceptance.

The backup command creates a plaintext staging snapshot and must not be copied
off-host until it is encrypted. Exact create, verify, isolated restore and
separate-host drill steps are in
[`docs/operations/backup-and-restore.md`](../docs/operations/backup-and-restore.md).

These deterministic checks do not spend provider quota. Run the redacted
preflight and explicitly acknowledged real LLM/Memory/TTS smoke from
[`docs/operations/provider-acceptance.md`](../docs/operations/provider-acceptance.md)
before public acceptance.
