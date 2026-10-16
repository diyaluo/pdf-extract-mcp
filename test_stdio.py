"""End-to-end stdio test: spawn the MCP server over stdio, list tools,
call health, then extract a real sample PDF."""
import asyncio, json, sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path("/root/data/disk/projects/invoice-extractor")
SRC = ROOT / "mcp-server/src"
VENV_PY = str(ROOT / ".venv/bin/python")

async def main():
    params = StdioServerParameters(
        command=VENV_PY,
        args=["-m", "pdf_extract_mcp.server"],
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])

            h = await s.call_tool("health", {})
            print("HEALTH:", h.content[0].text[:300])

            # find a real DIGITAL sample PDF (skip scanned)
            import pdfplumber as _pp
            samples = []
            if (ROOT / "samples").exists():
                for p in (ROOT / "samples").rglob("*.pdf"):
                    try:
                        with _pp.open(p) as pdf:
                            if len((pdf.pages[0].extract_text() or "")) > 100:
                                samples.append(p)
                    except Exception:
                        pass
            print("DIGITAL SAMPLES:", [p.name for p in samples][:5])
            if samples:
                import base64
                b64 = base64.b64encode(samples[0].read_bytes()).decode()
                res = await s.call_tool("extract_pdf", {"pdf_base64": b64, "output": "json"})
                txt = res.content[0].text
                d = json.loads(txt)
                print("EXTRACT ok=", d.get("ok"), "type=", d.get("type"),
                      "pages=", d.get("pages"), "tables=", d.get("tables"),
                      "rows=", d.get("rows"), "cols=", str(d.get("columns"))[:120])
                if d.get("ok") and d.get("rows_json"):
                    print("FIRST ROW:", json.dumps(d["rows_json"][0], ensure_ascii=False)[:200])
                if not d.get("ok"):
                    print("DETAIL:", txt[:400])

asyncio.run(main())
