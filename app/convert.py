"""Converts DOCX/PPTX attachments to PDF with headless LibreOffice so the model can read them.

Needs `soffice` on PATH (or LibreOffice in its default install folder); returns None when it's
missing so prep.py falls back to description text only.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

OFFICE = {".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx", ".odp", ".xls", ".xlsx", ".ods"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
TEXT = {".txt", ".md", ".csv"}

_INSTALL_PATHS = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]

# A profile of our own: with the default one, an open LibreOffice window swallows the
# conversion and soffice exits without writing the PDF.
_PROFILE = Path(tempfile.gettempdir()) / "start-helper-lo-profile"


def find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        if found := shutil.which(name):
            return found
    return next((p for p in _INSTALL_PATHS if Path(p).exists()), None)


def to_pdf(path: Path) -> Path | None:
    """Return the PDF path (PDFs pass through unchanged), or None if conversion isn't possible."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return path
    if suffix not in OFFICE:
        return None
    out = path.with_suffix(".pdf")
    if out.exists():
        return out
    soffice = find_soffice()
    if not soffice:
        return None
    try:
        subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={_PROFILE.as_uri()}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(path.parent),
                str(path),
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out if out.exists() else None


def model_readable(path: Path) -> Path | None:
    """A file the model can take directly (PDF, image, plain text), converting office docs. Else None."""
    suffix = path.suffix.lower()
    if suffix in IMAGES or suffix in TEXT:
        return path
    return to_pdf(path)
