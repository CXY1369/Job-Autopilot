from autojobagent.core.llm_runtime import run_chat_with_fallback


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, handler):
        self._handler = handler
        self.called_models: list[str] = []

    def create(self, **kwargs):
        model = kwargs.get("model", "")
        self.called_models.append(model)
        result = self._handler(model)
        if isinstance(result, Exception):
            raise result
        return _FakeCompletion(result)


class _FakeChat:
    def __init__(self, handler):
        self.completions = _FakeCompletions(handler)


class _FakeClient:
    def __init__(self, handler):
        self.chat = _FakeChat(handler)


def test_run_chat_with_fallback_switches_on_rate_limit():
    def handler(model: str):
        if model == "m1":
            return Exception("429 rate_limit exceeded")
        return '{"status":"continue"}'

    client = _FakeClient(handler)
    logs: list[tuple[str, str]] = []
    result = run_chat_with_fallback(
        client=client,
        fallback_models=["m1", "m2"],
        start_model_index=0,
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.1,
        max_tokens=100,
        on_log=lambda level, message: logs.append((level, message)),
        sleep_seconds=0.0,
    )

    assert result.ok is True
    assert result.model == "m2"
    assert result.model_index == 1
    assert result.raw == '{"status":"continue"}'
    assert client.chat.completions.called_models == ["m1", "m2"]
    assert any(level == "warn" for level, _ in logs)


def test_run_chat_with_fallback_stops_on_generic_error():
    client = _FakeClient(lambda _model: Exception("connection reset by peer"))
    result = run_chat_with_fallback(
        client=client,
        fallback_models=["m1", "m2"],
        start_model_index=0,
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.1,
        max_tokens=100,
        sleep_seconds=0.0,
    )

    assert result.ok is False
    assert result.error_code == "llm_call_failed"
    assert "LLM 调用失败" in (result.error_summary or "")
    assert client.chat.completions.called_models == ["m1"]


def test_run_chat_with_fallback_exhausts_unsupported_models():
    client = _FakeClient(lambda _model: Exception("model_not_found"))
    result = run_chat_with_fallback(
        client=client,
        fallback_models=["m1", "m2"],
        start_model_index=0,
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.1,
        max_tokens=100,
        sleep_seconds=0.0,
    )

    assert result.ok is False
    assert result.error_code == "model_unsupported_exhausted"
    assert "不支持当前请求" in (result.error_summary or "")
    assert client.chat.completions.called_models == ["m1", "m2"]
