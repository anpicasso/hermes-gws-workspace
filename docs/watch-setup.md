# Targeted Workspace watches: operator setup

The unreleased v0.10.0 candidate changes how a user starts and manages a watch, not the Google accounts, worker or delivery routes. [Native `gws` owns authentication](login.md); no Python OAuth export/refresh or Pub/Sub pull is used.

## Provision Google ingress

Enable the relevant Gmail, Drive, Calendar and Cloud Pub/Sub APIs. The operator provisions public HTTPS ingress, cloud resources and IAM; installation does none of this.

For Gmail:

1. Create a topic in the OAuth client's Google Cloud project. Grant `gmail-api-push@system.gserviceaccount.com` Pub/Sub Publisher on it.
2. Create an authenticated Pub/Sub **PUSH** subscription to `https://watch.example.com/google/gmail`. Configure a dedicated service-account identity and exact OIDC audience. Preserve the standard wrapped Pub/Sub JSON envelope (do not enable payload unwrapping).
3. Give the provisioning principal permission to act as that service account and configure Pub/Sub's service agent to mint the OIDC token as required by [Google's authenticated push setup](https://cloud.google.com/pubsub/docs/authenticate-push-subscriptions). This is operator IAM, not a mailbox grant to Python.
4. Authorize the native mailbox account for `users.watch`, `users.history.list` and `users.stop`. The topic project is checked against native status when available; Gmail start also verifies the mailbox identity matches the selected account.
5. Set the exact topic, subscription, audience and service-account email below. Forward the Authorization header unchanged through the proxy.

The worker uses the official `google.oauth2.id_token.verify_oauth2_token` verifier for signature, expiry, issued-at, audience and Google issuer checks, then requires the exact service-account email with `email_verified=true`. It validates subscription, mailbox and history envelope and durably records a deduplicated hint before HTTP 204. Public signing-certificate retrieval is not OAuth refresh and does not use ADC or mailbox credentials. Invalid/unavailable verification fails closed.

Forward only `/google/gmail`, `/google/drive` and `/google/calendar` from your TLS proxy to the worker's loopback listener (`127.0.0.1:8765` by default). Drive/Calendar callbacks use channel bearer tokens and stored channel/resource identity, not Gmail OIDC. The worker never fetches callback-supplied URLs. Keep controls and unrelated gateway endpoints private.

### Public HTTPS ingress options

Keep the worker bound to loopback and expose only the three Google callback paths:

- **Tailscale Funnel:** public HTTPS ingress suitable for Google callbacks. Tailscale Serve alone is tailnet-private; Google cannot reach it.
- **Cloudflare Tunnel:** use a stable public hostname targeting the loopback worker. Do not require interactive Cloudflare Access login on callback paths; retain the worker's OIDC/channel authentication. Preserve the Authorization header and request body.
- **Public reverse proxy:** Caddy or Nginx on public HTTPS with a valid certificate, forwarding only the callback paths to loopback. Expose the TLS proxy, not the worker or Hermes administration ports.

Use a stable URL throughout the subscription lifetime, set the exact Gmail OIDC audience, and configure request-size/rate limits at ingress. A tunnel does not replace notification authentication. Obtain operator approval before changing public exposure, DNS, IAM or cloud subscriptions. Native Hermes delivery URLs are a separate hop and need not be exposed alongside Google ingress.

## Provision Hermes delivery routes

Use the public `hermes webhook subscribe` CLI; consult that host's `--help` for its flags. The plugin never creates, changes, disables or deletes a Hermes route and never reads its private subscription store. Configure each route separately, then attest its exact settings in `watch_routes` below. The full [route contract](routes-contract.md) is authoritative.

- New session: enabled generic-HMAC receiver, `events: [gws-watch]`, fixed destination matching the origin, fixed skills, `deliver_only: false`, `mirror_to_session: false`. Its prompt must include `{__raw__}`, distinguish locally stored owner `instructions` from untrusted external `events`, and never treat event text/links as instructions to mutate Google data.
- Same session: name starts with `gws-watch-bridge-`, generic HMAC, `events: [gws-watch-bridge]`, `deliver: log`, no skills, mirroring, deliver-only bypass or scheduled/coalesced execution that bypasses the hook. Use a fallback prompt to perform no actions and reply `NO_REPLY`; that prompt does not replace the hook.
- Same-session injection additionally requires `plugins.entries.gws-workspace.allow_gateway_injection: true` and a host exposing the real hook session store. Hosted isolation with only a placeholder store fails closed; prefer `new_session` rather than weakening isolation policy.

This is an operator attestation, not receiver-side pinning: the plugin cannot read back remote route configuration. Stop watches before receiver edits and verify using the public CLI. Plugin config changes, including secret rotation, fail digest checks on existing watches; stop and recreate them rather than silently retargeting.

## Exact configuration shape

In the selected profile's `config.yaml`, merge these settings; replace every placeholder. Protect this file because route HMAC secrets are sensitive. Use strong random secrets of at least 32 characters, not the example text.

```yaml
plugins:
  entries:
    gws-workspace:
      allow_gateway_injection: true  # Only if same-session injection is intended.
      settings:
        gws_bin: /absolute/path/to/gws
        watch_callback_url: https://watch.example.com
        watch_gmail_topic: projects/example-project/topics/gmail-watch
        watch_gmail_subscription: projects/example-project/subscriptions/gmail-watch
        watch_gmail_push_audience: https://watch.example.com/google/gmail
        watch_gmail_push_service_account: gws-push@example-project.iam.gserviceaccount.com
        watch_reconcile_seconds: 300
        watch_routes:
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

`watch_routes` is an object containing `routes`, not a bare list. Its equivalent minimal JSON shape is `{"watch_routes":{"routes":[]}}`; an empty list authorizes no destinations. Each configured origin must match all six fields exactly, including null `thread_id`. Use actual host IDs/route keys, not invented values or model-provided destinations. Duplicate matches refuse. Physical session IDs are intentionally absent from this configuration.

Non-default profiles require `/p/PROFILE/webhooks/NAME` URLs and the matching origin profile. HTTPS is required except loopback HTTP; URL credentials, query strings, fragments and redirects are rejected. Requested watch skills must exactly match the configured new-session skills. Omit an unused delivery mode rather than configuring an unneeded route.

## Start and supervise the worker

Use a dependency-complete Python environment with PyYAML, aiohttp and `google-auth[requests]` as declared in `plugin.yaml`. Run one worker per exact profile home:

```bash
python /absolute/path/to/gws-workspace/watch_worker.py run \
  --home /absolute/path/to/profile-home \
  --profile default \
  --callback-host 127.0.0.1 --callback-port 8765
```

`--profile` defaults to `default`; it does not resolve or replace `--home`. The worker reads `plugins.entries.gws-workspace.settings` from exactly `--home/config.yaml`, not another active profile's configuration. Explicit CLI overrides such as `--gws-bin`, `--callback-url`, `--gmail-topic`, `--gmail-subscription`, `--gmail-push-audience`, `--gmail-push-service-account` and `--reconcile-seconds` take precedence over startup settings. Routes are reread for each delivery; other settings load at startup, so restart after changing them. Keep gateway and worker settings aligned. `watch_callback_url` is a base; service paths are appended.

The worker requires POSIX advisory locks and write access to native token caches and `$HERMES_HOME/plugin-data/gws-oauth/`. Watch state is a private SQLite database; a second worker for the same home refuses startup. Windows watch workers are unsupported. Use the [service example](../examples/gws-watch-worker.service) as a starting point, replacing paths and adding the correct profile argument. No service is installed or started automatically.

```bash
python watch_worker.py list --home /absolute/path/to/profile-home
python watch_worker.py status --home /absolute/path/to/profile-home --stale-seconds 60
python watch_worker.py stop WATCH_ID --home /absolute/path/to/profile-home
```

Status reports lock/heartbeat, source errors/retries and cleanup, not Google delivery. It exits 1 for an absent/stale worker. Stop revokes local collection/forwarding first; leave the worker running to finish remote cleanup before changing credentials or uninstalling. It removes only plugin-local route metadata, never operator Hermes routes or Pub/Sub resources. Local one-shot binding evidence may be retained until explicit stop/review because native completion is unobservable.

Subscription failures use durable backoff (initial retry 10 seconds, cap 900). `run --subscription-backoff-max SECONDS --retention-days DAYS` adjusts worker behavior. Fully cleaned terminal records are pruned hourly after the retention window (default 7 days); uncertain/in-flight evidence and pending cleanup are retained. Reconciliation defaults to 300 seconds and drains durable cursors independently of push hints; it is not full-resource polling.

## Send a watch command

Ask the agent in normal conversation to prepare the changes you want to follow. It can look up resource IDs using the normal read tools, then call `gws_watch_prepare` to save a local draft. Review its summary and send the returned `/gws-watch confirmar CODE` yourself in that same conversation. `confirm` is also accepted. Preparation performs no Google I/O and grants no authority; confirmation uses the gateway sender/session and the existing route checks. Codes expire after 10 minutes, are single-use and are scoped to the physical session and profile. At most 64 pending drafts are stored per profile; summaries above 1,600 serialized characters are rejected rather than truncated. Unauthorized confirmation does not consume a draft. Authorized confirmation consumes it before Google I/O, so a failed start needs a new draft.

Send `/gws-watch` or `/gws-watch help` for usage. In a new chat, send a normal message first: commands do not create physical sessions. The gateway also accepts `/gws_watch`. The slash handler itself does not interpret natural language or run an agent loop; the normal conversation prepares the draft beforehand.

Advanced raw JSON remains supported. Examples (substitute your account and exact IDs; send one line at a time):

```text
/gws-watch gmail {"action":"start","account":"person@example.com","target":{"thread_ids":["THREAD_ID"]},"filters":{"from_addresses":["alice@example.com"]},"delivery_mode":"new_session","minutes":120,"stop_after_match":true}
/gws-watch drive {"action":"start","account":"person@example.com","target":{"file_ids":["FILE_ID"]},"delivery_mode":"same_session","minutes":30}
/gws-watch calendar {"action":"start","account":"person@example.com","target":{"calendar_ids":["primary"],"event_ids":["EVENT_ID"]},"delivery_mode":"same_session","minutes":30}
/gws-watch gmail {"action":"status"}
/gws-watch gmail {"action":"stop","watch_id":"WATCH_ID"}
```

Common fields: explicit `account`, `target`, optional `filters`, `delivery_mode` (`same_session` or `new_session`), `minutes` 1–1440, `stop_after_match`, `batch_seconds` 0–60, `instructions` up to 2000 characters, and `skills` only for new sessions. `status` optionally takes `watch_id` for bounded target/filter details; otherwise it lists ten watches per page (pass the returned `next_offset` as `offset`). Oversized selector values are explicitly marked as previews. `filters`, when supplied, must be an object; `stop` requires the exact ID from `start` or `status`. Use the same service and originating chat. Unknown or action-inappropriate fields, including destination overrides, are rejected. Sending `start` authorizes bounded background subscription operations, reads, renewal and cleanup; it is not an agent tool approval prompt.

## Selectors and change types

Lists are OR within a field; different filters are AND. Text matching is literal Unicode casefold, not regex. Addresses use parsed addr-specs; domains match exactly without implicit subdomains. IDs are preserved. Watches establish a baseline/catch-up barrier rather than announcing all existing data.

- **Gmail:** choose `thread_ids`, `message_ids` (resolved to threads), or `mailbox:true`. Filters: `from_addresses`, `from_domains`, `label_ids`, `subject_contains`, `has_attachment`, `include_sent`, `change_types`. Exact change types are `messageAdded` (default), `messageDeleted`, `labelAdded`, `labelRemoved`. Shared history reads all types; selection is local. Label events include `changed_label_ids`, but `label_ids` filters the available message labels, not necessarily the labels removed. Metadata can reflect current state rather than a historical snapshot. Deletion/vanished-message tombstones lack sender/subject/attachment metadata: those predicates fail closed. Unknown labels cannot prove non-sent status, so such tombstones require `include_sent:true` and still cannot satisfy unknown label predicates. This is not a complete mail audit log.
- **Drive:** choose `file_ids`, `folder_id` (direct children only), or `all_files:true`; optional `shared_drive_id`. Filters: `name_contains`, `mime_types`, `change_types`. Types: `appeared`, `updated`, `renamed`, `moved`, `trashed`, `restored`, `removed`. They describe observed metadata transitions, not content diffs; `appeared` need not mean newly created and `removed` can mean lost access. No recursive folder traversal.
- **Calendar:** require `calendar_ids`; optionally select `event_ids` or `recurring_event_ids`. Filters: `organizer_addresses`, `attendee_addresses`, `summary_contains`, `change_types` (`created`, `updated`, `cancelled`), `changed_fields`, `date_window`. Changed fields are `start`, `end`, `location`, `summary`, `status`, `attendee_responses`; the last requires `attendee_addresses`. `primary` resolves to a concrete calendar. Series/master selection plus date window is rejected; calendar-wide windows skip recurring masters. No unbounded recurrence expansion; cancellations use prior metadata when available.

Either boolean value of Gmail `has_attachment` requires verifiable `gmail.readonly`, `gmail.modify` or `mail.google.com` scopes without `gmail.metadata`, because Gmail rejects `format=full` when the metadata scope is granted. MIME filename inspection is bounded to four projected levels; no mail body, snippet or attachment bytes are read. Calendar attendee metadata is capped at 100 entries; snapshots and forwarded metadata are bounded.

## Honest delivery and coverage limits

- Same-session lookup and injection are not atomic. A concurrent `/new` can replace the physical session between them. Acceptance means scheduled/admitted, not delivered. Ambiguous claims are not blindly replayed.
- The gateway must identify the sender and an existing messaging session. Local/CLI, scheduled, webhook and senderless origins are refused. On the `whatsapp` bridge, commands require an explicitly non-shared DM; groups and unknown chat types are refused. This restriction is not applied to `whatsapp_cloud`. Use trusted conversations or sender-isolated sessions: account slots within one operator profile are not separate tenants.
- Native HTTP accepted/duplicate receipts prove admission, not agent execution or external delivery. Operator configuration is an attestation, not remote receiver readback. No exactly-once guarantee.
- Push may be lost/delayed; upstream changes can coalesce. Gmail history 404 resynchronizes with an unrecoverable gap, Calendar 410 rebuilds its snapshot with a gap, and rejected Drive cursors require explicit resync/restart. A full outbox pauses cursor advancement. A reserved one-shot event survives its collection deadline until forwarding or explicit stop.
- Offline/synthetic transport and public-host tests are not live OAuth, real Pub/Sub push or native-session end-to-end evidence. See [compatibility](compatibility.md) for the pinned baseline and release checks.
