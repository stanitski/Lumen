"""Text extraction helpers for uploaded document attachments."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import zipfile
from xml.etree import ElementTree


class FileTextExtractionError(ValueError):
    """Raised when an attachment cannot be converted into useful text."""


class FileTextExtractor:
    _TEXT_EXTENSIONS = {
        ".csv",
        ".htm",
        ".html",
        ".ini",
        ".json",
        ".log",
        ".md",
        ".markdown",
        ".rst",
        ".toml",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
    _DOCX_CONTENT_TYPES = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    }
    _XLSX_CONTENT_TYPES = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    }
    _PPTX_CONTENT_TYPES = {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
    }

    @classmethod
    def supported_text_extensions(cls) -> set[str]:
        return set(cls._TEXT_EXTENSIONS)

    @classmethod
    def infer_kind(cls, filename: str | None, content_type: str | None) -> str:
        safe_filename = (filename or "").strip().lower()
        safe_content_type = (content_type or "").strip().lower()
        suffix = Path(safe_filename).suffix.lower()
        if safe_content_type.startswith("text/") or suffix in cls._TEXT_EXTENSIONS:
            return "text"
        if safe_content_type == "application/pdf" or suffix == ".pdf":
            return "pdf"
        if safe_content_type in cls._DOCX_CONTENT_TYPES or suffix == ".docx":
            return "docx"
        if safe_content_type in cls._XLSX_CONTENT_TYPES or suffix == ".xlsx":
            return "xlsx"
        if safe_content_type in cls._PPTX_CONTENT_TYPES or suffix == ".pptx":
            return "pptx"
        if safe_content_type.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
            return "image"
        if safe_content_type.startswith("video/") or suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}:
            return "video"
        return "unsupported"

    @staticmethod
    def decode_text(raw: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8", "cp1251", "cp866", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @classmethod
    def extract_text(cls, *, filename: str, content_type: str | None, raw: bytes) -> tuple[str, str]:
        kind = cls.infer_kind(filename, content_type)
        if kind == "text":
            return kind, cls.decode_text(raw)
        if kind == "pdf":
            return kind, cls._extract_pdf(raw)
        if kind == "docx":
            return kind, cls._extract_docx(raw)
        if kind == "xlsx":
            return kind, cls._extract_xlsx(raw)
        if kind == "pptx":
            return kind, cls._extract_pptx(raw)
        raise FileTextExtractionError(f"Unsupported file type for text extraction: {filename}")

    @classmethod
    def can_extract(cls, *, filename: str, content_type: str | None) -> bool:
        return cls.infer_kind(filename, content_type) in {"text", "pdf", "docx", "xlsx", "pptx"}

    @staticmethod
    def _normalize_text(parts: list[str]) -> str:
        text = "\n".join(part.strip() for part in parts if part and part.strip())
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _xml_text(xml_bytes: bytes) -> str:
        root = ElementTree.fromstring(xml_bytes)
        return "".join(root.itertext())

    @classmethod
    def _extract_docx(cls, raw: bytes) -> str:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            names = sorted(
                name
                for name in archive.namelist()
                if name == "word/document.xml" or name.startswith("word/header") or name.startswith("word/footer")
            )
            parts = [cls._xml_text(archive.read(name)) for name in names]
        text = cls._normalize_text(parts)
        if not text:
            raise FileTextExtractionError("No text was found in the DOCX file.")
        return text

    @classmethod
    def _extract_xlsx(cls, raw: bytes) -> str:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            shared_strings = cls._read_xlsx_shared_strings(archive)
            worksheet_names = sorted(name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
            rows: list[str] = []
            for worksheet_name in worksheet_names:
                sheet_rows = cls._read_xlsx_sheet(archive.read(worksheet_name), shared_strings)
                if sheet_rows:
                    rows.append(Path(worksheet_name).stem)
                    rows.extend(sheet_rows)
        text = cls._normalize_text(rows)
        if not text:
            raise FileTextExtractionError("No text was found in the XLSX file.")
        return text

    @classmethod
    def _read_xlsx_shared_strings(cls, archive: zipfile.ZipFile) -> list[str]:
        try:
            raw = archive.read("xl/sharedStrings.xml")
        except KeyError:
            return []
        root = ElementTree.fromstring(raw)
        return [cls._normalize_text(["".join(item.itertext())]) for item in root]

    @staticmethod
    def _xml_local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    @classmethod
    def _read_xlsx_sheet(cls, raw: bytes, shared_strings: list[str]) -> list[str]:
        root = ElementTree.fromstring(raw)
        rows: list[str] = []
        for row in root.iter():
            if cls._xml_local_name(row.tag) != "row":
                continue
            values: list[str] = []
            for cell in row:
                if cls._xml_local_name(cell.tag) != "c":
                    continue
                cell_type = cell.attrib.get("t", "")
                value_text = ""
                for child in cell:
                    child_name = cls._xml_local_name(child.tag)
                    if child_name == "v" and child.text is not None:
                        value_text = child.text
                        break
                    if child_name == "is":
                        value_text = "".join(child.itertext())
                        break
                if cell_type == "s":
                    try:
                        value_text = shared_strings[int(value_text)]
                    except (ValueError, IndexError):
                        pass
                if value_text.strip():
                    values.append(value_text.strip())
            if values:
                rows.append(" | ".join(values))
        return rows

    @classmethod
    def _extract_pptx(cls, raw: bytes) -> str:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            slide_names = sorted(name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
            parts = [cls._xml_text(archive.read(name)) for name in slide_names]
        text = cls._normalize_text(parts)
        if not text:
            raise FileTextExtractionError("No text was found in the PPTX file.")
        return text

    @staticmethod
    def _extract_pdf(raw: bytes) -> str:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise FileTextExtractionError("PDF text extraction requires the pypdf package.") from exc

        reader = PdfReader(BytesIO(raw))
        parts = [page.extract_text() or "" for page in reader.pages]
        text = FileTextExtractor._normalize_text(parts)
        if not text:
            raise FileTextExtractionError("No text was found in the PDF file.")
        return text
