# ChatBridge for Hermes

Hermes-native local MCP workbench for approved local files, patches, commands, durable sessions, images, artifacts, and opt-in worker delegation. It complements ARGOS, which governs the Hermes Codex OAuth credential pool.

Lineage: core executor patterns from [chat-on-steroids](https://github.com/totec448-spec/chat-on-steroids) (approved roots, capability switches, read-only kill switch, bounded results, loopback-only MCP endpoint with per-surface secret paths), reimplemented Hermes-native in Python. No Electron fork, no browser scraping, no extension port.

## Why a separate plugin (not part of ARGOS)

- Different trust boundary: ARGOS promises OAuth tokens only ever go to the official `chatgpt.com` usage endpoint and never scrapes a browser. ChatBridge must never touch ARGOS auth state.
- Different lifecycle: a loopback MCP server + tunnel + approved-folder permissions, vs quota governance.
- Deliberately NOT ported: the Chrome-extension DOM observation/attribution, Compact & Resume browser orchestration, worker-chat browser automation, Desktop computer-control, and tunnel-client bundling. Those would violate the no-scraping non-goal and need a full app, not a Hermes plugin.

## Hermes-first operation

ChatBridge is installed, configured, and run by Hermes. Its MCP endpoint is an optional interoperability surface for a client you explicitly choose; it is not a ChatGPT or Codex desktop-app extension, does not automate a browser, and does not consume web-chat quota.

## Install

Manual copy — `hermes plugins install <github>` deliberately refuses this plugin:
its security scanner returns a dangerous verdict (subprocess execution is the
plugin's purpose; 10 findings, all true positives on `exec.py`/`patch.py`).
You are installing your own code on your own machine, so copy it yourself:

```bash
rm -rf "$HERMES_HOME/plugins/chatbridge"
cp -R /path/to/hermes-plugin-chatbridge "$HERMES_HOME/plugins/chatbridge"
hermes plugins enable chatbridge
hermes chatbridge status
```

Approve at least one folder in `$HERMES_HOME/chatbridge.yaml`:

```yaml
approved_roots: /me/projects/app
read_only: true
allow_exec: false
allow_patch: false
```

Zero approved roots = the bridge answers but every `read` fails closed.

## Use ChatBridge from Hermes

ChatBridge is an MCP server that Hermes can consume itself. This is the
Hermes-native path: it does **not** require ChatGPT web, a browser extension,
or a ChatGPT/Codex desktop-app modification.

1. Configure the capability boundary in `$HERMES_HOME/chatbridge.yaml`.
   A project-scoped example:

   ```yaml
   approved_roots: C:/Users/me/projects/my-app
   read_only: false
   allow_patch: true
   allow_exec: true
   allow_save: true
   exec_allowlist: git status; npm test; python -m pytest
   agents_enabled: true
   agents_max_workers: 2
   port: 18789
   ```

   An explicitly user-authorized full-local-workspace configuration is also
   supported, but grants the MCP client the same filesystem access as your
   Windows user:

   ```yaml
   approved_roots: C:/
   read_only: false
   allow_patch: true
   allow_exec: true
   allow_save: true
   exec_allowlist: cmd /c; powershell -Command; python; py; git; node; npm; uv; hermes
   agents_enabled: true
   agents_max_workers: 2
   port: 18789
   ```

2. Start the loopback server (keep this process running):

   ```bash
   hermes chatbridge serve --port 18789
   ```

   Set `BRIDGE_HOME` to the active Hermes home (on this Windows install it is
   `%LOCALAPPDATA%\\hermes`), then register the local endpoint without printing
   its secret path:

   ```bash
   BRIDGE_HOME="${HERMES_HOME:-$LOCALAPPDATA/hermes}"
   TOKEN="$(tr -d '\r\n' < "$BRIDGE_HOME/chatbridge.token")"
   hermes mcp add chatbridge --url "http://127.0.0.1:18789/${TOKEN}/mcp"
   ```

   At the prompts, choose **No** for separate authentication (the secret path
   already authenticates this loopback endpoint), then enable the desired
   tools. Check registration with `hermes mcp list` and service state with
   `hermes chatbridge status`.

4. Start a **new Hermes session**. MCP tools are discovered at session startup
   and are named `mcp_chatbridge_read`, `mcp_chatbridge_find`,
   `mcp_chatbridge_view_image`, `mcp_chatbridge_apply_patch`,
   `mcp_chatbridge_exec_command`, `mcp_chatbridge_write_stdin`,
   `mcp_chatbridge_download_artifact`, and `mcp_chatbridge_agents` when their
   configuration gates are enabled. An already-running Hermes chat keeps its
   existing tool schema to preserve prompt caching.

5. Use ordinary requests in the new chat, for example: “Find all Python tests
   under C:/Users/me/projects/my-app and run the relevant test command.” The
   agent receives the MCP tools alongside Hermes's normal tools.

The local server is intentionally not a Windows service. After a reboot, start
`hermes chatbridge serve --port 18789` again before beginning a Hermes session,
or use your own supervised service mechanism. Never publish the local endpoint
or its token path through a public tunnel unless you intentionally configure
the tunnel guard in the next section.

## Optional remote MCP client (tunnel)

The bridge binds 127.0.0.1 only. An approved remote MCP client can reach it through a tunnel you run:

```bash
hermes chatbridge serve --port 18789
cloudflared tunnel --url http://127.0.0.1:18789
```

Take the `https://<name>.trycloudflare.com` URL cloudflared prints, put it in
`chatbridge.yaml` as `tunnel_host: <name>.trycloudflare.com`, restart
`serve`, and register this connector URL only with the MCP client you intend to authorize:

```
https://<name>.trycloudflare.com/<secret-from-~/.hermes/chatbridge.token>/mcp
```

Verified end to end: health, `initialize`, `tools/list`, `tools/call read`
all 200 through the public URL; wrong token → 404, other Host → 421,
non-loopback Origin → 403. Three things the tunnel run taught us (all fixed
and regression-tested):

- The loopback guard checks the TCP peer, not the Host header (tunnels
  forward the public hostname as Host).
- `serve` passes `proxy_headers=False`: uvicorn ≥ 0.41 trusts
  `X-Forwarded-For` from loopback peers by default, which would let the
  tunnel's XFF masquerade as the peer.
- The MCP SDK's DNS-rebinding check stays on; `tunnel_host` allowlists the
  public hostname explicitly. Quick-tunnel hostnames are random per run —
  for daily use prefer a named tunnel (stable hostname) and update
  `tunnel_host` accordingly. Treat the full connector URL as a password.

## Workers (opt-in multi-agent)

CoS's flagship is prime/worker chats; its workers are *browser tabs* driven by
the extension — deliberately not ported. The Hermes-native equivalent: a
worker is a background `hermes -z` one-shot run whose report the prime
collects with the `agents` tool (`spawn | message | status | finish`).

```yaml
agents_enabled: true
agents_max_workers: 2   # concurrency cap, hard max 8
worker_provider: ""     # empty = your configured defaults
worker_model: ""
```

Honest differences from CoS to know before enabling:

- Workers spend **Hermes-side model quota** (Codex/API). Each run writes `--usage-file` to
  `$HERMES_HOME/workers/<id>.usage.json` so cost is auditable.
- Workers are one-shot runs, not persistent chats. `message` on a finished
  worker revives it: a follow-up run seeded with (task, prior report, new
  instruction) under the same worker id. Running workers can't take injected
  input — `message` returns current output instead.
- Workers never receive the connector URL and run with
  `HERMES_CHATBRIDGE_WORKER=1`; the bridge refuses `spawn` under that marker,
  so workers cannot spawn workers. Worker cwd is locked to the first
  approved root.
- Default OFF (unlike CoS's on-by-default): enabling runs agent processes as
  your user. Full MCP path verified live (list → spawn → status →
  message/revive → finish → transcript, stub runner); only a real model run's
  stdout is pending a quota reset.

## Write tools (opt-in)

Disabled unless **all three** hold in `chatbridge.yaml`: `read_only: false`, the matching
`allow_patch` / `allow_exec: true`, and at least one approved root. Handlers re-read live
config on every call, so re-enabling read-only takes effect without a restart:

```yaml
read_only: false
allow_patch: true
allow_exec: true
exec_allowlist: npm test; git status
```

- `apply_patch` takes a unified diff, preflights **every** file (context must match, targets
  inside approved roots, no binary/non-UTF-8, new files need existing parents), then applies
  atomically — any failure writes nothing. Deletes verify hunk context too.
- `exec_command` runs one command from `exec_allowlist` (exact or prefix-plus-space match)
  with its cwd in the first approved root, 5-minute cap, 64 KiB output cap. POSIX runs
  argv without a shell; Windows runs the allowlisted string via `cmd` (the allowlist entry
  itself is the control, so keep entries exact).
- Commands are **not** folder-sandboxed once started — same-user privileges, same as CoS.
  Keep the allowlist to build/test/lint commands. Long commands return a `session_id`;
  `write_stdin` polls it, answers stdin prompts, or kills it (`kill: true`); final
  collection retires the session. Note: piped stdout is block-buffered — prefer
  unbuffered child output (`python -u`, `stdbuf -o0`) for live progress.

## File saving and images (read surface)

- `view_image` is always listed (read-gated): PNG/JPEG/GIF/WebP up to 10 MiB, delivered
  to the model as a vision block rather than text.
- `download_artifact` is opt-in (`allow_save: true` + `read_only: false`): saves only
  `https://files.oaiusercontent.com/…` (redirects re-checked), 20 MiB cap, destination
  parents must exist, existing files never overwritten, signed URL query material never
  logged.

## Optional MCP-client connection

1. `hermes chatbridge serve --port 0` (loopback only; prints the port).
2. Expose it via your own HTTPS tunnel (Cloudflare quick tunnel or equivalent) — the full public URL including the random tunnel path plus `/<token>/mcp` is the connector URL (the token segment is the credential; treat the URL as a password).
3. In the approved MCP client, add the connector URL, review its actions, and enable it.
4. Start with one read-only task; confirm the exact paths in the reply before widening anything.

## Safety

- Real MCP protocol (Streamable HTTP, stateless, JSON responses) — verified with a raw-protocol handshake test, not just the SDK client.
- Binds `127.0.0.1` only; Host must be loopback; a present Origin must be loopback.
- Secret path segment; wrong token = 404 (no oracle).
- Request bodies capped at 1 MiB; file payloads 256 KiB each / 512 KiB aggregate.
- Write tools absent from discovery unless explicitly enabled; handlers re-check live config anyway.
- No raw tokens in logs/CLI output.

## Tests

```bash
uv run --with pytest python -m pytest -q tests
hermes plugins doctor "$HERMES_HOME/plugins/chatbridge" --ci
```

## License

MIT.
