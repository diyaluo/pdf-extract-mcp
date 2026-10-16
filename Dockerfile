FROM python:3.12-slim

# Required by the official MCP Registry for OCI ownership verification.
# Value MUST match the server "name" in server.json.
LABEL org.opencontainers.image.title="pdf-extract-mcp"
LABEL io.modelcontextprotocol.mcp.version="0.1.0"
LABEL io.modelcontextprotocol.server.name="io.github.diyaluo/pdf-extract-mcp"

WORKDIR /app

# Install deps first for layer caching
RUN pip install --no-cache-dir \
    "mcp>=1.2.0" \
    "pdfplumber>=0.11" \
    "pandas>=2.0" \
    "openpyxl>=3.1"

# Copy the src-layout package
COPY src /app/src
COPY pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir --no-deps -e .

# stdio MCP server — entrypoint is the console script
ENTRYPOINT ["pdf-extract-mcp"]
