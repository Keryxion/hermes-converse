"""Connection-scoped model choices; no credentials or global configuration changes."""
from typing import Any, Optional


def public_model_catalog(raw: dict[str, Any]) -> dict[str, Any]:
    """Forward display fields only, never provider configuration/credentials."""
    providers = []
    for provider in raw.get('providers', []):
        if not isinstance(provider, dict) or provider.get('authenticated') is False:
            continue
        slug = provider.get('slug')
        if not isinstance(slug, str) or not slug or len(slug) > 80:
            continue
        models = provider.get('models')
        if not isinstance(models, list):
            continue
        visible = []
        for model in models:
            try:
                choice, _ = parse_model_selection({'model': model, 'provider': slug})
            except ValueError:
                continue
            if choice not in visible:
                visible.append(choice)
        if visible:
            name = provider.get('name')
            providers.append({'slug': slug, 'name': name[:256] if isinstance(name, str) else slug,
                              'models': visible})
    return {'providers': providers,
            **{key: raw[key] for key in ('model', 'provider') if isinstance(raw.get(key), str)}}


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
