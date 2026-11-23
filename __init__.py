"""Profile-scoped Workspace tools. Native gws owns credentials and refresh."""
import asyncio
import json
from pathlib import Path

from .router import _ACCOUNT, account_health, bounded_gws, execute_gws, list_accounts

# One instance per Hermes namespaced package; absent until registration succeeds.
native_accounts = None


class _PluginClient:
    """Bound native gws transport for the three watch services."""

    def __init__(self, home, gws_bin):
        self.root = home / "gws-oauth"  # Preserve existing account slots during migration.
        self.gws_bin = gws_bin

    def call(self, account, args, params=None, body=None, timeout=30):
        argv = list(args)
        if params is not None:
            argv += ["--params", json.dumps(params, separators=(",", ":"), sort_keys=True)]
        if body is not None:
            argv += ["--json", json.dumps(body, separators=(",", ":"), sort_keys=True)]
        return bounded_gws(self.root, account, argv, gws_bin=self.gws_bin,
                           timeout=timeout, max_output=2_000_000)

    def scopes(self, account):
        result = bounded_gws(self.root, account, ["auth", "status"], gws_bin=self.gws_bin,
                             timeout=10, max_output=200_000)
        if not result.get("ok") or not isinstance(result.get("data"), dict):
            return None
        scopes = result["data"].get("scopes")
        return [str(scope) for scope in scopes] if isinstance(scopes, list) else None

    def project(self, account):
        result = bounded_gws(self.root, account, ["auth", "status"], gws_bin=self.gws_bin,
                             timeout=10, max_output=200_000)
        if not result.get("ok") or not isinstance(result.get("data"), dict):
            return None
        project = result["data"].get("project_id") or result["data"].get("project")
        return project if isinstance(project, str) and project else None


def register(ctx):
    global native_accounts
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    from .account_backend import AccountBackend
    from . import watch as W
    from . import watch_routes, watch_tools
    from .session_origin import command_origin
    from .workspace_tools import SCHEMAS, build_args
    from .api_policy import make_pre_tool_call_hook

    home = get_hermes_home().resolve()
    profile = getattr(ctx, "profile_name", None) or "default"

    def root():
        if get_hermes_home().resolve() != home:
            raise W.WatchError("profile_mismatch", "Workspace runtime belongs to another profile")
        return home / "gws-oauth"

    def safe(handler):
        def wrapped(args, **kwargs):
            try:
                root()
                if not isinstance(args, dict):
                    raise W.WatchError("invalid_arguments", "Arguments must be an object")
                return json.dumps(handler(args, **kwargs))
            except W.WatchError as exc:
                return json.dumps({"ok": False, "error_code": exc.code, "error": exc.message})
            except ValueError:
                return json.dumps({"ok": False, "error_code": "invalid_arguments",
                                   "error": "Invalid Workspace arguments"})
            except Exception:
                return json.dumps({"ok": False, "error_code": "local_error",
                                   "error": "Workspace operation could not complete safely"})
        return wrapped

    class Runtime:
        def __init__(self):
            self.ctx = ctx
            self._store = None

        @property
        def store(self):
            root()
            if self._store is None:
                self._store = W.WatchStore(home)
            return self._store

        @property
        def client(self):
            root()
            return _PluginClient(home, ctx.get_config("gws_bin", default="gws"))

        @property
        def routes(self):
            return watch_routes.RouteManager(ctx, store=self.store, profile=profile,
                config=lambda: ctx.get_config("watch_routes", default={"routes": []}))

        @property
        def config(self):
            keys = ("watch_gmail_topic", "watch_gmail_subscription", "watch_callback_url",
                    "watch_reconcile_seconds", "watch_gmail_push_audience",
                    "watch_gmail_push_service_account")
            return {key: ctx.get_config(key, default=300 if key == "watch_reconcile_seconds" else "")
                    for key in keys}

    runtime = Runtime()
    ctx.register_skill("workspace", Path(__file__).parent / "skills/workspace/SKILL.md")

    ctx.register_hook("pre_tool_call", make_pre_tool_call_hook(build_args, SCHEMAS))

    def accounts(args, **kwargs):
        if set(args) - {"account", "check_health", "check_lifecycle"}:
            raise ValueError("Unknown account option")
        for key in ("check_health", "check_lifecycle"):
            if key in args and not isinstance(args[key], bool):
                raise ValueError("Invalid account option")
        account = args.get("account")
        if account is not None and (not isinstance(account, str) or not _ACCOUNT.fullmatch(account)):
            raise ValueError("Invalid account")
        if args.get("check_lifecycle"):
            if not account or args.get("check_health"):
                raise ValueError("Lifecycle check requires one account and no health request")
            account = account.lower()
            # The account backend holds lifecycle_guard across this read and its mutation.
            # Do not take that lock again or hold SQLite across native processes.
            with runtime.store.read() as conn:
                used = conn.execute(
                    "SELECT 1 FROM watches WHERE account=? AND "
                    "(state IN ('active','matched') OR cleanup_state!='done') LIMIT 1",
                    (account,),
                ).fetchone() is not None
            return {"ok": True, "contract": "gws-account-lifecycle-v1",
                    "account": account, "credentials_in_use": used}
        names = list_accounts(root())
        if account is not None:
            names = [name for name in names if name == account.lower()]
        payload = {"ok": True, "accounts": names}
        if args.get("check_health"):
            payload["health"] = {name: account_health(root(), name,
                gws_bin=ctx.get_config("gws_bin", default="gws")) for name in names}
        return payload

    def api(args, **kwargs):
        return execute_gws(root(), args.get("account"), args.get("args"),
                           gws_bin=ctx.get_config("gws_bin", default="gws"))

    def workspace_handler(name):
        def handler(args, **kwargs):
            return execute_gws(root(), args.get("account"), build_args(name, args),
                               gws_bin=ctx.get_config("gws_bin", default="gws"))
        return safe(handler)

    for name, schema in SCHEMAS.items():
        ctx.register_tool(name=name, toolset="gws_workspace",
                          handler=workspace_handler(name), schema=schema)

    ctx.register_tool(name="gws_accounts", toolset="gws_workspace", handler=safe(accounts),
        schema={"name": "gws_accounts", "description": "List explicitly provisioned native gws account slots in this single-operator profile. Health returns sanitized native status, not credentials. A slot name is not proof of the Google identity. check_lifecycle is a read-only companion coordination contract, not authorization to delete credentials.",
                "parameters": {"type": "object", "additionalProperties": False, "properties": {
                    "account": {"type": "string", "description": "Optional exact account; required for lifecycle check"},
                    "check_health": {"type": "boolean", "default": False},
                    "check_lifecycle": {"type": "boolean", "default": False}}, "required": []}})
    ctx.register_tool(name="gws_api", toolset="gws_workspace", handler=safe(api),
        schema={"name": "gws_api", "description": "Advanced native Workspace API for one explicit account. Prefer curated tools and gws_schema. Native auth lifecycle/export is blocked; provision accounts with operator-side hermes gws login. Mutations, sends/shares and local file input/output require Hermes approval. Pass CLI tokens, not shell text. Large successful output returns output_file/output_bytes; process locally and remove after use. Failures are sanitized.",
                "parameters": {"type": "object", "additionalProperties": False, "properties": {
                    "account": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}}}, "required": ["account", "args"]}})

    async def watch_command(raw_args, **kwargs):
        try:
            root()
            parsed = watch_tools.parse_command(raw_args)
            if parsed is None:
                return watch_tools.COMMAND_HELP
            service, args = parsed
            origin = command_origin(kwargs.get("source"), kwargs.get("session_id"),
                                    kwargs.get("session_key"), kwargs.get("platform"), profile)
            # Snapshot before the thread hop; no ambient session or model-chosen destination.
            result = await asyncio.to_thread(watch_tools.handle, service, args, runtime, origin=origin)
            return watch_tools.command_reply(result)
        except Exception as exc:
            return watch_tools.command_reply(watch_tools._error(exc))

    # The gateway checks access against the typed name before normalizing it.
    for name in ("gws-watch", "gws_watch"):
        ctx.register_command(name, watch_command,
                             description="Start, inspect or stop a Workspace watch for this conversation",
                             args_hint='<gmail|drive|calendar> <JSON>')

    ctx.register_hook("pre_gateway_dispatch", watch_tools.bridge_callback(runtime.store, ctx, runtime.routes, profile=profile))

    def lifecycle_check(args):
        # Companion completion threads have no routed ContextVars. This instance
        # is already bound to this home; bind only that home for the local read.
        token = set_hermes_home_override(home)
        try:
            return accounts(args)
        finally:
            reset_hermes_home_override(token)

    def native_binary():
        token = set_hermes_home_override(home)
        try:
            return ctx.get_config("gws_bin", default="gws")
        finally:
            reset_hermes_home_override(token)

    from .account_cli import register_cli
    backend = AccountBackend(home / "gws-oauth", native_binary, lifecycle_check)

    def cli_backend():
        root()  # Never borrow a different profile's loaded CLI descriptor.
        return backend

    register_cli(ctx, cli_backend)
    ctx.on_unload(backend.close)
    native_accounts = backend
