# ChatBridge for Hermes

Local MCP executor that lets **ChatGPT web chat** (your ChatGPT subscription quota) drive approved local work through Hermes — the complement to ARGOS, which governs the Codex API quota pool.

Lineage: core executor patterns from [chat-on-steroids](https://github.com/totec448-spec/chat-on-steroids) (approved roots, capability switches, read-only kill switch, bounded results, loopback-only MCP endpoint with per-surface secret paths), reimplemented Hermes-native in Python. No Electron fork, no browser scraping, no extension port.

## Why a separate plugin (not part of ARGOS)

- Different trust boundary: ARGOS promises OAuth tokens only ever go to the `chatgpt.com` usage endpoint and never scrapes the browser. A local executor for web ChatGPT must never touch ARGOS auth state.
- Different lifecycle: a loopback MCP server + tunnel + approved-folder permissions, vs quota governance.
- Deliberately NOT ported: the Chrome-extension DOM observation/attribution, Compact & Resume browser orchestration, worker-chat browser automation, Desktop computer-control, and tunnel-client bundling. Those would violate the no-scraping non-goal and need a full app, not a Hermes plugin.

## Requirements / plan-tier reality

ChatGPT Developer mode + custom MCP apps: full write actions need Business/Enterprise/Edu (Business may need admin); **Plus/Pro web plans are read/fetch-limited**. So v0 is genuinely useful read-only (read approved files, bounded search results later), and write tools stay disabled until your workspace supports them.

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

## Exposing it to ChatGPT (tunnel)

The bridge binds 127.0.0.1 only. ChatGPT reaches it through a tunnel you run:

```bash
hermes chatbridge serve --port 18789
cloudflared tunnel --url http://127.0.0.1:18789
```

Take the `https://<name>.trycloudflare.com` URL cloudflared prints, put it in
`chatbridge.yaml` as `tunnel_host: <name>.trycloudflare.com`, restart
`serve`, and register this connector URL in ChatGPT (Developer mode → MCP):

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

## Connect ChatGPT

1. `hermes chatbridge serve --port 0` (loopback only; prints the port).
2. Expose it via your own HTTPS tunnel (Cloudflare quick tunnel or equivalent) — the full public URL including the random tunnel path plus `/<token>/mcp` is the connector URL (the token segment is the credential; treat the URL as a password).
3. In ChatGPT web: enable Developer mode, create a custom MCP app of Tunnel type pointing at it, review actions, enable.
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
