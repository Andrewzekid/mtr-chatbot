from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class InspectionReportClient:
    """Loads and serves text from inspection reports in a directory.

    Supports plain-text files (.txt) and PDFs. The client reads every report
    it finds on first use and returns the combined text when a user question
    is about anomalies or inspection findings.
    """

    def __init__(self, reports_dir: str | Path) -> None:
        self.reports_dir = Path(reports_dir)
        self._context: str | None = None

    def _load_context(self) -> str:
        """Read all supported reports and concatenate their text."""
        if not self.reports_dir.exists():
            logger.warning("Reports directory does not exist: %s", self.reports_dir)
            return ""

        parts: list[str] = []
        for path in sorted(self.reports_dir.iterdir()):
            if not path.is_file():
                continue
            text = None
            if path.suffix.lower() == ".txt":
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    parts.append(f"--- {path.name} ---\n{text}")
                except OSError as exc:
                    logger.warning("Failed to read report %s: %s", path, exc)
            elif path.suffix.lower() == ".pdf":
                text = self._pdf_to_text(path)
                if text:
                    parts.append(f"--- {path.name} ---\n{text}")

        return "\n\n".join(parts)

    @staticmethod
    def _pdf_to_text(pdf_path: Path) -> str | None:
        """Extract text from a PDF using the system pdftotext tool."""
        if shutil.which("pdftotext") is None:
            logger.warning("pdftotext not available; skipping PDF report %s", pdf_path)
            return None

        try:
            with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            subprocess.run(
                ["pdftotext", str(pdf_path), str(tmp_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            text = tmp_path.read_text(encoding="utf-8", errors="ignore")
            tmp_path.unlink(missing_ok=True)
            return text
        except subprocess.CalledProcessError as exc:
            logger.warning("pdftotext failed for %s: %s", pdf_path, exc.stderr)
            return None
        except OSError as exc:
            logger.warning("Failed to read PDF text for %s: %s", pdf_path, exc)
            return None

    def get_context(self) -> str:
        if self._context is None:
            self._context = self._load_context()
        return self._context

    def get_image_urls(self) -> list[str]:
        """Return relative URLs for extracted anomaly images."""
        images_dir = self.reports_dir / "extracted_images"
        if not images_dir.exists():
            return []
        names = sorted(
            p.name for p in images_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )
        return [f"/reports/extracted_images/{name}" for name in names]

    def lookup(self, query: str | None = None) -> str | None:
        """Return the full report context, including anomaly images if available.

        The decision about *when* to inject report context is now made by the
        base LLM via the tool router (get_report_summary). This method simply
        packages the report text and image references so the LLM service can
        include it when the tool was selected.
        """
        context = self.get_context()
        if not context.strip():
            return None

        image_urls = self.get_image_urls()
        if image_urls:
            context += "\n\n--- Extracted anomaly images ---\n"
            context += "The following images are available for reference:\n"
            for url in image_urls:
                context += f"- {url}\n"

        return context
