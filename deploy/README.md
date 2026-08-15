# NEKO Public Room — Debian 12 deployment

The supported first-release target is Debian 12. It runs two loopback-only
Python services and one public Nginx reverse proxy; only Nginx listens on the
public network.

1. Install Debian packages `ca-certificates`, `curl`, `nginx`, `python3.11` and
   `python3.11-venv`, install `uv`, create the `neko` system user, and copy this
   tree to `/opt/neko-one`. Run `sudo -u neko uv sync --locked --no-dev` from
   that directory so deployment uses the committed lock file.
2. Copy `.env.public.example` to `/etc/neko-public.env`, replace every secret and
   domain, set `NEKO_PUBLIC_MIN_FREE_MIB` to the disk headroom required by the
   local retention/backup policy, then set owner `root:neko` and mode `0640`.
3. Keep the existing private model/TTS configuration under the service user's
   application data directory. Do not copy keys into the web root.
   Install only a model with proven Web publication rights under
   `/var/lib/neko-public/live2d/<name>/`, then set both `NEKO_PUBLIC_LIVE2D_*`
   values. No character model or default voice ships in this repository.
4. Install the two systemd units, run `systemctl daemon-reload`, then enable and
   start `neko-memory` and `neko-public`.
5. Install the Nginx configuration inside the `http` context (for example under
   `/etc/nginx/conf.d`), issue the TLS certificate, replace
   `neko.example.com`, run `nginx -t`, and only then reload Nginx.
6. Back up `/var/lib/neko-public` plus the memory/config application data every
   day. Test restore on a separate host.

The service automatically applies the finite retention policy configured by
`NEKO_PUBLIC_*_RETENTION_*`. Persisted values changed in `/admin` override the
environment defaults. Retention removes online data; it does not remove copies
from backups, so define and test a separate encrypted-backup expiry policy.

Expected network surface:

- Public: TCP 443 (and optional 80 redirect).
- Loopback only: public API `127.0.0.1:48911`, memory service on its configured
  memory port.
- Never expose the memory port, TTS upstream credentials or admin cookie secret.
- Memory is a weak dependency: an outage degrades recall and writes but must not
  stop `neko-public`.

Use `http://127.0.0.1:48911/api/v1/health/ready` for the same-host monitor or
load balancer and `/api/v1/health/live` only to detect a running process. The
readiness probe returns 503 when SQLite integrity, rollbackable writes, disk
headroom, room initialization, or the private LLM/Persona configuration fails.
Memory, TTS and Live2D remain reported degradations and do not block the text
room. Nginx denies public access to the detailed readiness response.

The default CSP refuses every iframe parent. If the room is later embedded in a
blog, replace `frame-ancestors 'none'` in both the application policy and Nginx
with the exact HTTPS blog origin. Never use `*`.

Before deployment, run from `/opt/neko-one` as the `neko` service user:

```bash
sudo -u neko uv run --locked python scripts/verify_public_room.py
sudo -u neko uv run --locked python scripts/verify_memory_runtime.py
sudo -u neko uv run --locked python scripts/check_public_boundary.py
sudo -u neko uv run --locked python scripts/verify_deployment_security.py
sudo -u neko uv run --locked python scripts/verify_room_capacity.py
sudo -u neko uv run --locked python scripts/verify_backup_restore.py
sudo -u neko uv run --locked python scripts/verify_public_assets.py
sudo -u neko uv run --locked python scripts/verify_provider_acceptance.py
```

The deployment-security script validates the example files, not the installed Nginx binary.
Production acceptance still requires `nginx -t`, an external port scan and
HTTP/WSS checks from outside the VPS.

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
