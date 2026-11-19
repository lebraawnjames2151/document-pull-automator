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
