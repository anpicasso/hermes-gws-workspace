"""Watch origins from host-dispatched slash-command context only."""
from __future__ import annotations

if __package__:
    from .watch import WatchError
else:
    from watch import WatchError


def _text(value, *, optional=False):
    if optional and value in (None, ""):
        return None
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise WatchError("no_trusted_origin", "The command needs a valid messaging session, chat and sender")
    return value


def command_origin(source, session_id, session_key, platform, profile):
    """Snapshot public command kwargs, never text arguments or ambient route state.

    The caller checks its registered profile home before entering here. A source
    with no explicit profile uses that command's routed PluginContext profile.
    This function is not exposed as a model tool or a general origin reader.
    """
    platform, profile = _text(platform), _text(profile)
    if platform in {"webhook", "msgraph_webhook", "api", "api_server", "cron",
                    "cli", "tui", "local", "desktop", "kanban", "tool", "gateway",
                    "subagent"}:
        raise WatchError("no_trusted_origin", "Use /gws-watch in the originating messaging conversation")
    source_platform = getattr(source, "platform", None)
    if getattr(source_platform, "value", source_platform) != platform:
        raise WatchError("no_trusted_origin", "Command source and platform do not match")
    if getattr(source, "profile", None) not in (None, "", profile):
        raise WatchError("no_trusted_origin", "Command source belongs to another profile")
    if getattr(source, "is_bot", False):
        raise WatchError("no_trusted_origin", "A bot-authored command cannot create a watch origin")
    # The WhatsApp bridge batches by session; command kwargs do not carry the merged event's actor.
    if platform == "whatsapp" and (getattr(source, "chat_type", None) != "dm" or getattr(source, "shared_session", False)):
        raise WatchError("no_trusted_origin", "Use a private WhatsApp DM; shared WhatsApp commands cannot identify a single sender")
    if not session_id:
        raise WatchError("no_trusted_origin", "Send a normal message first to create this chat's session, then retry /gws-watch")
    actor = _text(getattr(source, "user_id", None))
    origin = {"profile": profile, "principal": actor, "user_id": actor,
              "platform": platform, "chat_id": _text(getattr(source, "chat_id", None)),
              "session_id": _text(session_id), "session_key": _text(session_key)}
    for field in ("thread_id", "scope_id", "parent_chat_id", "chat_type"):
        origin[field] = _text(getattr(source, field, None), optional=True)
    return origin
