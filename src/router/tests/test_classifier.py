from router.classifier import classify_request


def test_image_content_routes_to_vision():
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

    assert classify_request(request) == "vision"


def test_code_request_routes_to_opencodego_fast():
    request = {
        "messages": [
            {
                "role": "user",
                "content": "Please refactor src/router/main.py and fix this stack trace",
            }
        ]
    }

    assert classify_request(request) == "opencodego-fast"


def test_analysis_request_routes_to_deepseek_pro():
    request = {
        "messages": [
            {
                "role": "user",
                "content": "Analyze this architecture and explain why requests time out.",
            }
        ]
    }

    assert classify_request(request) == "deepseek-pro"


def test_plain_request_routes_to_fast():
    request = {"messages": [{"role": "user", "content": "say hello"}]}

    assert classify_request(request) == "fast"
