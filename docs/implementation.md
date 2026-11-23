# Implementation and verification

## Public adapter boundary

In the unreleased v0.10.0 candidate, `__init__.py` registers 15 Workspace tools and the local `gws_watch_prepare` draft tool, `/gws-watch`, and the existing approval and gateway dispatch hooks. No agent watch activation or login/logout tools are registered. The optional `hermes-gws-remote-auth` companion is installed separately.

`router.py` selects `$HERMES_HOME/gws-oauth/accounts/<account>/` through `GOOGLE_WORKSPACE_CLI_CONFIG_DIR`, sets the file keyring backend and invokes `[gws_bin, *args]` without a shell. Account paths/files reject unsafe links. A filtered subprocess environment, isolated native HOME and empty dotenv sentinel prevent accidental adoption of inherited tokens, global ADC or ancestor dotenv credentials. Python inspects account-file metadata; native `gws` owns credential contents, decryption and refresh.

Native subprocesses serialize per account; unrelated accounts can run concurrently. `account_backend.py` exposes a registered profile-bound `gws-native-account-v2` instance to the local CLI and optional in-process companion. Native setup/login holds the watch lifecycle lock for the full operation, drains existing execution under the native lock, then creates a nonsecret `.hermes-unverified` routing marker before launch. Account lookup (including the worker) rejects the marker. Only a verified identity clears it, after the companion's cancellation/deadline check. Native setup can itself offer login, so it uses the same fence. Logout waits for exclusive access, calls native logout and checks that credential/cache files are gone. No SQLite transaction spans consent; the lifecycle query is also exposed by `gws_accounts(check_lifecycle=true)` and is not permission to remove credentials.

Existing managed paths are unchanged. There is no Python client parsing/copying, staging, rollback or credential-directory swap. Native writes may survive failure, but routing stays blocked. Native login does not clear old API caches or a plaintext fallback; before replacing such a store, require explicit native logout. The file-keyring and fresh child HOME apply to setup too: the user must satisfy native gcloud/Cloud Console prerequisites. Stop old-version workers and pending attempts before enabling the coordinated pair. Unmanaged native commands do not participate in plugin locks.

`gws_accounts(check_health=true)` returns sanitized native status/scopes. A stored slot or valid credential is not proof that its name matches the Google identity; the operator verifies identity during [provisioning](login.md). Failures use fixed classifications and guidance rather than returning raw native stdout/stderr. Authentication failures direct the operator to native auth, not a public `gws_login` tool.

Curated builders and the generic executor share `api_policy.py` and Hermes' approval gate. Agent-facing native auth is blocked. Successful output may contain private Workspace data. Output above 200,000 characters is saved under a private `response-*` directory with an `output_file` result; subprocess output is still buffered, not streamed. Clean these files explicitly. A local output failure after a remote mutation is not permission to retry blindly: read back first.

## Watches and routing

- `watch.py` owns strict validation, shared sources/cursors, fingerprints, watch lifecycle, hints and durable outbox. State lives at `$HERMES_HOME/plugin-data/gws-oauth/watch.sqlite3` with a private parent directory.
- `gws_watch_prepare` validates and stores an immutable local start draft without Google I/O. `/gws-watch confirmar CODE` resolves that expiring, single-use draft in the same physical session/profile, then reuses `watch_tools.handle` for activation. Raw service-plus-JSON start/status/stop remains supported. `session_origin.py` derives the sender and conversation from host command kwargs, scoped to the registered profile/home. There is no tool shortcut, private origin import, ContextVar reader or binding cache.
- `watch_gmail.py` reads Gmail history, filters explicit change types locally and handles deletion tombstones without inventing missing metadata. History expiry resynchronizes the cursor and records an unrecoverable gap.
- `watch_pubsub.py` accepts authenticated HTTPS PUSH envelopes. Official `google.oauth2.id_token.verify_oauth2_token` verifies the Google token; additional checks require the exact audience, verified service-account email, subscription and watched mailbox. The hint is durable before HTTP 204. No OAuth export, Python refresh, Pub/Sub pull or ADC is involved.
- `watch_drive.py` classifies bounded before/after metadata, including direct-folder membership. `watch_calendar.py` tracks sync tokens and bounded event metadata, not an unbounded recurrence expansion. See [selector limitations](watch-setup.md#selectors-and-change-types).
- `watch_routes.py` maps an exact trusted origin to an operator-provisioned route. It stores only a config digest and non-secret per-watch metadata, not a native route. Sends use HMAC v2 over timestamp/body, stable request IDs, bounded receipts and no redirects. Removal forgets plugin metadata only; no private webhook imports or automatic route creation exist.
- `watch_worker.py` is an explicitly supervised process. It owns Google subscription renewal/cleanup, loopback callback ingress, reconciliation, retries and forwarding, never starts during plugin registration or Doctor, and takes one POSIX worker lock per home.

The worker's `--profile` defaults to `default`; `--home` identifies the exact home whose `config.yaml` supplies `plugins.entries.gws-workspace.settings`. Explicit CLI values override startup settings. Route configuration is reread for each delivery; other configuration is loaded at startup and requires restart after edits. The gateway and worker must use the same home, profile and route contract. See [operator setup](watch-setup.md) for the exact `watch_routes: {routes: [...]}` shape.

A watch binds the selected route configuration, including its secret, by digest. A changed/disabled/missing configuration refuses delivery rather than retargeting. Stop and recreate watches after route changes. This checks the operator's attestation, not the remote receiver: the plugin has no public receiver readback API.

One-shot matching reserves one outbox event transactionally. Shared-source cursors advance after durable fan-out; backpressure pauses advancement rather than discarding matches. Google resources stop only when no active consumer needs them. Native delivery attempts can retain local route-binding evidence until explicit stop/review because completion is unobservable. Operator Hermes routes and Pub/Sub resources always remain operator-owned.

## Delivery limits

`new_session` uses an existing native route with a fixed destination, skills and prompt. HTTP accepted/duplicate receipts prove admission only. The same-session bridge verifies authenticated native webhook source/profile, durable watch/generation/outbox and current physical session before claiming and calling public injection. False/exception after a claim becomes uncertain and is not blindly replayed.

The host lacks an atomic expected-physical-session fence: `/new` between lookup and injection can target a replacement session. The command requires an identified sender; a profile still represents one trusted operator, and remote route configuration remains an operator attestation. These are residual limits, not race-free delivery, hostile-plugin isolation or exactly-once guarantees. The [route contract](routes-contract.md) is the detailed reference.

## Verification

Use [compatibility](compatibility.md) and the pinned CI workflow for reproducible checks. Offline tests cover transport/state-machine behavior and selected public host seams; they do not establish live Google OAuth, authenticated cloud push or native-session end-to-end delivery. No passing count or live verification claim is implied by this document.
