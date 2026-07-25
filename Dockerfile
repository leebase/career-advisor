FROM python:3.12-slim

WORKDIR /app

# Default LLM provider seam: agent_orch.llm. "agent-orch" is a named build
# context (see docker-compose.yml) pointing at that package's source —
# imported, not vendored.
#
# Using your own provider instead? Delete these three lines and the matching
# additional_contexts entry in docker-compose.yml, then set
# CAREER_ADVISOR_LLM_PROVIDER in the service environment. See README.md.
COPY --from=agent-orch pyproject.toml README.md /opt/agent-orch/
COPY --from=agent-orch src /opt/agent-orch/src
RUN pip install --no-cache-dir /opt/agent-orch

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV CAREER_ADVISOR_DB=/data/career-advisor.db

EXPOSE 8611

CMD ["career-advisor", "serve", "--host", "0.0.0.0", "--port", "8611"]
