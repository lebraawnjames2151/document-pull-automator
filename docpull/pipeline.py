"""Work out which documents to pull for a company, then download them."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from . import classify as cls
from .edgar import Company, EdgarClient, EdgarError, Filing, filing_documents, list_filings
from .render import Renderer

ANNUAL = {"10-K", "20-F", "40-F"}
FOREIGN_ANNUAL = {"20-F", "40-F"}
QUARTERLY = {"10-Q"}
# Shelf/registration statements. Under Forms F-9/F-10 the indenture is Exhibit 7.1;
# under F-1/F-3/F-4 it is Exhibit 4.x.
REGISTRATION = {"F-1", "F-3", "F-4", "F-9", "F-10"}
# Prospectus supplements mark a debt offering; executed indentures follow on a 6-K at closing.
OFFERING = {"SUPPL", "424B2", "424B5"}
OFFERING_CLOSE_DAYS = 30
DEFAULT_FORMS = ("10-K", "10-Q", "20-F", "40-F", "6-K")
EARNINGS = "Earnings Releases"


@dataclass
class Task:
    category: str
    url: str
    stem: str
    form: str = ""
    filing_date: date | None = None
    period: date | None = None
    description: str = ""
    status: str = ""
    path: Path | None = None


@dataclass
class Plan:
    company: Company
    tasks: list[Task] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, task: Task) -> None:
        task.url = cls.normalize_url(task.url)
        if all(t.url != task.url for t in self.tasks):
            self.tasks.append(task)


def _base_form(form: str) -> str:
    return form.split("/")[0]


def _periodic_stem(f: Filing) -> str:
    when = f.report_date or f.filing_date
    return cls.safe_filename(f"{when.isoformat()} {f.form.replace('/', '-')}")


def plan_documents(
    client: EdgarClient,
    company: Company,
    years: float = 3,
    forms: tuple[str, ...] = DEFAULT_FORMS,
    earnings: bool = True,
    debt: bool = True,
    log: Callable[[str], None] = print,
) -> Plan:
    since = date.today() - timedelta(days=round(365.25 * years))
    plan = Plan(company)
    filings = list_filings(client, company.cik, since)
    window = [f for f in filings if f.filing_date >= since]
    log(f"{company.folder_name}: {len(window)} filings since {since}")

    # --- Periodic reports ------------------------------------------------------------
    for f in window:
        base = _base_form(f.form)
        if base not in forms:
            continue
        if base == "6-K":
            _plan_6k(client, f, plan)
            continue
        plan.add(Task(base, f.primary_url, _periodic_stem(f), f.form, f.filing_date, f.report_date))
        if base == "40-F":  # the 40-F itself is a wrapper; AIF, MD&A and financials are EX-99s
            for d in filing_documents(client, f):
                if d.doc_type.upper().startswith("EX-99"):
                    title = cls.report_title(cls.document_opening(client.get_head_text(d.url))) or ""
                    stem = f"{_periodic_stem(f)} {d.doc_type} {title}"
                    plan.add(Task(base, d.url, cls.safe_filename(stem), f.form, f.filing_date,
                                  f.report_date, title))

    # --- Earnings releases (8-K Item 2.02) --------------------------------------------
    if earnings:
        for f in window:
            if _base_form(f.form) == "8-K" and f.has_item("2.02"):
                for d in filing_documents(client, f):
                    if d.doc_type.upper().startswith("EX-99"):
                        stem = f"{f.filing_date.isoformat()} Earnings Release {d.doc_type}"
                        plan.add(Task(EARNINGS, d.url, cls.safe_filename(stem), f.form, f.filing_date,
                                      f.report_date, d.description))

    if debt:
        _plan_debt_documents(client, filings, plan, log)
    return plan


def _plan_6k(client: EdgarClient, f: Filing, plan: Plan, debt_only: bool = False) -> None:
    """6-Ks carry everything a foreign issuer publishes. Keep results/MD&A/financials and debt
    documents; skip the rest (press releases, notices, voting results)."""
    docs = filing_documents(client, f)
    candidates = [d for d in docs if d.doc_type.upper().startswith(("EX-99", "EX-4", "EX-10"))]
    if not candidates:
        candidates = [d for d in docs if d.doc_type.upper().startswith("6-K")]
    for d in candidates:
        opening = cls.document_opening(client.get_head_text(d.url))
        debt = cls.classify_at(opening[:400])
        if debt:
            label, pos = debt
            plan.add(_debt_task(label, d.url, d.doc_type.removeprefix("EX-"), opening, f, pos))
            continue
        if debt_only:
            continue
        title = cls.report_title(opening)
        if title:
            stem = f"{f.filing_date.isoformat()} {title} {d.doc_type}"
            plan.add(Task("6-K", d.url, cls.safe_filename(stem), f.form, f.filing_date, f.report_date, title))


def _plan_debt_documents(client: EdgarClient, filings: list[Filing], plan: Plan, log) -> None:
    """Indentures and credit agreements currently in force.

    1. Exhibit index of the latest annual report, which hyperlinks every material debt
       document still outstanding, however old.
    2. Exhibit indexes of 10-Qs filed since then.
    3. 8-Ks filed since the latest periodic report with Item 1.01 (material agreement) or
       2.03 (new debt). Exhibits are classified from their opening text when EDGAR's
       description is generic.
    """
    annual = next((f for f in filings if f.form in ANNUAL), None)
    if annual is None:
        plan.warnings.append("No annual report found, so no exhibit index to read debt documents from.")
        sources = []
    else:
        sources = [annual] + [f for f in filings if f.form in QUARTERLY and f.filing_date > annual.filing_date]

    for f in sources:
        try:
            html = client.get_text(f.primary_url)
        except EdgarError as e:
            plan.warnings.append(f"Could not read {f.form} {f.filing_date}: {e}")
            continue
        links = cls.exhibit_links(html, f.folder_url)
        found = 0
        for link in links:
            label = cls.classify(link.description, "EX-" + link.exhibit_no)
            if label:
                plan.add(_debt_task(label, link.url, link.exhibit_no, link.description, f))
                found += 1
        log(f"  {f.form} {f.filing_date}: {found} debt documents in exhibit index")
        if f.form in ANNUAL and not links:
            plan.warnings.append(
                f"{f.form} {f.filing_date} has no hyperlinked exhibits. Debt docs may be missing; "
                "check its exhibit index manually."
            )

    if annual is not None and annual.form in FOREIGN_ANNUAL:
        _plan_foreign_debt(client, filings, plan, log)

    latest_periodic = max((f.filing_date for f in sources), default=date.min)
    for f in filings:
        if f.filing_date <= latest_periodic:
            break
        if _base_form(f.form) != "8-K" or not f.has_item("1.01", "2.03"):
            continue
        for d in filing_documents(client, f):
            t = d.doc_type.upper()
            if not (t.startswith("EX-4") or t.startswith("EX-10")):
                continue
            if not cls.is_generic_description(d.description):
                label = cls.classify(d.description, t)
                if label:
                    plan.add(_debt_task(label, d.url, t.removeprefix("EX-"), d.description, f))
                continue
            opening = cls.document_opening(client.get_head_text(d.url))
            found = cls.classify_at(opening, t)
            if found:
                label, pos = found
                plan.add(_debt_task(label, d.url, t.removeprefix("EX-"), opening, f, pos))


def _plan_foreign_debt(client: EdgarClient, filings: list[Filing], plan: Plan, log) -> None:
    """Foreign private issuers (20-F/40-F) often leave indentures out of the annual report's
    exhibit index. They do appear in:

    1. Registration statements (F-10, F-9, F-3, ...): hyperlinked exhibit lists, plus the
       indenture attached as EX-7.x / EX-4.x. Every registration in the company's history is read,
       because base indentures from old shelves often still govern outstanding notes.
    2. 6-Ks filed within OFFERING_CLOSE_DAYS after a prospectus supplement, which is when
       executed indentures and supplemental indentures are usually filed.
    """
    for f in filings:
        if _base_form(f.form) not in REGISTRATION or f.primary_doc.endswith(".paper"):
            continue
        before = len(plan.tasks)
        try:
            html = client.get_text(f.primary_url)
            for link in cls.exhibit_links(html, f.folder_url):
                label = cls.classify(link.description, "EX-" + link.exhibit_no)
                if label:
                    plan.add(_debt_task(label, link.url, link.exhibit_no, link.description, f))
            for d in filing_documents(client, f):
                t = d.doc_type.upper()
                if not t.startswith(("EX-7", "EX-4")):
                    continue
                opening = cls.document_opening(client.get_head_text(d.url))
                found = cls.classify_at(opening[:600], t)
                if found:
                    label, pos = found
                    plan.add(_debt_task(label, d.url, t.removeprefix("EX-"), opening, f, pos))
        except EdgarError as e:
            plan.warnings.append(f"Could not read {f.form} {f.filing_date}: {e}")
            continue
        log(f"  {f.form} {f.filing_date}: {len(plan.tasks) - before} debt documents")

    offerings = [f.filing_date for f in filings if _base_form(f.form) in OFFERING]
    closings = [
        f for f in filings
        if _base_form(f.form) == "6-K"
        and any(0 <= (f.filing_date - d).days <= OFFERING_CLOSE_DAYS for d in offerings)
    ]
    before = len(plan.tasks)
    for f in closings:
        try:
            _plan_6k(client, f, plan, debt_only=True)
        except EdgarError as e:
            plan.warnings.append(f"Could not read 6-K {f.filing_date}: {e}")
    if offerings:
        log(f"  6-Ks after {len(offerings)} prospectus supplements: {len(plan.tasks) - before} debt documents")


def _debt_task(label: str, url: str, exhibit_no: str, desc: str, source: Filing,
               cover_pos: int | None = None) -> Task:
    """`desc` is an exhibit-index description, or a document's opening text when `cover_pos`
    (where the title was matched) is given."""
    if cover_pos is None:
        short = cls.short_description(desc)
    else:
        short = cls.cover_description(desc, cover_pos)
        desc = desc[cover_pos:]
    if cls.is_form_of(desc) and not short.lower().startswith("form of"):
        short = "Form of " + short
    when = cls.agreement_date(desc[:300])
    stem = f"{when.isoformat()} {short}" if when else short
    task = Task(label, url, cls.safe_filename(stem), source.form, source.filing_date, None, desc)
    task.description = f"Ex. {exhibit_no}: {short}" if exhibit_no else short
    return task


def execute(plan: Plan, renderer: Renderer, out_dir: Path, log: Callable[[str], None] = print) -> Path:
    root = out_dir / cls.safe_filename(plan.company.folder_name)
    used: set[Path] = set()
    for i, t in enumerate(plan.tasks, 1):
        stem = root / t.category / t.stem
        path = renderer.target_path(t.url, stem)
        n = 2
        while path in used:  # two docs with the same name, e.g. identical supplemental indentures
            stem = root / t.category / f"{t.stem} ({n})"
            path = renderer.target_path(t.url, stem)
            n += 1
        used.add(path)
        if path.exists() and path.stat().st_size > 0:
            t.status, t.path = "already downloaded", path
            continue
        log(f"  [{i}/{len(plan.tasks)}] {t.category}/{path.name}")
        try:
            t.path = renderer.save(t.url, stem)
            t.status = "ok"
        except Exception as e:  # keep going; record the failure in the index
            t.status = f"failed: {e}"
            log(f"    failed: {e}")
    write_index(plan, root)
    return root


def write_index(plan: Plan, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "index.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["category", "file", "form", "filing_date", "period", "description", "source_url", "status"])
        for t in plan.tasks:
            w.writerow([
                t.category,
                str(t.path.relative_to(root)) if t.path else "",
                t.form,
                t.filing_date.isoformat() if t.filing_date else "",
                t.period.isoformat() if t.period else "",
                t.description,
                t.url,
                t.status,
            ])
