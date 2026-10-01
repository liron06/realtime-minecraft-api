# Realtime Minecraft management API

A small internal `aiohttp` service for the Realtime Minecraft server on `gust`.
It is intentionally not a generic Docker, command, or RCON proxy. The API binds
only to WireGuard address `10.77.0.2`; startup fails if `API_HOST` contains any
other address, including `0.0.0.0`.

## API

All responses are JSON. Every route except `GET /health` requires
`Authorization: Bearer <API_TOKEN>`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | API process health; no authentication |
| `GET` | `/status` | Fixed Compose service state and health |
| `GET` | `/players` | Runs the fixed RCON command `list` |
| `POST` | `/whitelist/add` | Runs `whitelist add <username>` |
| `POST` | `/whitelist/remove` | Runs `whitelist remove <username>` |
| `POST` | `/server/start` | Runs `docker compose up -d minecraft` |
| `POST` | `/server/stop` | Runs `docker compose stop minecraft` |
| `POST` | `/server/restart` | Runs `docker compose restart minecraft` |

Whitelist bodies must be JSON such as `{"username":"Example"}`. Usernames are
accepted only when they match `[A-Za-z0-9_]{3,16}`. Minecraft's RCON commands
are used while the server is running; this service never edits `whitelist.json`.
A successful API response means the server-side command exited successfully.
Discord ownership and reconciliation remain the responsibility of the bot and
its SQLite database.

## Security design

`app.py` compares bearer tokens with `hmac.compare_digest`. It never includes
the token in a response or application log, and the aiohttp access log is
disabled. Generate a high-entropy token (the deployment commands below use 32
random bytes). Keep TLS-free HTTP strictly inside the trusted WireGuard tunnel.

The API account is **not** a member of the `docker` group. Docker socket access
is root-equivalent, including for `docker compose exec`, so all Docker and RCON
operations pass through the root-owned `realtime-minecraft-control` helper. The
helper accepts only these exact forms:

```
status
players
whitelist-add USERNAME
whitelist-remove USERNAME
start
stop
restart
```

It hard-codes project directory `/opt/realtime-minecraft`, Compose service
`minecraft`, Docker executable `/usr/bin/docker`, RCON executable name
`rcon-cli`, subprocess timeouts, and argument-array execution with no shell.
The API also uses an argument array and a fixed absolute helper path.

The sudoers rule necessarily authorizes invocation of the helper with arguments;
the root-owned helper is the allowlist enforcement boundary. Never make the
helper or its parent directory writable by `realtime-mc-api`. Because `sudo`
must perform this one controlled privilege transition, the systemd service
cannot use `NoNewPrivileges=yes`. Other hardening remains enabled.

The Compose YAML, Compose `.env`, and `/opt/realtime-minecraft` directory must
also not be writable by `realtime-mc-api`: Docker Compose evaluates those files
as root when the helper runs. Bind-mounted game-data subdirectories may retain
the ownership needed by Minecraft, but do not grant this API account access to
them. The helper supplies a minimal fixed environment so caller-controlled
`DOCKER_HOST`, `COMPOSE_FILE`, and similar variables cannot redirect it.

## Deploy on `gust`

Run these steps from an administrative/root shell. First verify host assumptions:

```sh
ip address show
test -x /usr/bin/docker
/usr/bin/docker compose --project-directory /opt/realtime-minecraft config --services
```

   The host must own `10.77.0.2`, `/usr/bin/docker` must exist, and the last command
   must list `minecraft`. If Docker is installed elsewhere, update the single
   `DOCKER` constant in the helper before installing it.

1. Create the locked service account and application directory:

   ```sh
   useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin realtime-mc-api
   install -d -o root -g root -m 0755 /opt/realtime-minecraft-api
   ```

   Confirm the Compose directory and configuration files are not writable by
   that account (fix ownership/modes individually if this prints a path):

   ```sh
   sudo -u realtime-mc-api find /opt/realtime-minecraft -maxdepth 1 -type f -writable -print
   sudo -u realtime-mc-api test ! -w /opt/realtime-minecraft
   namei -l /opt/realtime-minecraft
   ```

2. Copy this repository's release files into `/opt/realtime-minecraft-api`, then
   make them root-owned and create the virtual environment:

   ```sh
   chown -R root:root /opt/realtime-minecraft-api
   chmod -R go-w /opt/realtime-minecraft-api
   python3 -m venv /opt/realtime-minecraft-api/.venv
   /opt/realtime-minecraft-api/.venv/bin/pip install --requirement /opt/realtime-minecraft-api/requirements.txt
   ```

3. Install the privileged helper and validate/install sudoers:

   ```sh
   install -o root -g root -m 0755 deploy/realtime-minecraft-control /usr/local/sbin/realtime-minecraft-control
   visudo -cf deploy/realtime-minecraft-api.sudoers
   install -o root -g root -m 0440 deploy/realtime-minecraft-api.sudoers /etc/sudoers.d/realtime-minecraft-api
   sudo -u realtime-mc-api sudo -n /usr/local/sbin/realtime-minecraft-control status
   ```

4. Create the secret environment file. Do not copy the placeholder token:

   ```sh
   install -o root -g root -m 0600 /dev/null /etc/realtime-minecraft-api.env
   TOKEN_VALUE="$(openssl rand -hex 32)"
   printf 'API_TOKEN=%s\nAPI_HOST=10.77.0.2\nAPI_PORT=8081\nLOG_LEVEL=INFO\n' "$TOKEN_VALUE" > /etc/realtime-minecraft-api.env
   unset TOKEN_VALUE
   ```

   Deliver the same token to the bot on `breeze` using the existing secret
   management channel. Do not put it in source control, chat, command output, or
   ordinary logs. The shell assignment above can remain in interactive shell
   history depending on shell configuration, but its value is generated by the
   command rather than embedded in history.

5. Install and enable systemd:

   ```sh
   install -o root -g root -m 0644 deploy/realtime-minecraft-api.service /etc/systemd/system/realtime-minecraft-api.service
   systemctl daemon-reload
   systemctl enable --now realtime-minecraft-api.service
   systemctl status realtime-minecraft-api.service
   curl --fail http://10.77.0.2:8081/health
   ```

   `enable --now` starts the API immediately and automatically after reboot.

6. Add a defense-in-depth firewall rule. If the WireGuard interface is `wg0`
   and UFW is in use:

   ```sh
   ufw allow in on wg0 from 10.77.0.1 to 10.77.0.2 port 8081 proto tcp comment 'Minecraft API from breeze only'
   ufw status numbered
   ```

   Replace `wg0` only if the actual WireGuard interface has another name. Remove
   any broader pre-existing allow rule for TCP 8081. With nftables, the equivalent
   rule in the input chain is:

   ```nft
   iifname "wg0" ip saddr 10.77.0.1 ip daddr 10.77.0.2 tcp dport 8081 ct state new accept
   ```

   Integrate that rule into the host's persistent nftables ruleset before
   reloading it; do not flush an existing remote host firewall. Ensure the policy
   or a later rule drops other inbound TCP 8081 traffic.

7. Test from `breeze` without printing the secret:

   ```sh
   curl --fail http://10.77.0.2:8081/health
   curl --fail -H "Authorization: Bearer $API_TOKEN" http://10.77.0.2:8081/status
   curl --fail -H "Authorization: Bearer $API_TOKEN" http://10.77.0.2:8081/players
   ```

## Development and tests

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

The tests cover username validation, authentication failures, fixed whitelist
and lifecycle actions, shell-free subprocess invocation, the unauthenticated
health route, and sanitized errors.

## Operational limitations

- Traffic is bearer-token HTTP, not TLS. Binding and firewalling to WireGuard is
  essential; any process or administrator able to inspect traffic on that tunnel
  can potentially obtain the token.
- One shared token provides no per-caller identity, audit attribution, rotation
  overlap, or rate limiting. Rotate it by atomically replacing the environment
  file and restarting the service.
- `/health` reports only API process health. `/status` queries Compose. When the
  Minecraft container defines no Docker health check, `health` is `null` and a
  running container is reported as healthy based on its running state.
- The helper depends on Docker Compose v2 JSON output and the image-provided
  `rcon-cli`. Validate both after image or Docker upgrades.
- Successful whitelist execution does not prove Discord/SQLite ownership or
  reconcile external changes; the bot remains the source of truth.
