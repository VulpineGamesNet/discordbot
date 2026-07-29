FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies using uv
RUN uv sync --frozen --no-dev

# Copy application code
COPY bot.py config.py database.py models.py ./

# Set uv cache to writable location
ENV UV_CACHE_DIR=/app/.cache/uv
ENV PYTHONUNBUFFERED=1

# Make app directory writable for any user
RUN chmod -R 777 /app

# Run the bot using uv
CMD ["uv", "run", "python", "bot.py"]
