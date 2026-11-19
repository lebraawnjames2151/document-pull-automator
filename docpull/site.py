"""Find documents on a company's own investor-relations website.

Used after EDGAR, mainly for Canadian issuers: transcripts, presentations and debt documents
that are not on EDGAR. Crawls politely: same domain only, robots.txt respected, 1 request/s,
bounded page count.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from . import classify as cls
from .edgar import EdgarError, HttpClient, decode

TRANSCRIPTS = "Transcripts"
PRESENTATIONS = "Presentations"
AIF = "Annual Information Form"
MDA = "MD&A"
FINANCIALS = "Financial Statements"
PRESS = "Press Releases"
CIRCULARS = "Circulars"
ANNUAL_REPORTS = "Annual Reports"
# Report categories, only pulled from the website when the company is not on EDGAR.
REPORTS = (AIF, MDA, FINANCIALS, PRESS, CIRCULARS, ANNUAL_REPORTS)
_REPORT_RULES = [
    (re.compile(r"\baif\b|annual information form", re.I), AIF),
    (re.compile(r"\bmd ?& ?a\b|\bmda\b|management'?s? discussion", re.I), MDA),
    (re.compile(r"press release|news release", re.I), PRESS),
    (re.compile(r"\bfs\b|financial statements|financials\b", re.I), FINANCIALS),
    (re.compile(r"circular|\bmic\b", re.I), CIRCULARS),
    (re.compile(r"annual ?report", re.I), ANNUAL_REPORTS),
]

# Sites a filing mentions that are not the company's own.
_NOT_COMPANY = re.compile(
    r"sedar|sec\.gov|securities-administrators|osc\.ca|bcsc|albertasecurities|lautorite|tsx|nyse|nasdaq"
    r"|computershare|tsxtrust|astfinancial|odyssey|broadridge|proxy|canada\.ca|gc\.ca|gov\.",
    re.I,
)
_WWW = re.compile(r"\bwww\.([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.(?:com|ca|net|org|co|us))\b", re.I)
# Pages worth following from an IR section.
_IR_PATH = re.compile(
    r"investor|/ir\b|/ir/|financial|report|result|event|presentation|webcast|debt|credit|fixed-income"
    r"|bond|shareholder|disclosure|filing|governance|quarterly|annual",
    re.I,
)
_DOC_EXT = (".pdf",)
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def website_from_text(text: str) -> str | None:
    """Company website as most often mentioned in a filing (e.g. the AIF), or None."""
    hits = Counter(m.group(1).lower() for m in _WWW.finditer(text) if not _NOT_COMPANY.search(m.group(1)))
    return f"https://www.{hits.most_common(1)[0][0]}/" if hits else None


@dataclass
class SiteDocument:
    url: str
    text: str  # link text
    category: str
    year: int | None

    @property
    def name(self) -> str:
        """Link text if it says what the document is, else the file name."""
        file_stem = unquote(urlparse(self.url).path.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
        text = re.sub(r"\[.*?\]|read more|click here|/", " ", self.text, flags=re.I)
        text = re.sub(r"^(download|view|see)\s+(the\s+)?", "", re.sub(r"\s+", " ", text).strip(), flags=re.I)
        good = text and _YEAR.search(text) and len(text) <= 120
        return cls.safe_filename(text if good else file_stem.replace("-", " ").replace("_", " "))

    @property
    def recency(self) -> tuple[int, int]:
        """(year, upload yyyymm) for picking the latest of several documents."""
        m = re.search(r"/(20\d{2})/(\d{2})/", self.url)
        return (self.year or 0, int(m.group(1) + m.group(2)) if m else 0)


def categorize(link_text: str, url: str, reports: bool = False) -> str | None:
    """Category from link text and file name. Folder names are ignored: a /presentations/
    folder holds all sorts of files. `reports` adds AIF, MD&A, financials, press releases and
    circulars, for companies whose reports are not on EDGAR."""
    file_name = re.sub(r"[-_.]+", " ", unquote(urlparse(url).path.rsplit("/", 1)[-1]))
    hay = f"{link_text} {file_name}"
    if re.search(r"transcript", hay, re.I):
        return TRANSCRIPTS
    debt = cls.classify(link_text) or cls.classify(file_name)
    if debt:
        return debt
    if reports:
        for rx, category in _REPORT_RULES:
            if rx.search(hay):
                return category
    if re.search(r"presentation|investor day|appendix|earnings slides|webcast", hay, re.I):
        return PRESENTATIONS
    return None


_FY = re.compile(r"\bFY ?'?(\d{2})(?!\d)", re.I)


def _year(link_text: str, url: str) -> int | None:
    """Year the document is about: from its name or link text ('Q1 2021', 'FY24'). The folder
    path (e.g. /uploads/2023/06/) is only a fallback, since sites re-upload old documents."""
    name = f"{link_text} {unquote(urlparse(url).path.rsplit('/', 1)[-1])}".replace("_", " ")
    years = [int(y) for y in _YEAR.findall(name)] + [2000 + int(y) for y in _FY.findall(name)]
    if not years:
        years = [int(y) for y in _YEAR.findall(unquote(urlparse(url).path))]
    return max(years) if years else None


def _year_variants(url: str, years: range | None) -> list[str]:
    """IR pages often filter by year with a query parameter (?financial_year=2026) and offer the
    other years in a dropdown rather than as links. Return the page without the query and with
    each year in `years`."""
    parsed = urlparse(url)
    if not parsed.query:
        return []
    out = [parsed._replace(query="").geturl()]
    params = parse_qsl(parsed.query)
    for i, (key, value) in enumerate(params):
        if years and re.fullmatch(r"(?:19|20)\d{2}", value):
            for y in years:
                variant = params[:i] + [(key, str(y))] + params[i + 1:]
                out.append(parsed._replace(query=urlencode(variant)).geturl())
    return out


def crawl(client: HttpClient, start_url: str, max_pages: int = 80, reports: bool = False,
          years: range | None = None, log=print) -> list[SiteDocument]:
    """Breadth-first crawl of the IR section of `start_url`'s domain (subdomains included)."""
    domain = urlparse(start_url).netloc.removeprefix("www.")
    domain = ".".join(domain.split(".")[-3:]) if domain.endswith((".co.uk", ".com.au")) else \
        ".".join(domain.split(".")[-2:])
    robots_by_host: dict[str, RobotFileParser] = {}

    def allowed(u: str) -> bool:
        host = urlparse(u).netloc
        if host not in robots_by_host:
            rp = RobotFileParser()
            try:
                rp.parse(decode(client.get_bytes(f"https://{host}/robots.txt")).splitlines())
            except Exception:  # no robots.txt (404) means no restrictions
                rp.parse([])
            robots_by_host[host] = rp
        return robots_by_host[host].can_fetch(client.user_agent, u)

    def same_site(u: str) -> bool:
        host = urlparse(u).netloc
        return host == domain or host.endswith("." + domain)

    queue: list[tuple[str, int]] = [(start_url, 0)]
    seen: set[str] = set()
    docs: dict[str, SiteDocument] = {}
    while queue and len(seen) < max_pages:
        url, depth = queue.pop(0)
        if url in seen or not allowed(url):
            continue
        seen.add(url)
        try:
            r = client.get(url)
        except Exception:
            continue
        if "html" not in r.headers.get("content-type", ""):
            continue
        soup = BeautifulSoup(decode(r.content), "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"].strip()).split("#")[0].replace("http://", "https://", 1)
            text = a.get_text(" ", strip=True)
            if urlparse(href).path.lower().endswith(_DOC_EXT):
                cat = categorize(text, href, reports)
                if cat and href not in docs:
                    docs[href] = SiteDocument(href, text, cat, _year(text, href))
                elif cat and _year(text, href) and not docs[href].year:
                    docs[href] = SiteDocument(href, text, cat, _year(text, href))
            elif same_site(href) and href not in seen and depth < 3:
                # From the home page follow only IR-looking links; inside the IR section follow more.
                if _IR_PATH.search(urlparse(href).path) or _IR_PATH.search(text):
                    queue.append((href, depth + 1))
                    queue.extend((v, depth + 1) for v in _year_variants(href, years) if v not in seen)
    log(f"  company site {start_url}: {len(seen)} pages read, {len(docs)} documents matched")
    return list(docs.values())


# --------------------------------------------------------------------------- AIF material contracts

_MC_HEADING = re.compile(r"MATERIAL CONTRACTS", re.I)
_MC_END = re.compile(
    r"\b(?:NAMES? AND )?INTERESTS OF EXPERTS\b|\bEXPERTS\b|\bADDITIONAL INFORMATION\b|\bAUDIT COMMITTEE\b"
    r"|\bTRANSFER AGENTS?\b|\bLEGAL PROCEEDINGS\b|\bSCHEDULE [A-Z]\b|\bINTEREST OF MANAGEMENT\b",
)
_BULLET = re.compile(r"\s*(?:[■•●▪◦]|\(\s*[a-z0-9]{1,3}\s*\)|\b\d{1,2}\.\s)\s*")
_PAGE_FOOTER = re.compile(r"\s*\S.{0,60}?\d{1,3}\s+\d{4} Annual Information Form\s*", re.I)


def material_contracts(aif_text: str) -> list[str]:
    """Items listed in an AIF's 'Material Contracts' section (Form 51-102F2, Item 15).

    The section lists every contract that is material and still in effect, including
    indentures and credit agreements, so it is the checklist for what should be found.
    """
    starts = [m for m in _MC_HEADING.finditer(aif_text) if m.group(0).isupper()]
    # Skip the table of contents: the real section is followed by prose, not a page number.
    body = None
    for m in starts:
        nxt = aif_text[m.end(): m.end() + 200]
        if not re.match(r"\s*(?:\.{3,}|…)?\s*\d{1,3}\b", nxt) and "...." not in nxt:
            body = aif_text[m.end():]
            break
    if body is None:
        return []
    end = _MC_END.search(body[50:])
    section = body[: (end.start() + 50) if end else 8000]
    section = _PAGE_FOOTER.sub(" ", section)
    parts = _BULLET.split(section)
    if len(parts) > 1:
        parts = parts[1:]  # text before the first bullet is the section's intro sentence
    items = []
    for part in parts:
        part = re.split(r"\.\s+(?:Copies|These|Each|All|Additional|Such)\b", part, maxsplit=1)[0]
        part = re.sub(r"[;,]?\s*(?:and|or)?\s*$", "", part.strip())
        part = re.split(r"\(\s*See\b", part, maxsplit=1, flags=re.I)[0]
        part = re.sub(r"\s+", " ", part).strip(" ;.,")
        if len(part) > 15 and re.search(r"agreement|indenture|contract|arrangement|facility|note", part, re.I):
            items.append(part)
    # The intro sentence ("The following are the only contracts ...") is not an item.
    return [i for i in items if not re.match(r"(the following|other than|except)", i, re.I)]


def pdf_text(data: bytes) -> str:
    """Plain text of a PDF, whitespace collapsed."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return re.sub(r"\s+", " ", " ".join((page.extract_text() or "") for page in reader.pages))
