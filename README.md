# pdf-extract-mcp

A Model Context Protocol (MCP) server that extracts tables and text from
**digital (text-layer) PDFs** — bank statements, invoices, bills — into
**CSV / JSON / Excel**, with automatic **PII redaction**.

> Part of the [invoice-extractor](../) product. This package is the MCP
> distribution channel ("ecosystem parasite": one backend, many host
> workflows). Web demo lives in the parent project.

## What it does

- `extract_pdf` — upload a digital PDF (base64 or URL), get back structured
  table rows as CSV / JSON records / XLSX. Sensitive fields are masked by
  default: national ID, phone, bank card, unified social credit code, email,
  passport, Chinese personal names, organization names.
- `health` — service status and free-tier limits.

Scanned / image-only PDFs are explicitly rejected (`SCANNED_PDF_UNSUPPORTED`)
— the free tier covers digital PDFs only (OCR is a paid-tier candidate).

## Limits (free tier)

- **100 pages / rolling 30 days / client** (anonymous, keyed by client hash).
- 50 MB per file.
- Above quota the tool returns a structured `QUOTA_EXCEEDED` response with an
  in-band upgrade pointer. It never silently bills.
- Set `PDF_EXTRACT_API_KEY` to a paid key to bypass the quota (billing
  gateway is a P1 item; current free tier has no paid keys issued).

## Run (local stdio — Claude Desktop / Cursor / any MCP client)

```bash
pip install -e .
pdf-extract-mcp
```

Client config example:

```json
{
  "mcpServers": {
    "pdf-extract": {
      "command": "pdf-extract-mcp",
      "env": {}
    }
  }
}
```

## Run (remote streamable-http, for hosted MCP clients)

```bash
REMOTE_TRANSPORT=http MCP_HOST=127.0.0.1 MCP_PORT=5060 \
  python -m pdf_extract_mcp.server
# POST JSON-RPC to http://127.0.0.1:5061/mcp
```

## Tool schema

```
extract_pdf(
  pdf_base64: str | None,     # preferred: PDF bytes as base64
  pdf_url:    str | None,     # alternative: direct https .pdf URL
  output:     "csv"|"json"|"xlsx",   # default csv
  redact_pii: bool = true,
  api_key:    str | None,    # paid key, optional
) -> {
  ok, type: "tables"|"text", pages, tables, rows, columns,
  csv | rows_json | xlsx_base64,   # depends on output
  elapsed_ms
}
```

## Telemetry

Every call is appended (best-effort, never blocks extraction) to
`../state/mcp_usage.jsonl`. The P0 validation metric is **real non-self
traffic**: third-party agents discovering and calling the server, not our own
tests. Clients are recorded as salted hashes, no PDF content is logged.

## Stack

`mcp >= 1.2` (works on both mcp 1.x FastMCP and mcp 2.x MCPServer),
pdfplumber, pandas, openpyxl. Python 3.10+.

## License

MIT
