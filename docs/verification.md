# Verification record

## Shared-source lifecycle regression coverage

The integrated repair passed **247 tests with zero skips** in an isolated home, with the installed Hermes source explicitly selected. Parent verification reproduced eight failing lifecycle regressions against the previous baseline before running the repaired suite. Doctor and Validate passed. Native endpoint configuration now also rejects contradictory fixed destinations and the reserved bridge prefix for new-session routes.

`tests/test_watch_source_lifecycle.py` covers rearming after a matched one-shot, preservation of a collecting sibling's committed/staged cursor, recovery of stranded active watches without a new push, health failures for stopped or missing source links, expiry, and start/drain/remote-cleanup races. Completed watches retain their pending deliveries but no longer own the Google feed.

`tests/test_watch_multisession_regression.py` exercises shared Gmail, Drive and Calendar sources with watches in the same and different sessions, matching/nonmatching filters, multiple principals, and mixed same-session bridge/native new-session delivery. It also exercises the real native webhook admission handler and replay deduplication in an isolated store. These are automated fixture/admission tests, **not proof of completed live model replies in every combination**.

Account lifecycle locking serializes baseline registration with cursor draining. A long drain can cause a new start to hit the existing account-lock timeout and require a retry. Collection failures or outbox backpressure hold a shared page for retry instead of silently dropping it; independent delivery retries do not block sibling destinations. Recovery still depends on the provider retaining the history cursor.

## v0.10.0 — short-command live verification

The draft-confirmation implementation passed **231 tests, zero skipped**, independent parent rerun included, against unchanged upstream Hermes `0f602c5f6f18615632535251d62a2db74ce93b69`. The suite exercises the real plugin manager and gateway command dispatcher, Gmail/Drive/Calendar start–status–stop in both delivery modes, sender/profile/session rejection and simulated injection to the saved session. Google responses and final message injection are test fixtures, not live delivery evidence. The suite also covers local-only preparation, immutable drafts, expiry, wrong-session/actor rejection, replay, concurrent confirmation and shared-Drive source deduplication. Doctor and Validate passed with **16 tools and two hooks**; scanner cautions are `context_exfil` on the skill paragraph instructing the agent to return a confirmation command in the current conversation (not conversation-history export), and `py_read_secrets_file` on the synthetic empty dotenv fixture in `tests/test_account_cli.py`.

### Live short-command tests

On 2026-11-23, runtime commit `f0fb307337a9baab8bda2ec472abff1f420ca852` was tested on Linux with the unpatched host and operator-provisioned Google/Hermes routes. Only release documentation changed afterward. Each watch was prepared conversationally and activated by the user's short confirmation command.

- **Drive / same_session:** a disposable file rename was read back from Google; `renamed`/`updated` metadata reached the initiating Discord conversation and the user confirmed receipt.
- **Gmail / same_session:** an authorized self-addressed test message was read back from Google; its `messageAdded` notification and subject reached the initiating conversation and received an agent acknowledgment.
- **Calendar / new_session:** a disposable event title update was read back from Google. The native webhook admitted a separate agent session, that session completed and generated the expected acknowledgment, and the native delivery obligation reached `delivered` with no recorded error. The destination was the configured Discord conversation; `new_session` means independent agent history, not a new Discord thread. No Discord message permalink or independent visual readback was obtained, so this is runtime delivery evidence, not a claim that the user located the message.
- All three one-shot watches matched and their Google sources stopped. The worker reported zero pending source cleanup; same-session cleanup completed. The new-session binding was retained as designed because the plugin cannot observe final native delivery completion. The host's delivery ledger was checked separately; webhook admission alone was not counted as delivery.
- The installed snapshot matched all 53 tracked source files at the tested runtime revision; its isolated suite also passed **231 tests, zero skips**.

**Coverage limits:** these live tests cover three service/mode combinations, not all six. Manual stop, expiry, long-duration renewal and expired-token recovery were not re-exercised in this run. Automated and historical evidence below remain separately scoped; neither implies exactly-once delivery or catalog admission.

## Historical: v0.9.1 namespace verification

The plugin ID/config namespace is `gws-workspace` and its toolset is `gws_workspace`, avoiding the unrelated existing catalog entry. Repository URLs, native account directories and the backend contract are unchanged. The renamed public suite passed **229 tests, zero skipped**, including rejection of the unrelated `google-workspace` worker configuration; Validate and Doctor passed (18 tools, two hooks), with the existing documented scanner warnings. This namespace check does not repeat or extend the live Google consent/delivery evidence below.

## Historical: v0.9.0 release verification

Runtime tested: `67d7d3bcab717ace38c30025aeef80b95dbb40f3`, native gws 0.22.5, Linux. Only documentation changed between that live test and the v0.9.0 release.

### Authentication and account isolation

- Fresh browser consent completed for one account through `hermes gws login EMAIL --full`, including a manually relayed native loopback callback and successful CLI exit.
- A second account completed fresh consent through the optional `gws-remote-auth` companion.
- Both account slots were healthy simultaneously. Explicit-account Gmail identity reads passed; the personal account also passed Drive and Calendar reads.
- The optional companion is not a dependency of public CLI login or Workspace operations. Its tests are separate from the public release.

### Real Google PUSH → Hermes

An authorized integration test used disposable Gmail, Drive and Calendar objects with a supervised worker, public HTTPS ingress and operator-provisioned Hermes routes:

- Gmail: sent a uniquely tagged message to the same account.
- Drive: renamed a disposable file and read the change back.
- Calendar: changed a disposable event and read the change back.
- Each service was watched in both `same_session` and `new_session` modes. All six matched and produced model acknowledgments.
- All six host delivery obligations reached `delivered` with no recorded error. Three same-session acknowledgments were observed in the originating Discord thread; three new-session acknowledgments completed through the configured native webhook delivery route. Plugin admission alone was not counted as execution.
- Same-session notifications queued behind an active turn; delayed acknowledgment was not treated as a lost event.
- All six watches were explicitly stopped, reached cleanup state `done`, and retained no delivery route bindings. Shared operator infrastructure was preserved.
- After renewed user approval following an approval timeout, the test message and file were trashed and the calendar event deleted. Exact-object readback confirmed `TRASH`, `trashed: true`, and `status: cancelled`. Account credentials were preserved.

### Automated checks

- Public installed suite: **229 tests passed, zero skipped**.
- Remote-auth companion suite: **72 tests passed, zero skipped**, plus the cross-plugin lifecycle check.
- [Public CI at the live-tested runtime](https://github.com/anpicasso/hermes-gws-workspace/actions/runs/44677238577) passed against the workflow's pinned Hermes host and native gws.
- Native dry-run tests may fetch public discovery schemas. A fresh credential-free HOME in a network-disabled sandbox can fail discovery without indicating an API mutation or argv regression; the passing isolated run permitted discovery access while keeping the host filesystem read-only.

## Limits of this evidence

The v0.9.0 live test covered Linux, two account login paths, selected authenticated reads, actual Google changes, both notification modes, approval handling and cleanup. It did not exercise v0.10.0's user-sent slash command or prove Windows support, long-duration lease renewal, expired-token recovery, or exactly-once delivery. The documented concurrent session-reset race and trusted-operator profile boundary remain. Validate/Doctor success and functional tests do not grant catalog admission.

See [compatibility](compatibility.md), [watch setup](watch-setup.md), and [catalog policy](catalog-policy.md). The worker and cloud routes remain operator-provisioned; installation does not start them.
