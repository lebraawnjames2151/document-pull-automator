"""Find documents on a company's own investor-relations website.

Used after EDGAR, mainly for Canadian issuers: transcripts, presentations and debt documents
that are not on EDGAR. Crawls politely: same domain only, robots.txt respected, 1 request/s,
bounded page count.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from . import classify as cls
from .edgar import EdgarError, HttpClient, decode

TRANSCRIPTS = "Transcripts"
PRESENTATIONS = "Presentations"

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


def categorize(link_text: str, url: str) -> str | None:
    # Link text and file name only: folder names like /presentations/ hold all sorts of files.
    hay = f"{link_text} {unquote(urlparse(url).path.rsplit('/', 1)[-1])}"
    if re.search(r"transcript", hay, re.I):
        return TRANSCRIPTS
    debt = cls.classify(link_text) or cls.classify(unquote(urlparse(url).path).replace("-", " "))
    if debt:
        return debt
    if re.search(r"presentation|investor day|appendix", hay, re.I):
        return PRESENTATIONS
    return None


def _year(link_text: str, url: str) -> int | None:
    years = [int(y) for y in _YEAR.findall(f"{link_text} {unquote(url)}")]
    return max(years) if years else None


def crawl(client: HttpClient, start_url: str, max_pages: int = 80, log=print) -> list[SiteDocument]:
    """Breadth-first crawl of the IR section of `start_url`'s domain."""
    domain = urlparse(start_url).netloc.removeprefix("www.")
    robots = RobotFileParser()
    try:
        robots.parse(decode(client.get_bytes(urljoin(start_url, "/robots.txt"))).splitlines())
    except Exception:  # no robots.txt (404) means no restrictions
        robots.parse([])

    def same_site(u: str) -> bool:
        return urlparse(u).netloc.removeprefix("www.") == domain

    queue: list[tuple[str, int]] = [(start_url, 0)]
    seen: set[str] = set()
    docs: dict[str, SiteDocument] = {}
    while queue and len(seen) < max_pages:
        url, depth = queue.pop(0)
        if url in seen or not robots.can_fetch(client.user_agent, url):
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
                cat = categorize(text, href)
                if cat and href not in docs:
                    docs[href] = SiteDocument(href, text, cat, _year(text, href))
                elif cat and _year(text, href) and not docs[href].year:
                    docs[href] = SiteDocument(href, text, cat, _year(text, href))
            elif same_site(href) and href not in seen and depth < 3:
                # From the home page follow only IR-looking links; inside the IR section follow more.
                if _IR_PATH.search(urlparse(href).path) or _IR_PATH.search(text):
                    queue.append((href, depth + 1))
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
        if not re.match(r"\s*\d{1,3}\b", nxt):
            body = aif_text[m.end():]
            break
    if body is None:
        return []
    end = _MC_END.search(body[50:])
    section = body[: (end.start() + 50) if end else 8000]
    section = _PAGE_FOOTER.sub(" ", section)
    items = []
    for part in _BULLET.split(section):
        part = re.split(r"\(\s*See\b", part, maxsplit=1, flags=re.I)[0]
        part = re.sub(r"\s+", " ", part).strip(" ;.,")
        if len(part) > 15 and re.search(r"agreement|indenture|contract|arrangement|facility|note", part, re.I):
            items.append(part)
    # The intro sentence ("The following are the only contracts ...") is not an item.
    return [i for i in items if not re.match(r"(the following|other than|except)", i, re.I)]
