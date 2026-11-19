"""Decide whether an exhibit is an indenture, a credit/loan document, or neither."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

INDENTURE = "Indentures"
CREDIT = "Credit Agreements"

_EXCLUDE = re.compile(
    r"description of (the )?(registrant|securities|capital stock|common stock)"
    r"|trust indenture act|statement of eligibility|form t-1"
    r"|equity incentive|stock (option|incentive|purchase) plan|employment agreement|severance"
    r"|indemnification agreement|underwriting agreement|registration rights",
    re.I,
)

_INDENTURE = re.compile(r"\bindenture\b", re.I)
# Officer's certificates and global notes are how many IG issuers (e.g. Apple) set terms of
# a new series under a base indenture. Only trusted on EX-4 exhibits.
_INDENTURE_EX4 = re.compile(r"officer[’']?s[’']? certificate|global note|form of .{0,60}\bnotes?\b", re.I)

_CREDIT = re.compile(
    r"credit agreement|loan agreement|credit facilit|term loan|revolving (credit|loan)"
    r"|loan and security agreement|guarantee and collateral agreement|pledge and security agreement"
    r"|collateral agreement|intercreditor agreement|note purchase agreement"
    r"|receivables (purchase|financing|securitization) agreement"
    r"|incremental (assumption|facility|term loan)|refinancing amendment",
    re.I,
)

_GENERIC_DESC = re.compile(r"^\s*(ex(hibit)?[\s\-.]*)?[\d.]+[a-z]?\s*$", re.I)


def classify(text: str, exhibit_type: str = "") -> str | None:
    """Classify by the earliest positive match in `text` (a description or a document's opening)."""
    found = classify_at(text, exhibit_type)
    return found[0] if found else None


def classify_at(text: str, exhibit_type: str = "") -> tuple[str, int] | None:
    """Like classify(), but also returns where in `text` the match starts."""
    if not text or _EXCLUDE.search(text[:600]):
        return None
    hits = []
    for pattern, label in ((_INDENTURE, INDENTURE), (_CREDIT, CREDIT)):
        m = pattern.search(text)
        if m:
            hits.append((m.start(), label))
    if exhibit_type.upper().startswith("EX-4"):
        m = _INDENTURE_EX4.search(text)
        if m:
            hits.append((m.start(), INDENTURE))
    if not hits:
        return None
    pos, label = min(hits)
    return label, pos


def is_generic_description(desc: str) -> bool:
    """True when EDGAR's index description says nothing useful (e.g. 'EXHIBIT 10.1')."""
    return not desc or bool(_GENERIC_DESC.match(desc))


def document_opening(html_or_text: str, chars: int = 2500) -> str:
    """Visible text at the start of a document, i.e. its title/cover area."""
    if "<" in html_or_text[:2000]:
        soup = BeautifulSoup(html_or_text, "html.parser")
        for hidden in soup.select('[style*="display:none"], [style*="display: none"], ix\\:header'):
            hidden.decompose()
        text = soup.get_text(" ")
    else:
        text = html_or_text
    return re.sub(r"\s+", " ", text).strip()[:chars]


_REPORT_TITLES = [
    (re.compile(r"annual information form", re.I), "Annual Information Form"),
    (re.compile(r"management[’']?s discussion (and|&) analysis", re.I), "MD&A"),
    (re.compile(r"(interim|condensed|consolidated|unaudited)[\w ,]{0,40}financial statements", re.I),
     "Financial Statements"),
    (re.compile(r"\bannual report\b", re.I), "Annual Report"),
    (re.compile(r"\b(first|second|third|fourth|q[1-4]|full[- ]year|year[- ]end|quarterly|annual)\b[\w ,]{0,40}"
                r"\b(results|earnings)\b|\bfinancial results\b", re.I), "Results Release"),
]


_RESULTS_NOTICE = re.compile(
    r"\bto (release|report|announce|host)\b[\w ,]{0,50}\b(results|earnings|conference call)\b"
    r"|\b(results|earnings)\b[\w ,]{0,30}\b(conference call|webcast) (on|date)\b",
    re.I,
)


def report_title(opening: str, chars: int = 700) -> str | None:
    """For foreign-filer 6-K/40-F exhibits: name the report if it is one, else None."""
    head = opening[:chars]
    if _RESULTS_NOTICE.search(head[:400]):
        return None
    hits = [(m.start(), title) for rx, title in _REPORT_TITLES if (m := rx.search(head))]
    return min(hits)[1] if hits else None


# --------------------------------------------------------------------------- exhibit index parsing

_CIK_IN_PATH = re.compile(r"(/Archives/edgar/data/)0+(\d)")


def normalize_url(url: str) -> str:
    """Same document can be linked with or without a zero-padded CIK, or over http."""
    url = url.replace("http://", "https://", 1)
    return _CIK_IN_PATH.sub(r"\1\2", url)



@dataclass
class ExhibitLink:
    exhibit_no: str
    description: str
    url: str


_ARCHIVE_DOC = re.compile(r"/Archives/edgar/data/.+\.(htm|html|txt|pdf)$", re.I)
_EXHIBIT_NO = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){0,3}[a-z]?)\*?\s+(.*)$", re.I | re.S)


def exhibit_links(html: str, base_url: str) -> list[ExhibitLink]:
    """Hyperlinked exhibits listed in a 10-K/10-Q/20-F exhibit index.

    Since 2017 the SEC requires each exhibit in the index to be hyperlinked, including ones
    incorporated by reference from earlier filings. This is how we find debt documents that
    are still in force but were filed years ago.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    out = []
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"].strip())
        if url.startswith("https://www.sec.gov/ix?doc="):
            url = "https://www.sec.gov" + url[len("https://www.sec.gov/ix?doc=") :]
        if not url.startswith(("http://www.sec.gov", "https://www.sec.gov")) or not _ARCHIVE_DOC.search(url):
            continue
        url = normalize_url(url)
        if url in seen:
            continue
        container = a.find_parent(["tr", "p", "li"]) or a.find_parent("div") or a
        text = re.sub(r"\s+", " ", container.get_text(" ")).strip()
        m = _EXHIBIT_NO.match(text)
        exhibit_no, desc = (m.group(1), m.group(2)) if m else ("", text)
        seen.add(url)
        out.append(ExhibitLink(exhibit_no, desc, url))
    return out


# --------------------------------------------------------------------------- naming helpers

_DATED = re.compile(
    r"dated\s+(?:as\s+of\s+)?([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})",
)


def agreement_date(desc: str) -> date | None:
    m = _DATED.search(desc)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", "").replace("Sept ", "Sep ")
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


_PARTIES = re.compile(r",?\s+(?:dated\b|by and (?:among|between)\b|among\b|between\b|entered into\b)", re.I)
_SUBJECT = re.compile(
    r"\b(?:related to|relating to|with respect to|in respect of|governing|providing for)\s+(?:the\s+)?"
    r"(.+?)(?=\s*\(|;|,\s+(?:among|between|dated)\b|$)",
    re.I,
)
_AGENT = re.compile(r"([A-Z][\w.&'’\- ]{2,60}?),?\s+as\s+(?:the\s+)?(administrative|collateral)\s+agent", re.I)


def short_description(desc: str, limit: int = 110) -> str:
    """'Indenture, dated ..., among X, Y and Z, as trustee, related to the 5.5% Notes due 2027 (incorporated...)'
    becomes 'Indenture - 5.5% Notes due 2027'."""
    desc = re.split(r"\((?:incorporated|filed|previously|included)", desc, maxsplit=1, flags=re.I)[0]
    desc = re.sub(r"\s+", " ", desc).strip(" ,.;:-")
    title = _PARTIES.split(desc, maxsplit=1)[0].strip(" ,.;:-")
    extra = ""
    subj = _SUBJECT.search(desc)
    if subj:
        extra = subj.group(1).strip(" ,.;:-")
    else:
        agent = _AGENT.search(desc)
        if agent:
            name = re.split(r"\band\b", agent.group(1))[-1].strip(" ,")
            name = re.sub(r"^the\s+", "", name, flags=re.I)
            extra = f"{name}, {agent.group(2).lower()} agent"
    if len(title) > limit * 0.75:  # no clean title boundary found
        title = title[: int(limit * 0.75)]
    out = f"{title} - {extra}" if extra and extra.lower() not in title.lower() else title
    return out[:limit].rstrip(" ,.;:-")


_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name: str, limit: int = 150) -> str:
    name = _BAD_CHARS.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].rstrip(" .")
