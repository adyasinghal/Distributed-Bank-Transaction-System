"""Maps an account ID to the server that owns it."""

import config


def route(account_id: str) -> tuple[int | None, str | None]:
    """Return (server_id, base_url), or (None, None) if no server owns the account."""
    sid = config.server_for(account_id)
    if sid is None:
        return None, None
    return sid, config.server_url(sid)
