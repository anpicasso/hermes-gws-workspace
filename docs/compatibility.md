# Compatibility

The unreleased `gws-workspace` v0.10.0 candidate requires no Hermes core patch. Runtime compatibility is not [catalog eligibility](catalog-policy.md). The optional `hermes-gws-remote-auth` companion is a separate installation.

## Pinned baseline

The repository tests/CI pin Hermes commit `f7e2898aaefc8f967dbf7345e3999a65672fe60e` and native `gws` 0.22.5. The `requires_hermes: >=0.23.2` manifest gate is not evidence that every newer host works, nor a claim that this worktree has passed CI.

[The workflow](../.github/workflows/ci.yml) is the reproducible environment specification: Python 3.14 with the pinned Hermes lockfile, PyYAML 6.0.3, aiohttp 3.14.5, `google-auth[requests]>=2.40,<3`, and a checksum-verified native CLI. It runs unittest discovery with isolated homes and no inherited Google credentials, rejects skips, then runs plugin validation and Doctor. Do not duplicate that setup with an unpinned host and call it the same baseline.

## Required host surfaces

- Public tool/hook registration and `ctx.register_command` for `/gws-watch`. The gateway must supply the command's messaging `source` (sender and chat), physical `session_id` and `session_key`; the plugin must be running in its registered profile/home. Missing context refuses watch operations, without a private origin API or environment fallback.
- `pre_tool_call` approval directives and the actual host approval dispatcher.
- Native generic-HMAC webhook ingress provisioned by the operator.
- For same-session delivery: `pre_gateway_dispatch` with a usable session store and public `ctx.inject_message(session_key=...)`, plus explicit `allow_gateway_injection` consent.

There is no private webhook routing dependency or process-global origin fallback. The [route contract](routes-contract.md) records the inspected host seams and limits. A host without trusted physical-session binding cannot safely start watches. A hosted-isolation placeholder session store cannot support the same-session precheck; use `new_session` rather than weakening isolation. Callable injection alone does not prove consent or delivery.

The watch worker and managed native auth writes (setup, login and logout) require POSIX advisory locking; these operations are unsupported on Windows. Use a dependency-complete Python environment and a separate worker for each exact profile home. Non-watch portability is not a claim of tested Windows support.

## Verification scope

From the plugin root, in the workflow's isolated environment:

```bash
python -m unittest discover -s tests -v
hermes plugins validate . --json
hermes plugins doctor . --ci
```

Inspect skips and failures rather than quoting historical passing counts. Tests exercise simulated Google transport, synthetic identities, loopback HTTP, native dry-run behavior and selected real host entry points. They do not establish real Google consent, live authenticated Pub/Sub push, remote receiver configuration, agent execution or external message delivery. Revalidate command dispatch, approval, webhook and injection behavior on upgrades. See the [candidate live-test checklist and historical evidence](verification.md).
