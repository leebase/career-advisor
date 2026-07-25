"""Career Advisor LLM wrapper: product defaults must reach the Codex argv.

The Codex mechanics themselves are tested in agent-orch (tests/test_llm.py);
here we only verify the delegation and the gpt-5.6-terra/medium defaults.
"""

import stat

import pytest

from career_advisor import llm

FAKE_CODEX = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CODEX_ARGV_LOG"
prev=""
for arg in "$@"; do
  if [ "$prev" = "--output-last-message" ]; then printf 'ok' > "$arg"; fi
  prev="$arg"
done
"""


@pytest.fixture()
def fake_codex(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_text(FAKE_CODEX, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    argv_log = tmp_path / "argv.log"
    monkeypatch.setenv("AGENT_ORCH_CODEX_BINARY", str(binary))
    monkeypatch.setenv("FAKE_CODEX_ARGV_LOG", str(argv_log))
    return argv_log


needs_default_seam = pytest.mark.skipif(
    not llm._SEAM_AVAILABLE,
    reason="agent-orch is not installed; the default Codex seam is unavailable",
)


@needs_default_seam
def test_defaults_reach_codex_argv(fake_codex):
    assert llm.complete("hello") == "ok"
    argv = fake_codex.read_text(encoding="utf-8").splitlines()
    assert argv[argv.index("-m") + 1] == "gpt-5.6-terra"
    assert 'model_reasoning_effort="medium"' in argv


@needs_default_seam
def test_env_overrides(fake_codex, monkeypatch):
    monkeypatch.setenv("CAREER_ADVISOR_LLM_MODEL", "gpt-5.5")
    monkeypatch.setenv("CAREER_ADVISOR_LLM_EFFORT", "high")
    llm.complete("hello")
    argv = fake_codex.read_text(encoding="utf-8").splitlines()
    assert argv[argv.index("-m") + 1] == "gpt-5.5"
    assert 'model_reasoning_effort="high"' in argv


# --- Pluggable provider: the only contract is complete(prompt) -> str --------


def test_custom_provider_takes_precedence(monkeypatch):
    monkeypatch.setenv(
        "CAREER_ADVISOR_LLM_PROVIDER", "tests.test_llm:_echo_provider"
    )
    assert llm.complete("ping") == "echo: ping"


def _echo_provider(prompt: str) -> str:
    return f"echo: {prompt}"


@pytest.mark.parametrize(
    "spec, fragment",
    [
        ("no_colon_here", "module.path:function"),
        ("tests.test_llm:not_a_real_name", "not callable"),
        ("nonexistent_module_xyz:complete", "Could not import"),
    ],
)
def test_bad_provider_specs_raise_llm_error(monkeypatch, spec, fragment):
    monkeypatch.setenv("CAREER_ADVISOR_LLM_PROVIDER", spec)
    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete("hello")
    assert fragment in str(excinfo.value)


def test_missing_seam_message_is_actionable():
    """A clone without agent-orch must still import and explain itself."""
    assert "CAREER_ADVISOR_LLM_PROVIDER" in llm._MISSING_SEAM_HELP
    assert "agent-orch" in llm._MISSING_SEAM_HELP
