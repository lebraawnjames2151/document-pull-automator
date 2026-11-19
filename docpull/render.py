"""Save EDGAR documents to disk as PDF (via headless Chromium) or as the original file."""

from __future__ import annotations

import html as htmlmod
import re
from pathlib import Path

from .edgar import EdgarClient, decode

_HEAD = re.compile(r"<head[^>]*>", re.I)


# Scanned exhibits are often full-page images wider than a printed page.
_PRINT_CSS = "<style>img{width:auto;height:auto;max-width:100%;max-height:9.6in}</style>"


def _with_base(html: str, base_url: str) -> str:
    """Make relative image links resolve against the SEC folder the doc came from."""
    tag = f'<base href="{base_url}">{_PRINT_CSS}'
    if _HEAD.search(html):
        return _HEAD.sub(lambda m: m.group(0) + tag, html, count=1)
    return tag + html


class Renderer:
    def __init__(self, client: EdgarClient, fmt: str = "pdf"):
        if fmt not in ("pdf", "html"):
            raise ValueError("fmt must be 'pdf' or 'html'")
        self.client = client
        self.fmt = fmt
        self._pw = None
        self._browser = None
        self._context = None

    def _page(self):
        if self._context is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
            self._context = self._browser.new_context(user_agent=self.client.user_agent)
            # Chromium would fire hundreds of image requests at once and get throttled by SEC.
            # Route them through the rate-limited client instead.
            self._context.route("https://www.sec.gov/**", self._fetch_via_client)
        return self._context.new_page()

    def _fetch_via_client(self, route) -> None:
        try:
            r = self.client.get(route.request.url)
            route.fulfill(status=200, body=r.content,
                          headers={"content-type": r.headers.get("content-type", "application/octet-stream")})
        except Exception:
            route.abort()

    def target_path(self, url: str, dest_stem: Path) -> Path:
        ext = Path(url.split("?")[0]).suffix.lower() or ".htm"
        if ext == ".pdf" or self.fmt == "pdf":
            return dest_stem.with_name(dest_stem.name + ".pdf")
        return dest_stem.with_name(dest_stem.name + (".txt" if ext == ".txt" else ".html"))

    def save(self, url: str, dest_stem: Path) -> Path:
        path = self.target_path(url, dest_stem)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = self.client.get_bytes(url)
        is_txt = url.lower().endswith(".txt")

        if url.lower().endswith(".pdf"):
            path.write_bytes(raw)
            return path

        base = url.rsplit("/", 1)[0] + "/"
        text = decode(raw)
        if self.fmt == "html":
            path.write_text(text if is_txt else _with_base(text, base), encoding="utf-8")
            return path

        if is_txt:
            text = (
                '<html><body><pre style="white-space:pre-wrap;font:9pt monospace">'
                + htmlmod.escape(text)
                + "</pre></body></html>"
            )
        page = self._page()
        try:
            page.set_content(_with_base(text, base), wait_until="load", timeout=180_000)
            page.pdf(
                path=str(path),
                format="Letter",
                print_background=True,
                margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
            )
        finally:
            page.close()
        return path

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
