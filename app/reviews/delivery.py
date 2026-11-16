"""Keep public review content and per-recipient delivery state in the same legacy field."""

import json

DELIVERY_KEYS = ("targets", "blocked_targets", "target_sent_at")


def payload_dict(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def refreshed_payload(incoming: str | None, existing: str | None) -> str | None:
    if incoming is None:
        return existing
    state = payload_dict(existing)
    content = payload_dict(incoming)
    for key in DELIVERY_KEYS:
        if key in state:
            content[key] = state[key]
    return json.dumps(content, ensure_ascii=False)
