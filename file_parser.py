"""
File parser for .docx and .pdf attachments.

Extracts text and tables in a structured, pipe-separated format
so the Claude agent can parse module names, dates, and credits
from curriculum documents (навчальні плани).

Usage:
    from file_parser import parse_file
    text = parse_file(raw_bytes, filename="plan.docx", mimetype="application/vnd.openxmlformats-...")
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

MAX_CHARS = 12000  # agent context budget


# ── .docx ─────────────────────────────────────────────────────────────────────

def parse_docx(content: bytes, filename: str = "") -> str:
    """
    Extract paragraphs and tables from a .docx file.
    Tables are rendered as pipe-separated rows so the agent can
    read columns (Освітній компонент | Період викладання | Кредити…).
    """
    try:
        from docx import Document  # python-docx
        from docx.oxml.ns import qn

        doc = Document(io.BytesIO(content))
        parts: list[str] = []

        def cell_text(tc) -> str:
            return "".join(node.text or "" for node in tc.iter(qn("w:t"))).strip()

        for element in doc.element.body:
            tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag

            if tag == "p":
                text = "".join(
                    node.text or "" for node in element.iter(qn("w:t"))
                ).strip()
                if text:
                    parts.append(text)

            elif tag == "tbl":
                rows = element.findall(".//" + qn("w:tr"))
                table_lines: list[str] = []
                for row in rows:
                    cells = row.findall(".//" + qn("w:tc"))
                    row_cells = [cell_text(c) for c in cells]
                    if any(row_cells):
                        table_lines.append(" | ".join(row_cells))
                if table_lines:
                    parts.append("\n".join(table_lines))

        result = "\n\n".join(parts)
        return result[:MAX_CHARS]

    except ImportError:
        return (
            f"[.docx файл: {filename} — "
            "для читання docx встанови: pip install python-docx]"
        )
    except Exception as e:
        logger.error(f"parse_docx failed for {filename}: {e}", exc_info=True)
        return f"[Помилка при читанні .docx: {e}]"


# ── .pdf ──────────────────────────────────────────────────────────────────────

def parse_pdf(content: bytes, filename: str = "") -> str:
    """
    Extract text and tables from a PDF file.
    Uses pdfplumber: tries table extraction first, falls back to raw text.
    """
    try:
        import pdfplumber

        parts: list[str] = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                if tables:
                    for table in tables:
                        for row in table:
                            clean = [cell or "" for cell in row]
                            line = " | ".join(clean)
                            if line.strip(" |"):
                                parts.append(line)
                else:
                    text = page.extract_text()
                    if text:
                        parts.append(text)

        return "\n".join(parts)[:MAX_CHARS]

    except ImportError:
        return (
            f"[PDF файл: {filename} — "
            "для читання PDF встанови: pip install pdfplumber]"
        )
    except Exception as e:
        logger.error(f"parse_pdf failed for {filename}: {e}", exc_info=True)
        return f"[Помилка при читанні PDF: {e}]"


# ── dispatcher ────────────────────────────────────────────────────────────────

def parse_file(content: bytes, filename: str, mimetype: str) -> str:
    """
    Route a downloaded Slack file to the appropriate parser.

    Supported:
        .docx  application/vnd.openxmlformats-officedocument.wordprocessingml.document
        .doc   application/msword
        .pdf   application/pdf
        .txt / text/*
        .json  application/json
    """
    mt = mimetype.lower()
    fn = filename.lower()

    docx_mimetypes = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "application/octet-stream",  # Slack sometimes sends generic type
    }

    if mt in docx_mimetypes or fn.endswith(".docx") or fn.endswith(".doc"):
        return parse_docx(content, filename)

    if "pdf" in mt or fn.endswith(".pdf"):
        return parse_pdf(content, filename)

    if mt.startswith("text/") or mt == "application/json":
        try:
            return content.decode("utf-8", errors="replace")[:MAX_CHARS]
        except Exception as e:
            return f"[Помилка при читанні текстового файлу: {e}]"

    return (
        f"[Файл: {filename} — тип '{mimetype}' не підтримується для автоматичного читання. "
        "Підтримуються: .docx, .pdf, .txt, .json]"
    )
