# Runs the MCP server over stdio: docker run -i --rm -v /path/to/nodes.json:/config/nodes.json:ro stack-guardian
# Standard library only, so the image is the base Python image plus this package.
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY stack_guardian ./stack_guardian
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 guardian
COPY examples/nodes.json /config/nodes.json

USER guardian
ENV STACK_GUARDIAN_NODES=/config/nodes.json \
    STACK_GUARDIAN_STATE=/home/guardian/.stack-guardian/state.json \
    PYTHONUNBUFFERED=1
ENTRYPOINT ["stack-guardian-mcp"]
