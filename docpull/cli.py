"""Command line entry point: `docpull AAPL MSFT --years 3`."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

from .edgar import AmbiguousCompany, EdgarClient, EdgarError, resolve_company
from .pipeline import DEFAULT_FORMS, execute, plan_documents
from .render import Renderer


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="docpull",
        description="Pull SEC filings, indentures and credit agreements for companies into folders.",
    )
    p.add_argument("companies", nargs="+", help="Ticker, CIK, or company name (quote names with spaces)")
    p.add_argument("--years", type=float, default=3, help="Lookback for periodic filings (default 3)")
    p.add_argument("--out", type=Path, default=Path("output"), help="Output folder (default ./output)")
    p.add_argument("--format", choices=["pdf", "html"], default="pdf", help="Save as PDF (default) or original HTML")
    p.add_argument("--forms", default=",".join(DEFAULT_FORMS),
                   help=f"Periodic forms to pull (default {','.join(DEFAULT_FORMS)})")
    p.add_argument("--no-earnings", action="store_true", help="Skip 8-K earnings releases")
    p.add_argument("--no-debt", action="store_true", help="Skip indentures and credit agreements")
    p.add_argument("--dry-run", action="store_true", help="List what would be downloaded without downloading")
    p.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT", ""),
                   help="SEC User-Agent: 'Your Name your@email.com' (or set SEC_USER_AGENT)")
    args = p.parse_args(argv)

    try:
        client = EdgarClient(args.user_agent)
    except EdgarError as e:
        print(e, file=sys.stderr)
        return 2

    forms = tuple(f.strip().upper() for f in args.forms.split(",") if f.strip())
    renderer = Renderer(client, args.format)
    status = 0
    try:
        for query in args.companies:
            try:
                company = resolve_company(client, query)
            except AmbiguousCompany as e:
                print(f"'{e.query}' is ambiguous. Re-run with the CIK of the one you want:", file=sys.stderr)
                for cik, name in e.candidates:
                    print(f"  {cik:>10}  {name}", file=sys.stderr)
                status = 1
                continue
            except EdgarError as e:
                print(e, file=sys.stderr)
                status = 1
                continue

            plan = plan_documents(client, company, args.years, forms,
                                  earnings=not args.no_earnings, debt=not args.no_debt)
            for w in plan.warnings:
                print(f"  warning: {w}")
            counts = Counter(t.category for t in plan.tasks)
            print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
            if args.dry_run:
                for t in plan.tasks:
                    print(f"    {t.category}/{t.stem}  <- {t.url}")
                continue
            root = execute(plan, renderer, args.out)
            failed = sum(t.status.startswith("failed") for t in plan.tasks)
            print(f"  saved to {root}" + (f" ({failed} failed, see index.csv)" if failed else ""))
            if failed:
                status = 1
    finally:
        renderer.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
