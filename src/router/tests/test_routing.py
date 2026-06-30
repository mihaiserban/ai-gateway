from router.routing import RouteConfig, choose_model, next_fallback


def test_defaults_to_default_model_without_inspecting_content():
    config = RouteConfig(cache_ttl_seconds=600, allowed_models={"fast", "opencodego-fast"}, default_model="fast")
    request = {
        "messages": [
            {"role": "user", "content": "please refactor src/app.py and explain the root cause"}
        ]
    }

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "fast"
    assert decision.reason == "default-model"


def test_keeps_warm_session_model_inside_ttl():
    config = RouteConfig(cache_ttl_seconds=600)
    session = {"model": "deepseek-pro", "last_used_ts": 1_000.0}
    request = {"messages": [{"role": "user", "content": "simple follow up"}]}

    decision = choose_model(request, session=session, now=1_200.0, config=config)

    assert decision.model == "deepseek-pro"
    assert decision.reason == "warm-session"


def test_reclassifies_cold_session_after_ttl():
    config = RouteConfig(cache_ttl_seconds=600, default_model="fast")
    session = {"model": "deepseek-pro", "last_used_ts": 1_000.0}
    request = {"messages": [{"role": "user", "content": "simple follow up"}]}

    decision = choose_model(request, session=session, now=1_700.0, config=config)

    assert decision.model == "fast"
    assert decision.reason == "default-model"


def test_honors_allowed_explicit_model():
    config = RouteConfig(cache_ttl_seconds=600, allowed_models={"fast", "ollama-cloud"})
    request = {
        "model": "ollama-cloud",
        "messages": [{"role": "user", "content": "say hello"}],
    }

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "ollama-cloud"
    assert decision.reason == "explicit-model"


def test_honors_explicit_model_case_insensitively():
    config = RouteConfig(cache_ttl_seconds=600, allowed_models={"fast", "deepseek-pro"})
    request = {
        "model": "DEEPSEEK-PRO",
        "messages": [{"role": "user", "content": "say hello"}],
    }

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "deepseek-pro"
    assert decision.reason == "explicit-model"


def test_ignores_unknown_explicit_model():
    config = RouteConfig(cache_ttl_seconds=600, allowed_models={"fast"}, default_model="fast")
    request = {
        "model": "unknown",
        "messages": [{"role": "user", "content": "say hello"}],
    }

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "fast"
    assert decision.reason == "default-model"


def test_next_fallback_returns_next_alias():
    config = RouteConfig(fallbacks={"deepseek-pro": ["opencodego-code", "fast"]})

    assert next_fallback("deepseek-pro", 0, config) == "opencodego-code"
    assert next_fallback("deepseek-pro", 1, config) == "fast"
    assert next_fallback("deepseek-pro", 2, config) is None


def test_default_model_can_be_configured():
    config = RouteConfig(allowed_models={"fast", "deepseek-pro"}, default_model="deepseek-pro")
    request = {"messages": [{"role": "user", "content": "say hello"}]}

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "deepseek-pro"
    assert decision.reason == "default-model"
