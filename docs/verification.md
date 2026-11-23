# Verification record

## v0.10.0 candidate — unreleased, user live test pending

The candidate passed **226 tests, zero skipped**, against unchanged upstream Hermes `0f602c5f6f18615632535251d62a2db74ce93b69`. The suite exercises the real plugin manager and gateway command dispatcher, Gmail/Drive/Calendar start–status–stop in both delivery modes, sender/profile/session rejection and simulated injection to the saved session. Google responses and final message injection are test fixtures, not live delivery evidence. Doctor and Validate passed with **15 tools and two hooks**; the only scanner caution is the synthetic empty dotenv fixture in `tests/test_account_cli.py`.

The slash command changes how a watch obtains its origin. No live Google or user-chat result for that change is recorded here. Before release, use an existing messaging chat to send `start`, confirm the returned ID with `status`, trigger an authorized disposable change and observe delivery in the intended session. Send `stop` for that ID and verify cleanup. Check both delivery modes; do not count admission alone as delivery.

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

The v0.9.0 live test covered Linux, two account login paths, selected authenticated reads, actual Google changes, both notification modes, approval handling and cleanup. It did not exercise the candidate's user-sent slash command or prove Windows support, long-duration lease renewal, expired-token recovery, or exactly-once delivery. The documented concurrent session-reset race and trusted-operator profile boundary remain. Validate/Doctor success and functional tests do not grant catalog admission.

See [compatibility](compatibility.md), [watch setup](watch-setup.md), and [catalog policy](catalog-policy.md). The worker and cloud routes remain operator-provisioned; installation does not start them.
