# Public route and origin contract

## Command origin

`/gws-watch` takes a service name and a JSON request, not an origin. For every
start, status or stop, the gateway supplies `source` (platform, `user_id`, chat
and optional thread), physical `session_id` and `session_key` as command kwargs.
The plugin uses `ctx.profile_name` and checks the active home against its registered
home. The principal is the command sender, not the configured route owner.

The command rejects missing sender, chat, physical session or route key, as well
as bot-authored commands and local/CLI, cron or webhook origins. In a new messaging chat, send a normal
message first; slash commands do not create a session. No binding cache, private
ContextVar reader, process-global routing fallback or agent watch tool is used.

## Route manager

`RouteManager(ctx=None, *, home=None, store=None, config=None, profile="default")`
uses the existing WatchStore transaction API. Pass the SAME profile-local store to
host and worker. `config` is the operator route object below, or a zero-argument
callable returning its current value. An omitted config fails closed at create;
there is no automatic native-store discovery. Prefer a callable reloading settings
through the parent's supported config loader for long-running workers. A static
mapping detects in-memory changes/reloads, not edits to a file it never rereads.

`create(watch, spec, origin)` returns plugin-local per-watch `name`, not the shared
operator route name. Keep this name in existing watch route_name/bridge_name fields.
`read(name)` returns only local non-secret metadata. `remove(name, watch_id)` removes
only matching plugin metadata and reports operator_route_preserved. No host route
is ever created, changed, disabled, or deleted. Metadata lives in `route_bindings`
in the existing watch SQLite database, preserving the existing restrictive directory.

Register `bridge_hook(store, ctx, routes, profile=profile)` on public
`pre_gateway_dispatch`. The callback accepts `event`, `session_store`, and other
host hook kwargs. Parent's existing bridge_callback wrapper must forward routes
and profile. Worker must construct RouteManager with the same config and profile;
`RouteManager(home=...)` alone deliberately cannot send.

## Small operator configuration

Place this object under a parent-selected plugin setting (recommended
`plugins.entries.gws-workspace.settings.watch_routes`). Do not put it in an
installed snapshot or expose it through a model tool. Supply strong random secrets
of at least 32 characters using the host's protected configuration/secrets setup.
Values below are placeholders, not credentials.

```yaml
routes:
  - origin:
      profile: default
      principal: USER_ID
      platform: discord
      chat_id: CHAT_ID
      thread_id: null
      session_key: EXACT_HOST_ROUTE_KEY
    new_session:
      name: gws-notify
      url: https://gateway.example/webhooks/gws-notify
      secret: REPLACE_WITH_A_STRONG_RANDOM_HMAC_SECRET
      destination: {platform: discord, chat_id: CHAT_ID, thread_id: null}
      skills: []
      enabled: true
    same_session:
      bridge_name: gws-watch-bridge-main
      url: https://gateway.example/webhooks/gws-watch-bridge-main
      secret: REPLACE_WITH_A_DIFFERENT_RANDOM_HMAC_SECRET
      enabled: true
```

Each origin is an exact six-field match, including null thread_id; no wildcard or
model-selected route. At most 1024 configured entries. Duplicate matches refuse.
Physical IDs are deliberately not in operator configuration: a new watch after
`/new` captures the new physical context without recreating the operator route. Non-default profiles
must use `/p/PROFILE/webhooks/NAME`. HTTPS is required except loopback HTTP. No
URL credentials, query strings, fragments, or redirects are accepted. The bridge
name MUST start with `gws-watch-bridge-`; the hook suppresses this namespace even
when config is missing/invalid or raw_message is malformed.

The operator provisions routes with the public `hermes webhook subscribe` CLI
(consult that host's `--help` for its exact flags). The plugin never imports CLI
implementation helpers or reads webhook_subscriptions.json. Operator requirements:

- Native receiver: generic HMAC verification, exact profile, enabled route. Use
  `events: [gws-watch]` for new-session and `[gws-watch-bridge]` for bridge.
- New-session: `deliver` and fixed `deliver_extra.chat_id/thread_id` must equal the
  configured destination; no payload templating in destination. `deliver_only`
  and `mirror_to_session` must both be false. Configure the chosen skills directly
  on the operator route; requested watch skills must exactly match plugin config.
- New-session prompt: include `{__raw__}` and describe its events as untrusted
  external metadata, never instructions to mutate Google data or follow links.
  The plugin adds the locally stored watch instructions as `instructions` in the
  signed payload. The operator prompt should distinguish those owner instructions
  from untrusted events, and report only requested changes.
- Bridge: `deliver: log`, no skills, no mirroring, no deliver-only bypass and no
  coalescing/scheduled execution that bypasses the hook. Use a fallback prompt
  saying this is an internal bridge, perform no actions, and reply NO_REPLY.
  Prompt text is defense in depth, not a substitute for the hook.
- Enable the documented `plugins.entries.gws-workspace.allow_gateway_injection:
  true` consent setting for same-session. No private consent-probe method is used;
  callable API availability is not proof of consent, and the injection return is
  authoritative for admission.

The plugin hashes the exact selected config including its secret, stores only the
hash and non-secret per-watch metadata, and rechecks config before send and bridge
injection. Edits/disable/secret rotation require stop and recreate; they never
silently retarget an old watch. A removed operator config cannot be reconstructed
from plugin state. Provision a route separately for each exact native destination.

## Host evidence and limits

- The gateway builds the native authenticated webhook
  source platform, `chat_name=webhook/NAME`, `user_id=webhook:NAME`, routed profile,
  and raw payload. The hook requires that exact source identity/profile plus the
  configured bridge and matching durable watch, origin, generation and outbox row.
  It trusts Hermes native ingress authentication; the hook is not another HTTP
  authentication layer. Forged source objects from another trusted plugin are
  outside the isolation boundary, as are malicious operators.
- `ctx.inject_message` supports injection
  with session_key/origin, but NO expected_session_id. The bridge prechecks the
  physical session through hook-provided `session_store.lookup_by_session_key`,
  then transactionally claims outbox before injection. `/new` between lookup and
  host admission can still target a replacement session. This accepted residual
  reset race is explicitly tested and is NOT race-free delivery.

Important limitations:

1. The plugin trusts the gateway's command metadata. It does not isolate accounts
   from a malicious plugin or operator. Use trusted chats or sender-isolated sessions.
2. Operator config is an attestation of the remote native route's destination,
   prompt, profile, skills and signature mode. There is no public readback API here
   to prove the receiver still matches it. Direct receiver edits without updating
   plugin config cannot be detected. Stop watches before editing receiver routes;
   verify configuration with public CLI. Do not claim receiver-side pinning.
3. Native HTTP accepted/duplicate receipts mean admission, not completion or actual
   external delivery. Stable request IDs and HMAC v2 timestamp/body signatures
   preserve the durable worker retry contract; replies are read at most 65536 bytes.
   No redirects are followed, including same-origin redirects.
4. Same-session true means scheduled/admitted. False or exception after claiming
   becomes uncertain and is not blindly replayed. Crash recovery retains the
   existing durable outbox uncertainty behavior. No exactly-once guarantee.
5. Historical end-to-end delivery tests predate the slash-command origin. See
   [verification](verification.md) for measured coverage and pending live checks.
