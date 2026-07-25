# Two ways to build this image.
#
#   docker compose up --build -d      the app alone (default). Works from a
#                                     fresh clone with no extra repositories.
#                                     Set CAREER_ADVISOR_LLM_PROVIDER to give
#                                     it a model — see README.md.
#
#   --target codex-seam               additionally installs agent-orch, the
#                                     default provider seam, from a named
#                                     build context. Needs that package's
#                                     source; see docker-compose.override.yml.
#
# The last stage is the default, so a bare `docker build .` builds the app and
# never fails looking for a build context you do not have.

FROM python:3.12-slim AS base

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV CAREER_ADVISOR_DB=/data/career-advisor.db

EXPOSE 8611

CMD ["career-advisor", "serve", "--host", "0.0.0.0", "--port", "8611"]


# Opt-in: the agent_orch.llm Codex seam, imported rather than vendored.
# Requires `additional_contexts: {agent-orch: <path>}` on the build.
FROM base AS codex-seam

COPY --from=agent-orch pyproject.toml README.md /opt/agent-orch/
COPY --from=agent-orch src /opt/agent-orch/src
RUN pip install --no-cache-dir /opt/agent-orch


# Default target. Kept last so the provider-agnostic build is what you get
# unless you explicitly ask for the seam above.
FROM base AS app
