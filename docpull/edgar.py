"""Thin client for SEC EDGAR: company lookup, filing lists, filing indexes."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"


class EdgarError(Exception):
    pass


class AmbiguousCompany(EdgarError):
    def __init__(self, query: str, candidates: list[tuple[int, str]]):
        self.query = query
        self.candidates = candidates
        super().__init__(f"'{query}' matched {len(candidates)} companies")


class EdgarClient:
    """HTTP client that respects SEC fair-access rules (User-Agent, <10 req/s)."""

    def __init__(self, user_agent: str, min_interval: float = 0.12, max_retries: int = 5):
        if not user_agent or "@" not in user_agent:
            raise EdgarError(
                "SEC requires a User-Agent with a name and contact email, "
                "e.g. 'Acme Research jane@acme.com'. Set SEC_USER_AGENT or pass --user-agent."
            )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last = 0.0
        self._heads: dict[str, str] = {}

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str, stream: bool = False) -> requests.Response:
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = self.session.get(url, stream=stream, timeout=60)
            except requests.RequestException:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(2**attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code == 404:
                raise EdgarError(f"Not found: {url}")
            r.raise_for_status()
            return r
        raise EdgarError(f"Gave up after {self.max_retries} attempts: {url}")

    def get_json(self, url: str):
        return self.get(url).json()

    def get_bytes(self, url: str) -> bytes:
        return self.get(url).content

    def get_text(self, url: str) -> str:
        return decode(self.get(url).content)

    def get_head_text(self, url: str, nbytes: int = 96_000) -> str:
        """First `nbytes` of a document, without downloading the whole thing."""
        if url in self._heads:
            return self._heads[url]
        r = self.get(url, stream=True)
        buf = b""
        for chunk in r.iter_content(16_384):
            buf += chunk
            if len(buf) >= nbytes:
                break
        r.close()
        self._heads[url] = decode(buf)
        return self._heads[url]


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# --------------------------------------------------------------------------- companies


@dataclass
class Company:
    cik: int
    name: str
    ticker: str | None = None

    @property
    def folder_name(self) -> str:
        return f"{self.ticker} - {self.name}" if self.ticker else self.name


def resolve_company(client: EdgarClient, query: str) -> Company:
    """Accepts a ticker, a CIK, or a company name."""
    q = query.strip()
    if q.isdigit():
        sub = client.get_json(f"{SEC_DATA}/submissions/CIK{int(q):010d}.json")
        tickers = sub.get("tickers") or []
        return Company(int(q), sub["name"], tickers[0] if tickers else None)

    tickers = client.get_json(f"{SEC_WWW}/files/company_tickers.json")
    norm = q.upper().replace(".", "-")
    for row in tickers.values():
        if row["ticker"].upper() == norm:
            return Company(int(row["cik_str"]), row["title"], row["ticker"])

    # Not a ticker: fall back to EDGAR's entity name search (covers private SEC filers).
    res = client.get_json(f"https://efts.sec.gov/LATEST/search-index?keysTyped={requests.utils.quote(q)}")
    hits = [(int(h["_id"]), h["_source"]["entity"]) for h in res.get("hits", {}).get("hits", [])]
    exact = [h for h in hits if h[1].strip().upper() == q.upper()]
    if len(exact) == 1:
        return resolve_company(client, str(exact[0][0]))
    if len(hits) == 1:
        return resolve_company(client, str(hits[0][0]))
    if not hits:
        raise EdgarError(f"No EDGAR company found for '{q}'")
    raise AmbiguousCompany(q, hits[:15])


# --------------------------------------------------------------------------- filings


@dataclass
class Filing:
    cik: int
    accession: str
    form: str
    filing_date: date
    report_date: date | None
    primary_doc: str
    items: str

    @property
    def folder_url(self) -> str:
        return f"{SEC_WWW}/Archives/edgar/data/{self.cik}/{self.accession.replace('-', '')}/"

    @property
    def primary_url(self) -> str:
        return urljoin(self.folder_url, self.primary_doc)

    @property
    def index_url(self) -> str:
        return urljoin(self.folder_url, f"{self.accession}-index.htm")

    def has_item(self, *items: str) -> bool:
        have = {i.strip() for i in self.items.split(",")}
        return any(i in have for i in items)


def _parse_date(s: str) -> date | None:
    return date.fromisoformat(s) if s else None


def _rows(block: dict, cik: int) -> list[Filing]:
    out = []
    for i, acc in enumerate(block["accessionNumber"]):
        out.append(
            Filing(
                cik=cik,
                accession=acc,
                form=block["form"][i],
                filing_date=date.fromisoformat(block["filingDate"][i]),
                report_date=_parse_date(block["reportDate"][i]),
                primary_doc=block["primaryDocument"][i],
                items=block.get("items", [""] * len(block["form"]))[i] or "",
            )
        )
    return out


def list_filings(client: EdgarClient, cik: int, since: date | None = None) -> list[Filing]:
    """All filings for a CIK, newest first. Older pages are fetched only if needed to reach `since`."""
    sub = client.get_json(f"{SEC_DATA}/submissions/CIK{cik:010d}.json")
    filings = _rows(sub["filings"]["recent"], cik)
    for page in sub["filings"].get("files", []):
        if since is not None and date.fromisoformat(page["filingTo"]) < since:
            continue
        filings += _rows(client.get_json(f"{SEC_DATA}/submissions/{page['name']}"), cik)
    filings.sort(key=lambda f: f.filing_date, reverse=True)
    return filings


@dataclass
class FilingDocument:
    seq: str
    description: str
    filename: str
    doc_type: str
    url: str


def filing_documents(client: EdgarClient, filing: Filing) -> list[FilingDocument]:
    """Documents listed in a filing's index page (primary doc + exhibits)."""
    soup = BeautifulSoup(client.get_text(filing.index_url), "html.parser")
    docs = []
    for table in soup.find_all("table", class_="tableFile"):
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            a = tds[2].find("a")
            if not a or not a.get("href"):
                continue
            href = a["href"]
            if href.startswith("/ix?doc="):
                href = href[len("/ix?doc=") :]
            docs.append(
                FilingDocument(
                    seq=tds[0].get_text(strip=True),
                    description=tds[1].get_text(" ", strip=True),
                    filename=a.get_text(strip=True),
                    doc_type=tds[3].get_text(strip=True),
                    url=urljoin(SEC_WWW, href),
                )
            )
    return docs
