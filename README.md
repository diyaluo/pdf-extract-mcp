# pdf-extract-mcp

A Model Context Protocol (MCP) server that extracts tables and text from
**digital (text-layer) PDFs** — bank statements, invoices, bills — into
**CSV / JSON / Excel**, with automatic **PII redaction**.

> Part of the [invoice-extractor](../) product. This package is the MCP
> distribution channel ("ecosystem parasite": one backend, many host
> workflows). Web demo lives in the parent project.

## What it does

- `extract_pdf` — upload a digital PDF (base64 or URL), get back structured
  table rows as CSV / JSON records / XLSX.
- `health` — service status and free-tier limits.

**Table detection** tries three strategies per page: ruled tables → explicit
columns derived from the header row's own x-positions (handles statement PDFs
with *no* grid lines, the common case for Western banks) → generic text
alignment.

**PII redaction** (on by default) covers:

| Region | Masked |
|---|---|
| US / international | SSN, EIN/TIN, IBAN, SWIFT/BIC, routing/ABA, account numbers, card numbers (plain **and** `4111 1111 1111 1111` / dashed), US & international phone, email |
| China | 18/15-digit national ID, mobile phone, bank card, USCC (统一社会信用代码), passport, 2–3 char personal names, organization keywords |

Bank identifiers that are ambiguous as bare numbers (routing/account/sort code)
are masked **only when they follow a label**, so reference numbers and amounts
stay readable. Amounts, dates and descriptions are never altered.

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
`~/.cache/pdf-extract-mcp/mcp_usage.jsonl` (override with
`PDF_EXTRACT_STATE_DIR`). Clients are recorded as salted hashes; **no PDF
content and no PII are ever logged**.

> Note: as a distributed stdio bundle this file lives on the *user's* machine,
> so the publisher cannot read it. Real external usage is read from the hosting
> registry's owner API instead (`GET /servers/{namespace}/{name}/logs` on
> Smithery returns the invocation count).

## Stack

`mcp >= 1.2` (works on both mcp 1.x FastMCP and mcp 2.x MCPServer), pdfplumber,
openpyxl. **pandas is optional** — when absent the package falls back to a
lightweight list-of-rows table (stdlib CSV/JSON + openpyxl), which keeps the
distributable bundle small. Python 3.10+.

## License

MIT
