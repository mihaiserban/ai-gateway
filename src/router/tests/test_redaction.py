from router.redaction import redact_text, redact_payload


def test_redacts_secret_like_values():
    text = "DEEPSEEK_API_KEY=sk-abc1234567890 and OLLAMA_API_KEY=2002781.secret"

    redacted = redact_text(text)

    assert "sk-abc1234567890" not in redacted
    assert "2002781.secret" not in redacted
    assert "DEEPSEEK_API_KEY=[REDACTED]" in redacted
    assert "OLLAMA_API_KEY=[REDACTED]" in redacted


def test_redacts_nested_payload_strings_without_mutating_input():
    payload = {
        "messages": [
            {"role": "user", "content": "token: sk-testsecret123456789"},
        ]
    }

    redacted = redact_payload(payload)

    assert payload["messages"][0]["content"] == "token: sk-testsecret123456789"
    assert "sk-testsecret123456789" not in redacted["messages"][0]["content"]
