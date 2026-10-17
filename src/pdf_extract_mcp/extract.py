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


# PII regex patterns — China + Western/international.
# Rules:
#   * Never use \b next to CJK: Han chars are \w, so \b does not fire between a
#     Han character and a digit. Use digit lookarounds (?<!\d)…(?!\d).
#   * Numeric patterns ordered long→short so an 18-digit CN ID is masked before
#     the 16–19 digit bank-card rule can swallow it.
PII_PATTERNS = {
    # --- international / Western ---
    "iban": re.compile(r'(?<![A-Za-z0-9])[A-Z]{2}\d{2}[A-Z0-9]{11,30}(?![A-Za-z0-9])'),  # IBAN
    "ssn": re.compile(r'(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)'),        # US SSN (dashed)
    "ein": re.compile(r'(?<!\d)\d{2}-\d{7}(?!\d)'),              # US EIN / TIN (dashed)
    "phone_us": re.compile(r'(?<!\d)(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}(?!\d)'),
    "phone_intl": re.compile(r'(?<![\d+])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){1,3}(?!\d)'),
    # --- China (id_card_18 MUST precede uscc & bank_card: both would otherwise
    #     swallow a bare 18-digit ID) ---
    "id_card_18": re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'),      # 身份证 18 位
    "uscc": re.compile(r'(?<![0-9A-HJ-NPQRTUWXY])[0-9A-HJ-NPQRTUWXY]{18}(?![0-9A-HJ-NPQRTUWXY])'),  # 统一社会信用代码
    "bank_card": re.compile(r'(?<!\d)\d{16,19}(?!\d)'),          # UnionPay + 国际卡号
    "card_spaced": re.compile(r'(?<!\d)(?:\d{4}[ \-]){3}\d{4}(?!\d)'),  # 4-4-4-4 卡号
    "id_card_15": re.compile(r'(?<!\d)\d{15}(?!\d)'),            # 旧版 15 位
    "phone_cn": re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'),         # 中国手机号
    "email": re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
    "passport": re.compile(r'(?<![A-Za-z0-9])[A-Z]\d{8}(?!\d)'), # 护照 1 字母 + 8 数字
    "name_2_3_cn": re.compile(r'[\u4e00-\u9fa5]{2,3}(?=[，。：\s\n]|$)'),  # 2-3 字中文姓名
}

# Western bank identifiers are ambiguous as bare numbers (a 9-digit routing or an
# 8-digit account could be any reference), so they are redacted ONLY when they
# follow a sensitive label. The gap allows words like "Number"/"Code" between the
# label and the value, and the value itself may contain dashes/spaces (e.g. a UK
# sort code "20-00-00"). (label+capture regex, number capture group)
KEYWORD_NUM_PATTERNS = [
    (re.compile(r'(?i)(\b(?:routing|aba|transit|sort[\s-]?code|swift|bic)\b[^\d\n]{0,18})([0-9][0-9 \-]{4,22}[0-9])'), 2),
    (re.compile(r'(?i)(\b(?:account|acct|a/c|iban)\b[^\d\n]{0,16})([A-Z]{0,2}[0-9][0-9 \-]{4,26}[0-9])'), 2),
]


def _mask_number_keep_tail(num: str, tail: int = 4) -> str:
    """Mask every digit except the last `tail`, preserving dashes/spaces."""
    digits = [c for c in num if c.isdigit()]
    keep_from = len(digits) - tail
    out, di = [], 0
    for c in num:
        if c.isdigit():
            out.append(c if di >= keep_from else "*")
            di += 1
        else:
            out.append(c)
    return "".join(out)

# 2026-08-29 升级: 常见中文单位/职务关键词 (脱敏真实单位名)
ORG_KEYWORDS = [
    "中共", "县委", "县委统战部", "统战部", "县委统一战线", "人民政府", "办公室",
    "委员会", "党组", "国务院", "省政府", "市委", "县政府", "县政协",
    "党支部", "团支部", "工商联", "侨联", "台办", "民宗局",
    "公司", "有限公司", "股份", "集团", "企业", "事务所", "中心",
    "医院", "学校", "大学", "中学", "小学", "学院",
    "银行", "支行", "营业部", "信用社",
]


def _mask_for(label: str, s: str) -> str:
    """Return the masked form for a matched PII string.

    China masks are kept byte-for-byte as before; Western masks keep the tail
    (last 4) so the value stays recognisable to its owner but unusable to others.
    """
    n = len(s)
    if label == "id_card_18":
        return s[:6] + "********" + s[14:] if n == 18 else s[:6] + "*********"
    if label == "id_card_15":
        return s[:6] + "*********"
    if label == "uscc":
        return s[:4] + "**********" + s[14:]
    if label == "phone_cn":
        return s[:3] + "****" + s[7:]
    if label == "email":
        local, _, dom = s.partition("@")
        return (local[:2] + "***@" + dom) if dom else "***"
    if label == "bank_card":
        return s[:4] + "********" + s[-4:]
    if label == "card_spaced":
        return "**** **** **** " + s[-4:]
    if label == "passport":
        return s[:1] + "********"
    if label == "name_2_3_cn":
        return "**"
    if label == "iban":
        return s[:4] + "*" * max(1, n - 8) + s[-4:]
    if label == "ssn":
        return "***-**-" + s[-4:]
    if label == "ein":
        return "**-*******"
    if label in ("phone_us", "phone_intl"):
        return ("*" * (n - 4) + s[-4:]) if n > 4 else "****"
    return "***"


def redact_pii(text: str) -> str:
    """Auto-mask PII: China (ID/phone/USCC) + Western (SSN/EIN/IBAN/phone/card)
    + email + bank identifiers behind labels. Org names are masked by keyword."""
    if not isinstance(text, str):
        return text
    for label, pattern in PII_PATTERNS.items():
        text = pattern.sub(lambda m, l=label: _mask_for(l, m.group(0)), text)
    # label-gated Western bank identifiers (routing / account / swift / sort code)
    for pat, grp in KEYWORD_NUM_PATTERNS:
        text = pat.sub(lambda m, g=grp: m.group(1) + _mask_number_keep_tail(m.group(g)), text)
    # 单位名: 用关键词匹配 + 整段 mask (粗略但有效, 后续可升级为 NER)
    for kw in ORG_KEYWORDS:
        text = text.replace(kw, "**[单位]")
    return text


HEADER_HINTS = {
    "date", "description", "details", "amount", "balance", "debit", "credit",
    "withdrawal", "deposit", "qty", "quantity", "price", "total", "item",
    "invoice", "reference", "type", "category", "memo", "payee", "vendor",
}


def _header_columns(page):
    """Find a table header line and return its cell x-ranges.

    Western statements frequently have no ruling lines; pdfplumber's generic text
    strategy then splits headers mid-word. Instead we locate the header row
    (>=3 known header words on one line) and use each label's own x0 as an
    explicit column boundary.
    """
    try:
        words = page.extract_words(use_text_flow=False)
    except Exception:
        return None
    if not words:
        return None
    lines = {}
    for w in words:
        lines.setdefault(round(w["top"] / 3) * 3, []).append(w)

    target = None
    target_top = None
    for top in sorted(lines):
        ws = sorted(lines[top], key=lambda w: w["x0"])
        hits = sum(
            1 for w in ws
            if w["text"].strip(":").lower() in HEADER_HINTS
            or any(h in w["text"].lower() for h in ("description", "amount", "balance", "date"))
        )
        if hits >= 3:
            target = ws
            target_top = top
            break
    if not target:
        return None

    # merge labels that are visually one cell (e.g. "Unit" + "Price")
    cols = []
    for w in target:
        if cols and (w["x0"] - cols[-1]["x1"]) < 8:
            cols[-1]["x1"] = w["x1"]
            cols[-1]["text"] = cols[-1]["text"] + " " + w["text"]
        else:
            cols.append({"x0": w["x0"], "x1": w["x1"], "text": w["text"]})
    if len(cols) < 3:
        return None
    return cols, target_top


def extract_tables_digital(pdf_path: str) -> list:
    """Extract tables from digital (text-based) PDF using pdfplumber.

    Strategy order per page:
      1. default rule/line strategy (accurate when the page is drawn with lines)
      2. explicit vertical lines derived from the header row's own x positions
         (handles statements with text alignment but no rules — the common case
         for Western bank statements)
      3. generic text-alignment strategy as a last resort
    """
    tables = []
    TEXT_SETTINGS = {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "min_words_vertical": 3,
        "min_words_horizontal": 1,
        "intersection_tolerance": 5,
        "snap_tolerance": 3,
    }
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            page_tables = page.extract_tables()
            if not page_tables:
                found = _header_columns(page)
                if found:
                    cols, header_top = found
                    header_top = header_top or 0
                    try:
                        # Crop from the header row downward: header paragraphs often
                        # hold the account/SSN lines, and if those entered the table
                        # a column boundary could split the number and defeat the
                        # per-cell redaction.
                        region = page.crop((0, max(0, header_top - 4), page.width, page.height))
                        # Right edge = rightmost text extent (+pad). A line at the
                        # page edge itself gets dropped by pdfplumber, which would
                        # silently lose the last column.
                        try:
                            _words = page.extract_words()
                            _right = max((w["x1"] for w in _words), default=page.width - 2) + 6
                        except Exception:
                            _right = page.width - 6
                        _right = min(_right, page.width - 2)
                        vlines = [c["x0"] for c in cols] + [_right]
                        page_tables = region.extract_tables(table_settings={
                            "vertical_strategy": "explicit",
                            "explicit_vertical_lines": vlines,
                            "horizontal_strategy": "text",
                            "intersection_tolerance": 6,
                            "snap_tolerance": 4,
                        })
                        # Replace the (possibly fragmented) header with header labels
                        fixed = []
                        for tbl in page_tables:
                            if not tbl:
                                continue
                            rows = [list(r) for r in tbl]
                            if rows and len(cols) == len(rows[0]):
                                rows[0] = [c["text"] for c in cols]
                            fixed.append(rows)
                        if fixed:
                            page_tables = fixed
                    except Exception:
                        page_tables = []
            if not page_tables:
                try:
                    page_tables = page.extract_tables(table_settings=TEXT_SETTINGS)
                except Exception:
                    page_tables = []
            for table in page_tables:
                # drop rows where every cell is empty (artifacts of text strategy)
                rows = [r for r in table if r and any((c or "").strip() for c in r)]
                if len(rows) > 1:  # at least header + 1 data row
                    redacted = [[redact_pii(str(cell).strip()) if cell else "" for cell in row] for row in rows]
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
