"""
PDF Extract MCP Server
======================
Model Context Protocol server that extracts tables/text from digital
(text-based) PDFs into CSV/JSON/Excel, with automatic PII redaction.

Two transports:
  - stdio:  default, for Claude Desktop / Cursor / local MCP clients
            (python -m pdf_extract_mcp.server)
  - streamable-http:  REMOTE_TRANSPORT=http python -m pdf_extract_mcp.server
            (hosted MCP endpoint for Smithery/Glama/remote agents)

Tools:
  extract_pdf(pdf_base64 | pdf_url, output=csv|json|xlsx, redact_pii=true)
  health()

Free tier: 100 pages / 30 days / client (anonymous, keyed by client hash).
Above quota the tool returns an upgrade-required response (HTTP 402-like
in-band signal) — it never silently bills. Set PDF_EXTRACT_API_KEY to a
paid key to bypass the free quota (billing gateway is a P1 item).

Telemetry: every call is appended (best-effort, never blocks) to
state/mcp_usage.jsonl so we can measure real (non-self) traffic — the
whole point of the P0 validation.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import uuid
import hashlib
import logging
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---- paths / config ----------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[3]  # mcp-server/src/pdf_extract_mcp -> project root
STATE_DIR = PROJECT_ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)
USAGE_LOG = STATE_DIR / "mcp_usage.jsonl"
QUOTA_FILE = STATE_DIR / "mcp_quota.json"

FREE_PAGES_PER_30D = int(os.environ.get("FREE_PAGES_PER_30D", "100"))
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
FETCH_TIMEOUT_SEC = 30

UPGRADE_INFO = {
    "plan": "pdf-extract-pro",
    "free_limit_pages_30d": FREE_PAGES_PER_30D,
    "message": (
        "Free quota used. Get an API key at "
        "https://elbertscribe6.gumroad.com/ (Paddle/Stripe billing in progress) "
        "and set it as the `api_key` argument or PDF_EXTRACT_API_KEY."
    ),
}

logging.basicConfig(
    level=os.environ.get("MCP_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("pdf-extract-mcp")


# ---- MCP SDK (official Python SDK; mcp 2.x renamed FastMCP -> MCPServer) -------
try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer  # noqa: E402
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # noqa: E402

mcp = MCPServer("pdf-extract")


# ---- core extraction (reuse project extract.py, no copy-paste) ---------------
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from extract import extract_tables_digital, extract_text_digital, tables_to_dataframe  # noqa: E402
except Exception:  # pragma: no cover - allow import without heavy deps for schema
    extract_tables_digital = extract_text_digital = tables_to_dataframe = None


import pandas as pd  # noqa: E402
import pdfplumber  # noqa: E402


# ---- telemetry + quota -------------------------------------------------------
def _client_id(ctx_or_key: str | None) -> str:
    raw = ctx_or_key or f"anon-{uuid.getnode()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _record_usage(rec: dict) -> None:
    rec["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        with USAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # telemetry must never break extraction
        log.warning("telemetry write failed: %s", e)


def _load_quota() -> dict:
    if QUOTA_FILE.exists():
        try:
            return json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_quota(q: dict) -> None:
    tmp = QUOTA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
    tmp.replace(QUOTA_FILE)


def pages_used_30d(client: str) -> int:
    cutoff = time.time() - 30 * 86400
    total = 0
    if not USAGE_LOG.exists():
        return 0
    with USAGE_LOG.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("client") == client and r.get("ts_epoch", 0) >= cutoff:
                    total += int(r.get("pages", 0))
            except Exception:
                continue
    return total


# ---- input handling ----------------------------------------------------------
def _load_pdf_bytes(pdf_base64: str | None, pdf_url: str | None) -> bytes:
    if pdf_base64:
        return base64.b64decode(pdf_base64)
    if pdf_url:
        req = urllib.request.Request(
            pdf_url,
            headers={"User-Agent": "pdf-extract-mcp/1.0 (+https://gumroad.com)"},
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError("PDF too large (50 MB max)")
            if "pdf" not in ctype and not pdf_url.lower().split("?")[0].endswith(".pdf"):
                # allow but flag — some CDNs send application/octet-stream
                pass
            return data
    raise ValueError("Provide either pdf_base64 or pdf_url")


def _count_pages(path: str) -> int:
    with pdfplumber.open(path) as pdf:
        return len(pdf.pages)


# ---- tools -------------------------------------------------------------------
@mcp.tool()
def health() -> dict:
    """Service health + free-tier limits. No auth required."""
    return {
        "status": "ok",
        "service": "pdf-extract-mcp",
        "free_tier": {"pages_per_30d": FREE_PAGES_PER_30D},
        "digital_pdf_only": True,
        "pii_redaction": "china-id/phone/bank-card/uscc/email/name/org",
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
    }


@mcp.tool()
def extract_pdf(
    pdf_base64: str | None = None,
    pdf_url: str | None = None,
    output: str = "csv",
    redact_pii: bool = True,
    api_key: str | None = None,
) -> dict:
    """Extract tables from a digital (text-layer) PDF bank statement / invoice
    / bill into CSV, JSON rows, or Excel bytes (base64). PII is redacted by
    default. Scanned/image PDFs are rejected (no OCR in free tier).

    Args:
        pdf_base64: PDF content as base64 (preferred — no third-party fetch).
        pdf_url:    Direct HTTPS URL to a .pdf (alternative to base64).
        output:     "csv" | "json" | "xlsx".
        redact_pii: keep true unless you explicitly need raw PII (paid only).
        api_key:    paid key to bypass free quota (optional).
    """
    started = time.time()
    client = _client_id(api_key or os.environ.get("MCP_CLIENT_ID"))
    is_paid = bool(api_key and api_key == os.environ.get("PDF_EXTRACT_API_KEY", ""))
    rec = {"client": client, "ts_epoch": time.time(), "tool": "extract_pdf"}

    if output not in ("csv", "json", "xlsx"):
        return {"ok": False, "error": "output must be csv|json|xlsx"}
    if tables_to_dataframe is None:
        return {"ok": False, "error": "extraction deps unavailable on server"}

    # fetch to a temp file
    try:
        data = _load_pdf_bytes(pdf_base64, pdf_url)
    except Exception as e:
        rec.update(ok=False, pages=0, error=f"fetch:{e}")
        _record_usage(rec)
        return {"ok": False, "error": f"could not load PDF: {e}"}

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        try:
            pages = _count_pages(tmp_path)
        except Exception:
            pages = 0
        rec["pages"] = pages

        # free quota gate (counts before processing)
        if not is_paid:
            used = pages_used_30d(client)
            if used + max(pages, 1) > FREE_PAGES_PER_30D:
                rec.update(ok=False, blocked="quota")
                _record_usage(rec)
                return {
                    "ok": False,
                    "error_code": "QUOTA_EXCEEDED",
                    "pages_used_30d": used,
                    "pages_in_request": pages,
                    "free_limit": FREE_PAGES_PER_30D,
                    "upgrade": UPGRADE_INFO,
                }

        # digital vs scanned
        text = extract_text_digital(tmp_path) if redact_pii else _raw_text(tmp_path)
        if len((text or "").strip()) <= 100:
            rec.update(ok=False, blocked="scanned")
            _record_usage(rec)
            return {
                "ok": False,
                "error_code": "SCANNED_PDF_UNSUPPORTED",
                "message": "This PDF has no text layer (scanned/image). Free tier supports digital PDFs only.",
            }

        if redact_pii:
            tables = extract_tables_digital(tmp_path)
        else:
            tables = _extract_tables_raw(tmp_path)

        if not tables:
            result = {
                "ok": True,
                "type": "text",
                "pages": pages,
                "tables": 0,
                "text": text,
                "format": "text",
            }
            rec.update(ok=True, result="text")
            _record_usage(rec)
            return result

        df = tables_to_dataframe(tables)
        result = {
            "ok": True,
            "type": "tables",
            "pages": pages,
            "tables": len(tables),
            "rows": int(df.shape[0]),
            "columns": [str(c) for c in df.columns],
        }
        if output == "csv":
            result["format"] = "csv"
            result["csv"] = df.to_csv(index=False)
        elif output == "json":
            result["format"] = "json"
            result["rows_json"] = json.loads(df.to_json(orient="records", force_ascii=False))
        else:
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as xw:
                df.to_excel(xw, index=False, sheet_name="extracted")
            result["format"] = "xlsx"
            result["xlsx_base64"] = base64.b64encode(buf.getvalue()).decode()

        result["elapsed_ms"] = int((time.time() - started) * 1000)
        rec.update(ok=True, result="tables", rows=int(df.shape[0]))
        _record_usage(rec)
        return result
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _raw_text(path: str) -> str:
    with pdfplumber.open(path) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def _extract_tables_raw(path: str) -> list:
    out = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            for t in page.extract_tables():
                if t and len(t) > 1:
                    out.append({"page": i, "rows": [[str(c).strip() if c else "" for c in r] for r in t]})
    return out


# ---- entrypoint --------------------------------------------------------------
def main() -> None:
    transport = os.environ.get("REMOTE_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http", "sse"):
        host = os.environ.get("MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_PORT", "5060"))
        log.info("pdf-extract MCP over streamable-http on %s:%s", host, port)
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
