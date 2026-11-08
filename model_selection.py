"""Connection-scoped model choices; no credentials or global configuration changes."""
from typing import Any, Optional


def parse_model_selection(frame: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    model, provider = frame.get('model'), frame.get('provider')
    if model is None and provider is None:
        return None, None
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise ValueError('bad_model_selection')
    if provider is not None and (not isinstance(provider, str) or not provider.strip() or len(provider) > 80):
        raise ValueError('bad_model_selection')
    if any(ord(c) < 32 or ord(c) == 127 for value in (model, provider or '') for c in value):
        raise ValueError('bad_model_selection')
    return model.strip(), provider.strip() if provider else None
