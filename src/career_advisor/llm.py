"""LLM seam — one function, one contract: ``complete(prompt) -> str``.

The interview and document engines only ever call :func:`complete`, so
swapping providers means satisfying that contract and nothing else.

By default this delegates to ``agent_orch.llm.complete`` (the Codex CLI seam
this app was built against; model and effort pinned below — all Codex
mechanics live there, imported, not copied, and auth is the CLI's own
``~/.codex`` state, never env vars). That package is **optional**: when it is
absent the import still succeeds and everything else — including the whole
test suite — works. Only a real model call fails, with an actionable message.

To use your own provider, set ``CAREER_ADVISOR_LLM_PROVIDER`` to the import
path of a callable that takes a prompt string and returns the model's text,
e.g. ``mypackage.myprovider:complete``.
"""

from __future__ import annotations

import importlib
import os

DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_EFFORT = "medium"

_PROVIDER_ENV = "CAREER_ADVISOR_LLM_PROVIDER"

_MISSING_SEAM_HELP = (
    "No LLM provider is available. Either install the agent-orch package "
    "(which provides the default agent_orch.llm Codex seam), or point "
    f"{_PROVIDER_ENV} at your own callable — 'module.path:function', taking a "
    "prompt string and returning the model's reply as text."
)

try:  # The default seam. Optional on purpose — see the module docstring.
    from agent_orch.llm import CodexCompletionError as _ProviderError
    from agent_orch.llm import complete as _codex_complete

    _SEAM_AVAILABLE = True
except ImportError:  # pragma: no cover - hit by clones without agent-orch

    class _ProviderError(RuntimeError):
        """Raised when a completion cannot be produced."""

    _codex_complete = None
    _SEAM_AVAILABLE = False


# The engines catch llm.LLMError, so it must name the same class either way.
LLMError = _ProviderError


def _load_provider(spec: str):
    """Import a ``module.path:function`` provider callable."""
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise LLMError(
            f"{_PROVIDER_ENV} must look like 'module.path:function', got {spec!r}"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise LLMError(f"Could not import provider {spec!r}: {exc}") from exc
    provider = getattr(module, attr, None)
    if not callable(provider):
        raise LLMError(f"Provider {spec!r} is not callable.")
    return provider


def complete(prompt: str, *, timeout_seconds: float | None = None) -> str:
    """Return the model's reply to ``prompt``.

    Raises :data:`LLMError` if no provider is configured, or if the call fails.
    """
    provider_spec = os.environ.get(_PROVIDER_ENV)
    if provider_spec:
        return _load_provider(provider_spec)(prompt)

    if not _SEAM_AVAILABLE:
        raise LLMError(_MISSING_SEAM_HELP)

    kwargs = {}
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds
    elif os.environ.get("CAREER_ADVISOR_LLM_TIMEOUT"):
        kwargs["timeout_seconds"] = float(os.environ["CAREER_ADVISOR_LLM_TIMEOUT"])
    if os.environ.get("CAREER_ADVISOR_CODEX_SANDBOX"):
        kwargs["sandbox"] = os.environ["CAREER_ADVISOR_CODEX_SANDBOX"]
    return _codex_complete(
        prompt,
        model=os.environ.get("CAREER_ADVISOR_LLM_MODEL", DEFAULT_MODEL),
        effort=os.environ.get("CAREER_ADVISOR_LLM_EFFORT", DEFAULT_EFFORT),
        **kwargs,
    )
