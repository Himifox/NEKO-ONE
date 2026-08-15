# NEKO Public Room deployment

The first release runs two loopback-only Python services and one public reverse
proxy. Only Nginx listens on the public network.

1. Install Python 3.11 and `uv`, create the `neko` system user, copy this tree to
   `/opt/neko-one`, then run `uv venv --python 3.11` and
   `uv pip install -r requirements-public.txt`.
2. Copy `.env.public.example` to `/etc/neko-public.env`, replace every secret and
   domain, then set owner `root:neko` and mode `0640`.
3. Keep the existing private model/TTS configuration under the service user's
   application data directory. Do not copy keys into the web root.
4. Install the two systemd units, run `systemctl daemon-reload`, then enable and
   start `neko-memory` and `neko-public`.
5. Install the Nginx configuration, issue the TLS certificate, replace
   `neko.example.com`, and reload Nginx.
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

Before deployment, run:

```powershell
uv --cache-dir .uv-cache run --active --no-sync python scripts/verify_public_room.py
uv --cache-dir .uv-cache run --active --no-sync python scripts/check_public_boundary.py
```
