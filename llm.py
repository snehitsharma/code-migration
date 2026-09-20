from __future__ import annotations
from typing import Callable, Protocol


class LLMClient(Protocol):
    """Minimal seam for agent calls: a prompt in, the raw reply (expected JSON) out."""

    def complete(self, prompt: str) -> str: ...


class FakeLLM:
    """Offline stand-in for tests. `responses` is a list consumed in order (the last one
    repeats) or a callable prompt -> reply. All prompts are kept in `prompts`."""

    def __init__(self, responses: list[str] | Callable[[str], str]) -> None:
        self._responses = responses
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if callable(self._responses):
            return self._responses(prompt)
        i = min(len(self.prompts) - 1, len(self._responses) - 1)
        return self._responses[i]


class ClaudeLLM:
    """Real client over the Anthropic Messages API. Thinking is left at the model default;
    the SDK reads ANTHROPIC_API_KEY (or an `ant auth login` profile) from the environment.
    A refusal raises instead of returning text, so it can never be mistaken for output."""

    def __init__(self, model: str = "claude-opus-5", max_tokens: int = 16000) -> None:
        import anthropic  # optional dependency: pip install ".[claude]"

        self._client = anthropic.Anthropic()
        self.model, self.max_tokens = model, max_tokens

    def complete(self, prompt: str) -> str:
        response = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"model refused the request: {response.stop_details}")
        return "".join(b.text for b in response.content if b.type == "text")
