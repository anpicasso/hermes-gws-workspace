# Workspace tools and approvals

The public `gws-workspace` adapter leaves credentials and refresh to native `gws`. Start with [OAuth setup](oauth-setup.md) and [operator provisioning](login.md); see the [catalog disclosure](catalog-policy.md). The separate `hermes-gws-remote-auth` companion is optional, not a public runtime dependency.

One profile is one trusted operator. Every connected Google account belongs to that operator; every session authorized to use this profile can use those accounts. Choose an explicit `account` on every Workspace call. Use separate profiles and access controls for different operators.

## Hybrid surface

Prefer the curated tools for common jobs; use `gws_schema` to discover native request fields, then `gws_api` for operations not covered here. We do not duplicate the entire Google API surface.

- Gmail: `gws_gmail_search` → `gmail users messages list`; `gws_gmail_read` → `gmail users messages get`; `gws_gmail_draft` → `gmail users drafts create`; `gws_gmail_send` → `gmail users messages send`.
- Drive: `gws_drive_search` → `drive files list`; `gws_drive_read` → `drive files get` or `export`; `gws_drive_upload` → `drive files create`; `gws_drive_share` → `drive permissions create`.
- Calendar: `gws_calendar_list` → `calendar events list`; `gws_calendar_freebusy` → `calendar freebusy query`; `gws_calendar_create` → `calendar events insert`; `gws_calendar_update` → `calendar events patch`.
- Discovery: `gws_schema` → `schema <service.resource.method>`; optional `resolve_refs=true`.
- Advanced: `gws_api(account,args)` takes exact CLI tokens, not a shell command.
- Accounts: `gws_accounts` lists provisioned slots; `check_health=true` requests sanitized native status. `check_lifecycle=true` requires one account and no health request; it reports credential use for companion coordination, not permission to remove credentials. A slot name is not Google identity proof.

`gws_watch_prepare` stages a validated local watch-start draft and returns a readable summary plus `/gws-watch confirmar CODE`. It performs no Google I/O, creates no subscription and grants no delivery authority. The user confirms in the same physical conversation; the command obtains trusted sender/session context and runs the existing route checks and start lifecycle. Direct watch activation, status and stop remain [`/gws-watch` commands](watch-setup.md#send-a-watch-command), not agent tools. Expired or consumed codes require a fresh draft.

Schemas reject unknown fields, invalid types, malformed mail headers, and out-of-range requests. Searches return one bounded page; pass the returned page token to continue. IDs are never repaired or guessed.

## Examples

These are independent illustrative tool calls in JSON Lines format, not operations performed during installation.

```jsonl
{"tool":"gws_gmail_search","arguments":{"account":"me@example.com","query":"from:client@example.com newer_than:7d","max_results":10}}
{"tool":"gws_gmail_read","arguments":{"account":"me@example.com","message_id":"MESSAGE_ID","format":"metadata","metadata_headers":["From","Subject"]}}
{"tool":"gws_gmail_draft","arguments":{"account":"me@example.com","to":["client@example.com"],"subject":"Project update","body":"The draft is ready for review."}}
{"tool":"gws_drive_search","arguments":{"account":"me@example.com","query":"trashed = false and name contains 'Budget'","page_size":20}}
{"tool":"gws_drive_read","arguments":{"account":"me@example.com","file_id":"FILE_ID","mode":"export","mime_type":"application/pdf","output_path":"exports/budget.pdf"}}
{"tool":"gws_drive_upload","arguments":{"account":"me@example.com","path":"reports/update.pdf","parent_id":"FOLDER_ID"}}
{"tool":"gws_drive_share","arguments":{"account":"me@example.com","file_id":"FILE_ID","type":"user","role":"reader","email_address":"client@example.com","send_notification":true}}
{"tool":"gws_calendar_freebusy","arguments":{"account":"me@example.com","time_min":"2026-12-01T09:00:00-05:00","time_max":"2026-12-01T17:00:00-05:00","calendar_ids":["primary"]}}
{"tool":"gws_calendar_create","arguments":{"account":"me@example.com","summary":"Project review","start":"2026-12-01T10:00:00-05:00","end":"2026-12-01T10:30:00-05:00","send_updates":"none"}}
{"tool":"gws_schema","arguments":{"account":"me@example.com","method":"drive.files.update"}}
```

Mail is plain text without attachments. Recipients must be plain email addresses, not display-name syntax. Reply threading accepts `thread_id` and `in_reply_to`; the latter sets both In-Reply-To and References. Calendar start/end must both be dates or both zoned timestamps; all-day end is exclusive. Calendar notifications default to `none`; request `send_updates="all"` explicitly when invitations should be sent. Drive user/group sharing defaults to notifying recipients.

Drive binary content/export requires a relative `output_path`; upload uses a relative `path`. Paths cannot contain traversal components or start with `/` or `-`. They resolve inside the private `<HERMES_HOME>/gws-oauth/files/` directory (mode 0700), never the gateway's working directory. Ensure output parent directories exist there; put upload sources there first. Local file operations require approval. Shared-drive searches expose Google's `incompleteSearch` indicator; narrow the search if it is true.

## Approval policy

The same `pre_tool_call` hook classifies generic and curated calls before execution:

- Known reads and schema/help discovery run without this additional gate.
- Mutations (including drafts), deletes, sends, sharing, uploads and local output files request **native Hermes approval**. Unknown methods/options/helpers and most request bodies also request approval. Calendar free/busy is a known read-only POST exception.
- The native `auth` namespace is blocked through the agent-facing executor. Use the [profile-scoped `hermes gws` terminal commands](login.md) for setup, login and logout, or `gws_accounts(check_health=true)` for sanitized status. Public tools do not include `gws_login` or `gws_logout`; those belong to the optional companion.
- Model arguments such as `approved=true` cannot grant approval. Host approval mode, existing operator grants and frontend approval support determine the result; this is not an independent per-call confirmation system or an OS sandbox.

Curated approval previews always show recipients, attendees, share targets and paths in full; only body/description/message text is shortened. Generic `gws_api` previews are bounded and marked when truncated; inspect the full intended operation before approving a long generic request. A successful remote mutation followed by a local output/transport failure is not safe to blindly retry: read back the remote object first.

The operator selects native login scopes; the public plugin does not choose a login preset or widen grants. The OAuth client, consent screen, Google policy and actual granted scopes govern access. `gws_accounts(check_health=true)` reports sanitized granted scopes, never credentials.
