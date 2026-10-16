"""
PDF Invoice / Bank Statement Extractor
=====================================
Reads PDF/Image → extracts table data → outputs CSV/Excel

Usage:
    python extract.py input.pdf output.csv
    python extract.py input.pdf output.xlsx
    python extract.py --batch inputs/  outputs/

Architecture:
    1. pdfplumber extracts tables (digital PDFs)
    2. OpenAI Vision API fallback for scanned PDFs
    3. Output: CSV (default) or Excel
    4. PII redaction: auto-mask ID numbers, phone, credit codes
"""
import re
import sys
import os
import argparse
import json
from pathlib import Path
import pdfplumber  # noqa: E402

# pandas is intentionally NOT bundled (it + numpy/openblas would blow Smithery's
# 25 MB MCPB limit). Tables are plain list-of-rows; we serialize CSV/JSON/XLSX with
# the stdlib + openpyxl. A pandas-like shim keeps server.py unchanged.
try:  # optional, only present in the full project venv (never in the MCPB bundle)
    import pandas as pd  # type: ignore
except Exception:  # pragma: no cover
    pd = None


# PII regex patterns (China-specific + generic)
# 2026-08-29 升级: 加中文姓名 + 单位关键词 (防真实姓名泄露)
PII_PATTERNS = {
    # 注意：中文与数字之间 \b 不构成边界（中文也是 \w），故一律用环视断言
    # (?<!\d)…(?!\d) 锁定独立数字串，避免长号被短模式截断、也避免中文邻接漏匹配。
    # 顺序敏感：先长后短（18位身份证 → 银行卡 → 15位身份证 → 手机）。
    "id_card_18": re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'),          # 身份证 18 位
    "bank_card": re.compile(r'(?<!\d)\d{16,19}(?!\d)'),             # 银行卡 16-19 位
    "id_card_15": re.compile(r'(?<!\d)\d{15}(?!\d)'),               # 旧版 15 位
    "uscc": re.compile(r'(?<![0-9A-HJ-NPQRTUWXY])[0-9A-HJ-NPQRTUWXY]{18}(?![0-9A-HJ-NPQRTUWXY])'),  # 统一社会信用代码
    "phone": re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'),               # 中国手机号
    "email": re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
    "passport": re.compile(r'(?<![A-Za-z0-9])[A-Z]\d{8}(?!\d)'),    # 护照 1 字母 + 8 数字
    "name_2_3_cn": re.compile(r'[\u4e00-\u9fa5]{2,3}(?=[，。：\s\n]|$)'),  # 2-3 字中文姓名
}

# 2026-08-29 升级: 常见中文单位/职务关键词 (脱敏真实单位名)
ORG_KEYWORDS = [
    "中共", "县委", "县委统战部", "统战部", "县委统一战线", "人民政府", "办公室",
    "委员会", "党组", "国务院", "省政府", "市委", "县政府", "县政协",
    "党支部", "团支部", "工商联", "侨联", "台办", "民宗局",
    "公司", "有限公司", "股份", "集团", "企业", "事务所", "中心",
    "医院", "学校", "大学", "中学", "小学", "学院",
    "银行", "支行", "营业部", "信用社",
]


def redact_pii(text: str) -> str:
    """Auto-mask PII (ID, phone, USCC, email, bank card, name, org)."""
    if not isinstance(text, str):
        return text
    for label, pattern in PII_PATTERNS.items():
        def mask(match, l=label):
            s = match.group(0)
            if l == "id_card_18" or l == "id_card_15":
                return s[:6] + "********" + s[14:] if len(s) == 18 else s[:6] + "*********"
            if l == "uscc":
                return s[:4] + "**********" + s[14:]
            if l == "phone":
                return s[:3] + "****" + s[7:]
            if l == "email":
                parts = s.split("@")
                return parts[0][:2] + "***@" + parts[1]
            if l == "bank_card":
                return s[:4] + "********" + s[-4:]
            if l == "passport":
                return s[:1] + "********"
            if l == "name_2_3_cn":
                return "**"  # 中文姓名整体 mask
            return "***"
        text = pattern.sub(mask, text)
    # 单位名: 用关键词匹配 + 整段 mask (粗略但有效, 后续可升级为 NER)
    for kw in ORG_KEYWORDS:
        text = text.replace(kw, "**[单位]")
    return text


def extract_tables_digital(pdf_path: str) -> list:
    """Extract tables from digital (text-based) PDF using pdfplumber."""
    tables = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            page_tables = page.extract_tables()
            for table in page_tables:
                if table and len(table) > 1:  # at least 2 rows
                    # Redact PII in each cell
                    redacted = [[redact_pii(str(cell).strip()) if cell else "" for cell in row] for row in table]
                    tables.append({
                        "page": page_num,
                        "rows": redacted,
                    })
    return tables


def extract_text_digital(pdf_path: str) -> str:
    """Extract all text from digital PDF, with PII redaction."""
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""
            text += "\n"
    return redact_pii(text)


class _RowsTable:
    """Minimal pandas-DataFrame stand-in over list-of-rows (MCPB, no numpy)."""

    def __init__(self, columns, rows):
        self.columns = list(columns)
        self._rows = [list(r) for r in rows]

    @property
    def shape(self):
        return (len(self._rows), len(self.columns))

    def _normalize(self, row):
        r = list(row)
        if len(r) < len(self.columns):
            r += [""] * (len(self.columns) - len(r))
        return r[: len(self.columns)]

    def to_csv(self, index=False):
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(self.columns)
        for r in self._rows:
            w.writerow(self._normalize(r))
        return buf.getvalue()

    def to_json(self, orient="records", force_ascii=False):
        import json
        out = [
            dict(zip(self.columns, self._normalize(r))) for r in self._rows
        ]
        return json.dumps(out, ensure_ascii=force_ascii)

    def to_excel(self, writer, index=False, sheet_name="extracted"):
        from openpyxl.utils import get_column_letter
        ws = writer.book.create_sheet(sheet_name) if hasattr(writer, "book") else writer
        ws.append(self.columns)
        for r in self._rows:
            ws.append(self._normalize(r))


def tables_to_dataframe(tables: list):
    """Merge extracted tables into one table (DataFrame if pandas present, else shim)."""
    all_rows = []
    for t in tables:
        for row in t["rows"]:
            all_rows.append([str(cell).strip() if cell else "" for cell in row])
    if not all_rows:
        return _RowsTable([], []) if pd is None else pd.DataFrame()

    columns, rows = all_rows[0], all_rows[1:]
    if pd is not None:
        return pd.DataFrame(rows, columns=columns)
    return _RowsTable(columns, rows)


def detect_format(pdf_path: str) -> str:
    """Detect if PDF is digital (text-based) or scanned (image-based)."""
    text = extract_text_digital(pdf_path)
    return "digital" if len(text.strip()) > 100 else "scanned"


def process_pdf(pdf_path: str, output_path: str = None, format: str = "csv"):
    """Main processing function."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        print(f"❌ File not found: {pdf_path}")
        return None

    print(f"📄 Processing: {pdf_path.name}")

    fmt = detect_format(str(pdf_path))
    print(f"   Format detected: {fmt}")

    if fmt == "digital":
        tables = extract_tables_digital(str(pdf_path))
        print(f"   Tables found: {len(tables)}")
        print(f"   PII: auto-redacted ✅")

        if not tables:
            text = extract_text_digital(str(pdf_path))
            print(f"   Text length: {len(text)} chars (PII redacted)")
            return {"type": "text", "content": text}

        df = tables_to_dataframe(tables)
        print(f"   DataFrame shape: {df.shape}")

        if output_path is None:
            output_path = pdf_path.with_suffix(f".{format}")

        if format == "xlsx":
            df.to_excel(output_path, index=False)
        else:
            df.to_csv(output_path, index=False)

        print(f"   ✅ Saved to: {output_path}")
        return {"type": "tables", "dataframe": df, "path": str(output_path)}
    else:
        print("   ⚠️  Scanned PDF - OCR not implemented yet")
        return {"type": "scanned", "path": str(pdf_path)}


def main():
    parser = argparse.ArgumentParser(description="PDF Invoice Extractor with PII redaction")
    parser.add_argument("input", help="Input PDF file or directory")
    parser.add_argument("output", nargs="?", help="Output file path")
    parser.add_argument("--format", choices=["csv", "xlsx"], default="csv")
    parser.add_argument("--batch", action="store_true", help="Batch mode")

    args = parser.parse_args()
    input_path = Path(args.input)

    if args.batch or input_path.is_dir():
        pdf_files = list(input_path.glob("*.pdf"))
        print(f"📂 Batch mode: found {len(pdf_files)} PDFs")
        for pdf in pdf_files:
            output = pdf.with_suffix(f".{args.format}")
            process_pdf(str(pdf), str(output), args.format)
    else:
        process_pdf(str(input_path), args.output, args.format)


if __name__ == "__main__":
    main()
