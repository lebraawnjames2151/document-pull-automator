from docpull import site


def test_website_from_text_ignores_regulators():
    text = "available on www.sedarplus.ca and www.sec.gov and at www.teck.com. See www.teck.com/investors"
    assert site.website_from_text(text) == "https://www.teck.com/"


def test_website_from_text_none():
    assert site.website_from_text("Filed on www.sedarplus.ca") is None


def test_categorize():
    media = "https://www.teck.com/media/"
    assert site.categorize("Q3 2026 Call Transcript", media + "Q3-2026-Conference-Transcript.pdf") == site.TRANSCRIPTS
    assert site.categorize("See presentation", media + "Teck-Q3-2026-Presentation.pdf") == site.PRESENTATIONS
    assert site.categorize("Information Circular 2023", "https://x.com/presentations/Information-Circular-2023.pdf") is None
    assert site.categorize("Credit Agreement", media + "ca.pdf") == "Credit Agreements"
    assert site.categorize("2025 Annual Report", media + "2025-Annual-Report.pdf") is None


def test_document_name():
    d = site.SiteDocument("https://x.com/media/Teck-Q3-2026-Presentation.pdf", "See presentation", site.PRESENTATIONS, 2026)
    assert d.name == "Teck Q3 2026 Presentation"
    d = site.SiteDocument("https://x.com/m/Q2.pdf", "Download Q2 2025 Conference Call Transcript", site.TRANSCRIPTS, 2025)
    assert d.name == "Q2 2025 Conference Call Transcript"


AIF = """CORPORATE STRUCTURE 3 MATERIAL CONTRACTS 103 INTERESTS OF EXPERTS 104 ... body text ...
MATERIAL CONTRACTS The following are the only contracts entered into by Teck that are material, still in effect
and not entered into in the ordinary course of business: ■ Arrangement Agreement, dated as of September 9, 2025,
between Teck and Anglo American (See " Development of Business " for more details) Teck Resources Limited 103 2025
Annual Information Form ■ Indenture, dated as of June 30, 2020, between Teck Resources Limited and The Bank of New
York Mellon (See " Investor Information " for more details) ■ Indenture, dated as of September 12, 2002, between
Teck Resources Limited and The Bank of New York Mellon, as trustee INTERESTS OF EXPERTS The following persons"""


def test_material_contracts():
    items = site.material_contracts(AIF)
    assert items == [
        "Arrangement Agreement, dated as of September 9, 2025, between Teck and Anglo American",
        "Indenture, dated as of June 30, 2020, between Teck Resources Limited and The Bank of New York Mellon",
        "Indenture, dated as of September 12, 2002, between Teck Resources Limited and The Bank of New York Mellon, "
        "as trustee",
    ]


def test_material_contracts_absent():
    assert site.material_contracts("No such section here") == []


def test_year_variants():
    url = "https://x.com/investors/financial-reports/?_sft_financial_year=2026"
    assert site._year_variants(url, range(2024, 2026)) == [
        "https://x.com/investors/financial-reports/",
        "https://x.com/investors/financial-reports/?_sft_financial_year=2024",
        "https://x.com/investors/financial-reports/?_sft_financial_year=2025",
    ]
    assert site._year_variants("https://x.com/investors/", range(2024, 2026)) == []


def test_reports_categories():
    up = "https://dyedurham.com/wp-content/uploads/2026/"
    assert site.categorize("Q1", up + "FY26-Q1-MDA-FINAL.pdf", reports=True) == site.MDA
    assert site.categorize("Q3", up + "FY26-Q3-FS.pdf", reports=True) == site.FINANCIALS
    assert site.categorize("AR", up + "DD-FY2026-AIF-Filing-Version.pdf", reports=True) == site.AIF
    assert site.categorize("Q4", up + "DD-Draft-Press-release-re-Q4-2026-Financials.pdf", reports=True) == site.PRESS
    assert site.categorize("Q3", up + "FY26-Q3-MDA.pdf") is None  # reports only when not on EDGAR


DD_AIF = """MATERIAL CONTRACTS ........................ 70 EXPERTS ......... 70 ... MATERIAL CONTRACTS The only
material contracts ... still in effect, are as follows: • the IPO Underwriting Agreement, as described in the
Company's IPO prospectus dated July 13, 2020; • the Original Indenture, as described in the Company's prospectus
supplement dated February 18, 2021; • the Senior Credit Agreement, as amended by the first amendment to the Senior
Credit Agreement dated September 26, 2025; and • the SRP as described herein. Copies of these agreements are
available under the Company's profile on SEDAR+. EXPERTS The Company's auditor"""


def test_material_contracts_skips_dotted_toc():
    items = site.material_contracts(DD_AIF)
    assert items[0].startswith("the IPO Underwriting Agreement")
    assert any(i.startswith("the Original Indenture") for i in items)
    assert any(i.startswith("the Senior Credit Agreement, as amended") for i in items)


def test_year_prefers_file_name_over_upload_folder():
    up = "https://dyedurham.com/wp-content/uploads/2023/06/"
    assert site._year("Q1", up + "Dye-Durham-Financial-Statements-Q1-2021.pdf") == 2021
    assert site._year("Q1", up + "DD-FY24-Q1-MDA.pdf") == 2024
    assert site._year("Q1", up + "DD_FY24_Q1_MDA.pdf") == 2024
    assert site._year("AR", up + "DD-MIC.pdf") == 2023  # nothing in the name: folder year
