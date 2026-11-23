# Connect native accounts, then opt in to watches

`gws-workspace` delegates credential storage and refresh to native `gws`. It does not implement OAuth or automatically import global credentials. One Hermes profile belongs to one trusted operator; all authorized sessions in that profile can use its accounts.

## Connect an account

1. Run `hermes gws setup EMAIL` in a terminal for the native gcloud wizard, or provision your own Google Cloud **Desktop OAuth client** and enable the APIs you need: [Google Cloud setup](docs/oauth-setup.md).
2. Run `hermes gws login EMAIL` with the desired scopes. Follow [native login and account selection](docs/login.md) to authorize each account in its explicit profile-scoped native configuration directory. Keep client files and credentials outside the plugin directory, with private permissions. Never paste client JSON or tokens into chat.
3. Start a fresh session to load the updated tool schemas. Use `gws_accounts(check_health=true)` to check native authentication; stored credentials alone do not prove a working session.

Native `gws auth` is available in an operator terminal with the account configuration selected. Agent-facing `gws_api` intentionally refuses auth commands, including credential export. The optional, separately distributed `gws-remote-auth` companion supplies `gws_login` and `gws_logout` for remote-browser authorization; those tools are not part of this public plugin. Stop watches and let remote cleanup finish before replacing or removing credentials. Local logout does not revoke Google consent.

Prefer curated Gmail/Drive/Calendar tools, `gws_schema` for request discovery, and guarded `gws_api` for other native operations. Mutations and local file input/output require Hermes approval.

## Watches are opt-in

Installation does not start a worker, provision cloud resources, or create delivery routes. Follow [watch setup](docs/watch-setup.md) for:

- Operator-provisioned native Hermes webhook routes and exact route attestations.
- A supervised worker with a loopback listener and public HTTPS forwarding of only its Google callback paths.
- Gmail authenticated Pub/Sub PUSH with topic publisher IAM, a dedicated service-account identity and exact OIDC audience; Drive/Calendar channel callbacks.
- In the intended messaging chat, send `/gws-watch help` for start, status and stop commands. In a brand-new chat, send a normal message first. The agent can draft a command but cannot run it for you.

Same-session delivery requires explicit injection permission. Session lookup and injection are not atomic: a concurrent `/new` can still race. HTTP admission or injection acceptance is not proof of delivery. See [compatibility and limitations](docs/compatibility.md) and [catalog notes](docs/catalog-policy.md).
