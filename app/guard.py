"""Public-demo guards: input validation and the client IP (the rate limit itself is in app/db.py)."""
from __future__ import annotations

from starlette.requests import Request

from .config import Data, Settings


class BadInput(ValueError):
    pass


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _text(body: dict, key: str, max_chars: int) -> str:
    v = body.get(key)
    if not isinstance(v, str) or not v.strip():
        raise BadInput(f"{key} is required")
    v = v.strip()
    if len(v) > max_chars:
        raise BadInput(f"{key} is longer than {max_chars} characters")
    return v


def validate_race(body: object, settings: Settings, data: Data) -> dict:
    if not isinstance(body, dict):
        raise BadInput("body must be a JSON object")
    feature = body.get("feature")
    preset_id = body.get("preset_id")
    if preset_id is not None and (not isinstance(preset_id, str) or len(preset_id) > 32):
        raise BadInput("preset_id must be a short string")

    model = body.get("openai_model") or settings.openai_model
    if model not in settings.allowed_openai_models:
        raise BadInput(f"openai_model must be one of: {', '.join(settings.allowed_openai_models)}")

    if feature == "recat":
        return {"feature": feature, "preset_id": preset_id, "openai_model": model, "comment": _text(body, "comment", settings.max_input_chars)}

    if feature == "prioritiser":
        return {"feature": feature, "preset_id": preset_id, "openai_model": model, "message": _text(body, "message", settings.max_input_chars)}

    raise BadInput("feature must be 'recat' or 'prioritiser'")
