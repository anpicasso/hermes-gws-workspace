# Hermes Google Workspace

Multi-account Google Workspace access through the [native `gws` CLI](https://github.com/googleworkspace/cli): curated Gmail, Drive and Calendar tools, guarded advanced commands, and targeted change notifications.

**Native `gws` owns Google credentials, decryption and refresh.** This public plugin does not implement OAuth login, export tokens, or require the optional remote-login companion. One Hermes profile belongs to one trusted operator; all authorized sessions in that profile can use its connected accounts.

## Install and configure

Requirements: Hermes with the [plugin APIs listed here](docs/compatibility.md), native `gws` (tested baseline 0.22.5), and the Python dependencies declared in `plugin.yaml` (PyYAML, aiohttp and google-auth with requests). Watches require POSIX locking and a separately supervised worker. No binary, cloud provisioning or worker auto-start is included.

This README describes the **unreleased v0.10.0 candidate**: watches use a slash command on unmodified Hermes. The published **v0.9.1** still uses agent watch tools; the install below gets that release, not the candidate.

```bash
hermes plugins install anpicasso/hermes-gws-workspace --ref v0.9.1 --enable
hermes config set plugins.entries.gws-workspace.settings.gws_bin /absolute/path/to/gws
```

Use `hermes --profile <name>` for another profile. Check the installer result for live reload; a fresh session may be needed for updated tools, and older frontends may require an external process reload.

### Google OAuth and accounts

You need your own **Google Cloud Desktop app OAuth client**, not an API key. The `gcloud` executable is optional for login. [Create the client, enable APIs and configure consent](docs/oauth-setup.md), then use [native per-account login/logout](docs/login.md):

```bash
hermes gws setup person@example.com           # interactive gcloud / Desktop client wizard
hermes gws login person@example.com --readonly
hermes gws accounts
hermes gws status person@example.com
hermes gws logout person@example.com
```

These local commands handle account directories and isolation automatically; native gws still performs OAuth. `gws` is resolved on PATH unless `plugins.entries.gws-workspace.settings.gws_bin` sets an explicit executable. Setup/login/logout refuse active watches or pending cleanup. Setup requires `gcloud` and operator input in Cloud Console; skip it if this slot already has a Desktop client. Login writes in place: a failed or wrong-account attempt is blocked from routing, not rolled back.

Each account has a native configuration directory:

```text
$HERMES_HOME/gws-oauth/accounts/<account>/
```

The legacy `gws-oauth` storage name is deliberately retained; renaming the plugin must not silently move or lose existing accounts. Account selection chooses that directory, not an exported token. Global native accounts, raw-token environment variables and global Application Default Credentials are not automatically adopted. Native `gws auth` remains usable by the operator; the agent-facing `gws_api` blocks the auth namespace.

### Optional remote login

The optional [hermes-gws-remote-auth](https://github.com/anpicasso/hermes-gws-remote-auth) companion is publicly available but **is not being submitted to the Hermes catalog**. It adds chat-based `gws_login` and approval-gated local `gws_logout`, sharing this plugin's native account backend. It is separate because remote consent and cross-device callback handling introduce chat-history exposure and gateway-specific bindings that are unnecessary for Workspace tools and watches. Keeping it outside this catalog submission makes that convenience an explicit opt-in, not a dependency or a claim of catalog compliance. Without it, use the terminal commands above. Never paste client JSON or refresh tokens into chat.

## Tools

Load the bundled operational skill with `skill_view("gws-workspace:workspace")` for accounts, watch commands and public HTTPS ingress options.

The candidate registers **15 Workspace tools**, two hooks (`pre_tool_call`, `pre_gateway_dispatch`) and the `/gws-watch` command:

- Accounts: `gws_accounts` (list or sanitized native health).
- Gmail: `gws_gmail_search`, `gws_gmail_read`, `gws_gmail_draft`, `gws_gmail_send`.
- Drive: `gws_drive_search`, `gws_drive_read`, `gws_drive_upload`, `gws_drive_share`.
- Calendar: `gws_calendar_list`, `gws_calendar_freebusy`, `gws_calendar_create`, `gws_calendar_update`.
- Discovery/advanced: `gws_schema`, `gws_api`.

Every Workspace operation selects an explicit account. Known reads run directly; mutations, sends/shares, local file input/output and unknown methods/options request the native Hermes approval gate. The configured host approval mode still applies. A model-supplied `approved` field cannot grant permission.

```json
{"tool":"gws_accounts","arguments":{"check_health":true}}
```

```json
{"tool":"gws_gmail_search","arguments":{"account":"person@example.com","query":"is:unread"}}
```

[Tool mappings and examples](docs/api-tools.md). Relative files resolve under `$HERMES_HOME/gws-oauth/files/`, not the gateway's working directory. Absolute paths retain their meaning. The CLI is not a filesystem sandbox. Large successful output is saved privately as `output_file`; process and delete it after use. Raw native failures are sanitized; successful results can contain private Workspace content.

## Notifications

```text
Gmail users.watch → authenticated Pub/Sub HTTPS push ┐
Drive changes.watch → HTTPS callback                 ├→ durable hints/cursors
Calendar events.watch → HTTPS callback               ┘  → local filters → outbox → Hermes
```

Send `/gws-watch <gmail|drive|calendar> <JSON object>` in the messaging chat where you want notifications. In a new chat, send a normal message first so Hermes creates a physical session. The agent can draft the command, but you must send it yourself. The WhatsApp bridge supports only non-shared DMs; groups and unknown chat types are refused. `/gws-watch` or `/gws-watch help` shows usage; the gateway also accepts `/gws_watch`.

```text
/gws-watch gmail {"action":"start","account":"person@example.com","target":{"mailbox":true},"filters":{"from_addresses":["alice@example.com"]},"delivery_mode":"same_session","minutes":30}
/gws-watch gmail {"action":"status"}
/gws-watch gmail {"action":"stop","watch_id":"WATCH_ID"}
```

Replace the account and sender before starting; use the returned watch ID to stop. `same_session` sends matches to this conversation; `new_session` runs the configured webhook separately. The command uses the gateway's sender and session, not destination IDs in the JSON. Local/CLI, cron, webhook and senderless requests cannot manage conversational watches.

The operator provisions delivery routes and public HTTPS ingress; one separately supervised worker per profile owns renewal, reconciliation and cleanup. Gmail push uses verified Google OIDC identity, exact audience/subscription/mailbox checks and durable acceptance before acknowledgement. Google API reads and renewals remain short account-locked native CLI calls. We do not run a persistent native watch alongside calls sharing its credential cache.

[Watch setup and limitations](docs/watch-setup.md). Same-session injection retains a concurrent `/new` race because the host has no atomic physical-session fence. Webhook admission is not proof of agent execution or delivery.

## Migration, security and removal

The plugin ID is `gws-workspace` (toolset `gws_workspace`); account storage paths are unchanged. Before upgrading from our `gws-oauth` or pre-catalog v0.9.0 `google-workspace` build, stop watches, finish cleanup, stop the worker and disable that old installation; duplicate tool names must not coexist. Preserve account directories and move this plugin's settings and injection consent to `plugins.entries.gws-workspace`, then update the worker path and restart it. Do not copy credentials into the installed plugin directory. Update any explicit toolset allowlist and use remote-auth 0.1.1 or newer if wanted.

The existing Hermes catalog entry named `google-workspace` belongs to another maintainer: do not overwrite its settings or treat it as this plugin's previous installation.

Before removal, stop watches, allow remote cleanup, then stop the worker. Operator-provisioned Pub/Sub and Hermes routes are never deleted by this plugin. Removing the plugin does not remove credentials, watch state, or response files.

```bash
hermes plugins disable gws-workspace
hermes plugins remove gws-workspace
```

[Security and catalog disclosure](docs/catalog-policy.md): external native credential ownership, content egress, account trust, filesystem and delivery limits. No catalog acceptance or endorsement is implied.

## Verify

```bash
python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate . --json
```

User testing of the slash command with live Google delivery is pending. [Verification records](docs/verification.md) distinguish earlier release results from this candidate. [Catalog PR #157482](https://github.com/NousResearch/hermes-agent/pull/157482) remains a draft; the [proposed upstream API for agent tools](docs/catalog-policy.md#upstream-follow-up-trusted-origin-for-agent-tools) is separate.
