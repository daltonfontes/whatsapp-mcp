FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app/whatsapp-mcp-server
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY *.py ./
ENV WHATSAPP_API_BASE_URL=http://bridge:8080/api
CMD ["uv", "run", "--frozen", "main.py"]
