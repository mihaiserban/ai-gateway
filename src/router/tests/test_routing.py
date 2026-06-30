from router.routing import RouteConfig, choose_model, next_fallback


def test_defaults_to_default_model_without_inspecting_content():
    config = RouteConfig(cache_ttl_seconds=600, allowed_models={"explorer", "coder"}, default_model="explorer")
    request = {"messages": [{"role": "user", "content": "please refactor src/app.py and explain the root cause"}]}

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "explorer"
    assert decision.reason == "default-model"


def test_keeps_warm_session_model_inside_ttl():
    config = RouteConfig(cache_ttl_seconds=600)
    session = {"model": "planner", "last_used_ts": 1_000.0}
    request = {"messages": [{"role": "user", "content": "simple follow up"}]}

    decision = choose_model(request, session=session, now=1_200.0, config=config)

    assert decision.model == "planner"
    assert decision.reason == "warm-session"


def test_reclassifies_cold_session_after_ttl():
    config = RouteConfig(cache_ttl_seconds=600, default_model="explorer")
    session = {"model": "planner", "last_used_ts": 1_000.0}
    request = {"messages": [{"role": "user", "content": "simple follow up"}]}

    decision = choose_model(request, session=session, now=1_700.0, config=config)

    assert decision.model == "explorer"
    assert decision.reason == "default-model"


def test_honors_allowed_explicit_model():
    config = RouteConfig(cache_ttl_seconds=600, allowed_models={"explorer", "explorer-ocg"})
    request = {
        "model": "explorer-ocg",
        "messages": [{"role": "user", "content": "say hello"}],
    }

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "explorer-ocg"
    assert decision.reason == "explicit-model"


def test_honors_explicit_model_case_insensitively():
    config = RouteConfig(cache_ttl_seconds=600, allowed_models={"explorer", "planner"})
    request = {
        "model": "PLANNER",
        "messages": [{"role": "user", "content": "say hello"}],
    }

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "planner"
    assert decision.reason == "explicit-model"


def test_ignores_unknown_explicit_model():
    config = RouteConfig(cache_ttl_seconds=600, allowed_models={"explorer"}, default_model="explorer")
    request = {
        "model": "unknown",
        "messages": [{"role": "user", "content": "say hello"}],
    }

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "explorer"
    assert decision.reason == "default-model"


def test_next_fallback_returns_next_alias():
    config = RouteConfig(fallbacks={"planner": ["planner-ocg", "explorer"]})

    assert next_fallback("planner", 0, config) == "planner-ocg"
    assert next_fallback("planner", 1, config) == "explorer"
    assert next_fallback("planner", 2, config) is None


def test_default_model_can_be_configured():
    config = RouteConfig(allowed_models={"explorer", "planner"}, default_model="planner")
    request = {"messages": [{"role": "user", "content": "say hello"}]}

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "planner"
    assert decision.reason == "default-model"
