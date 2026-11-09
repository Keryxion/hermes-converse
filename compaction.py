"""Apply an absolute compaction trigger only to this profile's live voice agents."""
from functools import wraps


def install_voice_compaction(adapter, hub, get_config):
    # The Runs API has no per-request compression option. Wrap this adapter's
    # agent factory, before preflight compaction, rather than editing shared config.
    original = adapter._create_agent

    @wraps(original)
    def create_agent(*args, **kwargs):
        agent = original(*args, **kwargs)
        session_id = kwargs.get('session_id') or getattr(agent, 'session_id', '')
        if hub.voice_target(session_id) is None:
            return agent
        from .settings import compaction_tokens
        limit = compaction_tokens(get_config('compression_threshold_tokens', 0))
        if not limit:
            return agent
        compressor = agent.context_compressor
        existing = compressor.threshold_tokens_cap
        compressor.threshold_tokens_cap = min(existing, limit) if existing else limit
        compressor._apply_threshold_tokens_cap()
        # Recompute retained-history and summary budgets against the new trigger.
        compressor._tail_token_budget = compressor._max_summary_tokens = None
        agent.compression_enabled = True
        return agent

    adapter._create_agent = create_agent

    def uninstall():
        if adapter._create_agent is create_agent:
            adapter._create_agent = original

    return uninstall
