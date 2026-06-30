from router.routing import DEFAULT_ALLOWED_MODELS, DEFAULT_FALLBACKS, RouteConfig, choose_model, next_fallback


def test_default_allowed_models_does_not_include_vision():
    assert "vision" not in DEFAULT_ALLOWED_MODELS


def test_default_fallbacks_does_not_include_vision():
    assert "vision" not in DEFAULT_FALLBACKS


def test_classified_vision_maps_to_fast_when_vision_disallowed():
    config = RouteConfig(cache_ttl_seconds=600)
    request = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is in this screenshot?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ]
    }

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "fast"
    assert decision.reason == "classified"


def test_keeps_warm_session_model_inside_ttl():
    config = RouteConfig(cache_ttl_seconds=600)
    session = {"model": "deepseek-pro", "last_used_ts": 1_000.0}
    request = {"messages": [{"role": "user", "content": "simple follow up"}]}

    decision = choose_model(request, session=session, now=1_200.0, config=config)

    assert decision.model == "deepseek-pro"
    assert decision.reason == "warm-session"


def test_reclassifies_cold_session_after_ttl():
    config = RouteConfig(cache_ttl_seconds=600)
    session = {"model": "deepseek-pro", "last_used_ts": 1_000.0}
    request = {"messages": [{"role": "user", "content": "simple follow up"}]}

    decision = choose_model(request, session=session, now=1_700.0, config=config)

    assert decision.model == "fast"
    assert decision.reason == "classified"


def test_honors_allowed_explicit_model():
    config = RouteConfig(cache_ttl_seconds=600, allowed_models={"fast", "ollama-cloud"})
    request = {
        "model": "ollama-cloud",
        "messages": [{"role": "user", "content": "say hello"}],
    }

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "ollama-cloud"
    assert decision.reason == "explicit-model"


def test_ignores_unknown_explicit_model():
    config = RouteConfig(cache_ttl_seconds=600, allowed_models={"fast"})
    request = {
        "model": "unknown",
        "messages": [{"role": "user", "content": "say hello"}],
    }

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "fast"
    assert decision.reason == "classified"


def test_next_fallback_returns_next_alias():
    config = RouteConfig(fallbacks={"deepseek-pro": ["opencodego-code", "fast"]})

    assert next_fallback("deepseek-pro", 0, config) == "opencodego-code"
    assert next_fallback("deepseek-pro", 1, config) == "fast"
    assert next_fallback("deepseek-pro", 2, config) is None


def test_choose_model_uses_configured_classifier_keywords():
    config = RouteConfig(
        allowed_models={"fast", "opencodego-fast"},
        classifier_keywords={
            "code_signals": ["frobnicate"],
            "reasoning_signals": [],
        },
    )
    request = {"messages": [{"role": "user", "content": "please frobnicate the widget"}]}

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "opencodego-fast"
    assert decision.reason == "classified"


def test_choose_model_falls_back_to_default_signals_when_keywords_empty():
    config = RouteConfig(allowed_models={"fast", "opencodego-fast"})
    request = {
        "messages": [{"role": "user", "content": "please refactor src/app.py"}]
    }

    decision = choose_model(request, session=None, now=1_000.0, config=config)

    assert decision.model == "opencodego-fast"
