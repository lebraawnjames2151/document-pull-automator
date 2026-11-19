# docpull

Pulls a company's public SEC documents into one organized folder:

| Folder | What | Source |
|---|---|---|
| `10-K/`, `10-Q/` | Annual and quarterly reports | EDGAR, last N years |
| `20-F/`, `40-F/` | Foreign issuer annual reports. For 40-F, the AIF, financial statements and MD&A exhibits are pulled and named | EDGAR, last N years |
| `6-K/` | Foreign issuer results releases, MD&A and interim financials. Other 6-Ks (notices, proxy material) are skipped | EDGAR, last N years |
| `Earnings Releases/` | EX-99 exhibits of 8-Ks filed under Item 2.02 | EDGAR, last N years |
| `Indentures/` | Base and supplemental indentures, officer's certificates setting note terms | See below |
| `Credit Agreements/` | Credit and loan agreements, amendments, intercreditor and collateral agreements | See below |
| `index.csv` | Every document with form, dates, description, source URL and download status | |

Files are saved as PDF (default) or as the original HTML.

## How debt documents are found

1. **Latest 10-K / 20-F / 40-F exhibit index.** The SEC has required hyperlinked exhibit indexes since 2017, including for exhibits incorporated by reference. So this index links to every material indenture and credit agreement still in force, even ones filed 10+ years ago. This is the main source.
2. **10-Qs filed after that annual report.** Same parsing, which picks up new debt docs filed with a 10-Q.
3. **8-Ks filed after the latest periodic report** under Item 1.01 or 2.03. EX-4 and EX-10 exhibits are classified from their opening text when EDGAR's description is generic.
4. **6-Ks** (foreign issuers). Exhibits whose title is an indenture or credit agreement.

Foreign issuers that file 40-F or 20-F (e.g. Canadian MJDS filers) often leave indentures out of the annual report. For them, two more EDGAR sources are searched across the company's full filing history:

5. **Registration statements (F-10, F-9, F-3, F-4, F-1).** Their hyperlinked exhibit lists, plus the indenture attached as Exhibit 7.1 (F-9/F-10) or 4.x (F-1/F-3/F-4).
6. **6-Ks filed within 30 days after a prospectus supplement (SUPPL, 424B2, 424B5).** Executed base and supplemental indentures are usually filed then, at closing.

Names come from the document's cover page where possible, e.g. `2012-08-08 Fifth Supplemental Indenture - 2.500% Notes due 2018, 3.750% Notes due 2023, 5.400% Notes due 2043.pdf`. Unexecuted templates from shelf registrations are prefixed `Form of`.

## Setup

```bash
pip install -e .
```

```bash
playwright install chromium
```

SEC requires every automated request to identify you with a name and email. Set it once:

```bash
export SEC_USER_AGENT="Your Name your.email@example.com"
```

## Usage

```bash
docpull CVNA
```

```bash
docpull AAPL MSFT TECK --years 5 --out ~/Documents/filings
```

Other inputs and options:

- Private SEC filers without a ticker: pass the company name (`docpull "Univision Holdings"`) or the CIK (`docpull 1640579`). If a name matches several entities, the candidates and their CIKs are printed.
- `--dry-run` lists what would be downloaded.
- `--format html` keeps the original files (faster, no browser needed).
- `--forms 10-K,10-Q` limits periodic forms.
- `--no-earnings` and `--no-debt` skip those categories.

Re-running is safe. Files that already exist are skipped, so a re-run only fetches new filings.

## Known limitations

- **Research reports and earnings call transcripts are not included.** They are licensed content. Adding them requires API access from a vendor (e.g. Bloomberg, S&P Capital IQ) and is not built yet.
- **144A-for-life and private-company debt docs** are generally not on EDGAR and cannot be pulled.
- **Annual reports filed before 2017** have no hyperlinked exhibit index, so older debt docs need the 8-K scan or a manual check. A warning is printed when an annual report has no hyperlinks.
- **Debt docs for foreign issuers come from full history, not just what is in force.** Indentures for notes that have since been repaid are included. Check maturities before relying on a document.
- **Foreign issuers' credit agreements** are often not filed on EDGAR at all. Canadian issuers usually file them on SEDAR+ only, when they file them at all.
- **Canadian SEDAR+ filings** are not covered, only what is also filed on EDGAR.
- Classification uses keyword rules. Unusual titles can be missed or misfiled. `index.csv` lists the source URL for every file so you can check.

## Tests

```bash
pip install -e ".[dev]"
```

```bash
pytest
```
