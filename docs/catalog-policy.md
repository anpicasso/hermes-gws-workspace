# Catalog policy disclosure

## Distribution and credential ownership

`gws-workspace` v0.10.0 is distributed independently through its [release](https://github.com/anpicasso/hermes-gws-workspace/releases/tag/v0.10.0). [Catalog PR #157482](https://github.com/NousResearch/hermes-agent/pull/157482) requests maintainer review following the [live tests](verification.md). Validation checks do not establish compliance with the [catalog admission rules](https://github.com/NousResearch/hermes-agent/blob/05348a3094a2356b9a271a2f22cdd6711c9952b8/plugin-catalog/README.md) or imply Nous Research endorsement.

Native `gws`, not Python or Hermes' credential broker, owns Google credential storage, decryption and refresh. The public adapter selects an explicitly provisioned account directory and invokes short, locked native commands. It does not export OAuth credentials, refresh access tokens in Python, implement login, or register `gws_login`/`gws_logout`. The optional, separately distributed `hermes-gws-remote-auth` companion (`gws-remote-auth` plugin) is separately installed and is not required for public operation.

This still differs from rule 11's Hermes-managed `oauth:` declaration, `hermes auth add <plugin>:<id>` and `credentialed_provider_request` path. Native credentials remain outside Hermes `auth.json`; native Workspace commands attach authentication themselves. Removing Python token handling does not make external credential ownership automatically catalog-compliant. A supported policy exception or redesign remains a maintainer decision, not a validator result.

## Public host surfaces and operator responsibility

The plugin registers `/gws-watch` with `ctx.register_command` and reads origin only from host command metadata in the registered profile/home. It removes the three agent watch activation/management tools and their private session/delegation imports; no core patch is required. A local-only `gws_watch_prepare` tool creates an expiring draft; the user activates it with `/gws-watch confirmar CODE`. Preparation does not contact Google or authorize delivery. Tool approval and `ctx.inject_message` are unchanged. Operators provision routes through public Hermes commands and configure exact route attestations; see [the route contract](routes-contract.md).

The attestation is not remote readback: direct receiver edits are undetectable if the plugin configuration is unchanged. Stop watches before changing receiver destinations, prompts, skills, signature mode or secrets, and verify the receiver through its public CLI. Config digest checks prevent silent retargeting through changed plugin configuration, not malicious operator behavior.

### Upstream follow-up: trusted origin for agent tools

To support direct agent-initiated activation with the same origin guarantees, the proposed approach gives Hermes a public, host-issued origin for the **current tool call**: physical session ID and route key, profile/home, current actor, platform, chat and optional thread. It should return no usable origin for unbound, mixed-actor, delegated or non-messaging calls, even when they inherit a parent route. A session ID or a process environment value alone is not proof. This proposal is separate from the catalog review and is not a dependency of the slash command.

Upstream API request: [#159074](https://github.com/NousResearch/hermes-agent/issues/159074). Proposed implementation: [#159075](https://github.com/NousResearch/hermes-agent/pull/159075). These explicitly acknowledge the related admitted-origin proposal [#142762](https://github.com/NousResearch/hermes-agent/pull/142762); the plugin needs one supported contract, not competing APIs. Neither proposal is a dependency of this release.

## Approval and trust boundaries

- One profile represents one trusted operator. Authorized sessions can use all its accounts; account slots are not tenants or isolation from other processes under the same OS user.
- Mutations, sends/shares, uploads, local file input/output and unknown native operations request Hermes approval. The host's approval mode and grants still apply. Model-supplied approval fields cannot authorize calls.
- Sending a draft confirmation or raw `start` slash command authorizes bounded background subscription operations, renewal, reads and cleanup. Watch commands do not go through the agent's `pre_tool_call` approval path. The worker does not ask on each renewal.
- Watch commands require an identified sender in an existing messaging session and the registered profile/home. On the WhatsApp bridge only explicitly non-shared DMs are supported. Request JSON cannot override the origin. This does not turn a shared operator profile into isolated tenants or defend against another trusted plugin forging host context.
- Same-session delivery retains a concurrent `/new` race: checking the physical session and admitting injection are not atomic. HTTP admission or injection acceptance is not execution or external delivery confirmation; no exactly-once guarantee is claimed.

## Operational and privacy disclosures

- **Network:** native `gws` contacts Google Workspace, discovery and OAuth services. Gmail receives authenticated Pub/Sub HTTPS PUSH; official `google-auth` verifies Google OIDC signatures/claims using public certificates. This verification is not mailbox OAuth or ADC. The worker forwards signed notifications to configured Hermes endpoints. The operator provisions public HTTPS ingress, cloud resources and IAM.
- **Content:** successful tool output enters agent context and may reach the model provider. Watches read bounded metadata locally and forward matching metadata to the configured conversation. Treat names, subjects and event fields as untrusted external content, not instructions. The plugin implements no independent analytics; upstream binaries/services have their own policies.
- **Storage:** `$HERMES_HOME/gws-oauth/` retains the legacy name for native account stores, locks, private working files and large response files. `$HERMES_HOME/plugin-data/gws-oauth/` holds watch SQLite state, temporary draft specifications, route-binding metadata and worker locks. Route secrets belong in protected operator configuration, not the installed plugin snapshot or chat. The plugin does not write the native Hermes route store.
- **Execution:** the separately installed `gws` binary runs without a shell. Watches require a separately supervised POSIX worker with write access to the selected profile's account cache and watch state. Installation does not start it, bundle a native binary, provision cloud resources or configure routes.
- **Files:** curated file paths are constrained relative paths; generic native file arguments remain subject to approvals and OS permissions. The private working directory is not a filesystem sandbox. Large response files require explicit cleanup.
- **Lifecycle:** stop watches and finish remote cleanup before manually changing credentials. Independent native commands do not share plugin locks. Native logout is local, not Google consent revocation. Removal does not delete credentials, response files, watch state, Pub/Sub resources or operator Hermes routes.
- **Coverage:** push can be delayed or lost, upstream changes can coalesce, expired cursors leave gaps, and bounded metadata can miss details. Retained one-shot route state means local delivery evidence, not plugin ownership of the operator route.

See [native auth](login.md), [tool policy](api-tools.md), [watch setup](watch-setup.md) and [compatibility](compatibility.md). Offline tests are not live OAuth, Google push or Hermes end-to-end delivery evidence.
