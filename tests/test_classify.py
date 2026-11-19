from datetime import date

import pytest

from docpull import classify as cls


@pytest.mark.parametrize(
    "text, ex_type, expected",
    [
        ("Indenture, dated March 29, 2021, among Carvana Co. and U.S. Bank, as trustee", "EX-4.1", cls.INDENTURE),
        ("Second Supplemental Indenture dated as of August 30, 2023", "", cls.INDENTURE),
        ("Credit Agreement, dated as of August 14, 2026, among Carvana Co. and Barclays", "EX-10.1", cls.CREDIT),
        ("Amendment No. 3 to the Credit Agreement", "EX-10.2", cls.CREDIT),
        ("Intercreditor Agreement among the Agents", "EX-10.3", cls.CREDIT),
        ("Officer's Certificate of the Registrant, dated May 3, 2013", "EX-4.2", cls.INDENTURE),
        ("Officer's Certificate re: annual bonus", "EX-10.9", None),  # only trusted on EX-4
        ("Description of the Registrant's Securities", "EX-4.5", None),
        ("2021 Equity Incentive Plan", "EX-10.4", None),
        ("Statement of Eligibility under the Trust Indenture Act of 1939 on Form T-1", "EX-25.1", None),
        ("Agreement and Plan of Merger", "EX-2.1", None),
        ("", "", None),
    ],
)
def test_classify(text, ex_type, expected):
    assert cls.classify(text, ex_type) == expected


def test_earliest_match_wins():
    # A credit agreement amendment that later mentions an indenture is still a credit document.
    text = "FIRST AMENDMENT TO CREDIT AGREEMENT ... permitted under the Indenture"
    assert cls.classify(text) == cls.CREDIT


@pytest.mark.parametrize("desc", ["EXHIBIT 10.1", "Ex-4.2", "10.1", "", "EX 99.1a"])
def test_generic_descriptions(desc):
    assert cls.is_generic_description(desc)


def test_specific_description_not_generic():
    assert not cls.is_generic_description("Credit Agreement")


def test_short_description_uses_note_series():
    desc = (
        "Indenture, dated March 29, 2021, among Carvana Co., each of the guarantors party thereto and "
        "U.S. Bank National Association, as trustee, related to the 5.500% Senior Notes due 2027 "
        "(incorporated by reference to Exhibit 4.1 to Form 8-K filed March 29, 2021)"
    )
    assert cls.short_description(desc) == "Indenture - 5.500% Senior Notes due 2027"


def test_short_description_uses_agent():
    desc = (
        "Credit Agreement, dated as of August 14, 2026, among Carvana Co., the lenders party thereto "
        "and Barclays Bank PLC, as administrative agent"
    )
    assert cls.short_description(desc) == "Credit Agreement - Barclays Bank PLC, administrative agent"


@pytest.mark.parametrize(
    "desc, expected",
    [
        ("Indenture, dated March 29, 2021, among", date(2021, 3, 29)),
        ("Supplemental Indenture dated as of Sept. 1, 2023", date(2023, 9, 1)),
        ("Credit Agreement dated as of Aug. 14, 2026", date(2026, 8, 14)),
        ("Credit Agreement", None),
    ],
)
def test_agreement_date(desc, expected):
    assert cls.agreement_date(desc) == expected


def test_safe_filename():
    assert cls.safe_filename('a/b:c*d?"e<f>g|h') == "a-b-c-d--e-f-g-h"


def test_normalize_url_strips_cik_padding():
    url = "http://www.sec.gov/Archives/edgar/data/0001690820/000169082026000059/x.htm"
    assert cls.normalize_url(url) == "https://www.sec.gov/Archives/edgar/data/1690820/000169082026000059/x.htm"


EXHIBIT_INDEX = """
<table>
<tr><td>4.1</td><td>Indenture, dated March 29, 2021 (incorporated by reference to
  <a href="https://www.sec.gov/Archives/edgar/data/1690820/000119312521100307/d151490dex41.htm">Exhibit 4.1</a>)</td></tr>
<tr><td>10.12</td><td><a href="cvna-20251231xex1012.htm">Credit Agreement</a> filed herewith</td></tr>
<tr><td>31.1</td><td>Certification (filed herewith)</td></tr>
<tr><td>99</td><td><a href="https://www.example.com/x.htm">elsewhere</a></td></tr>
</table>
"""


def test_exhibit_links():
    links = cls.exhibit_links(EXHIBIT_INDEX, "https://www.sec.gov/Archives/edgar/data/1690820/000169082026000009/")
    assert [(l.exhibit_no, l.url) for l in links] == [
        ("4.1", "https://www.sec.gov/Archives/edgar/data/1690820/000119312521100307/d151490dex41.htm"),
        ("10.12", "https://www.sec.gov/Archives/edgar/data/1690820/000169082026000009/cvna-20251231xex1012.htm"),
    ]
    assert links[0].description.startswith("Indenture, dated March 29, 2021")


@pytest.mark.parametrize(
    "opening, expected",
    [
        ("News Release Teck Reports Unaudited Third Quarter Results for 2026", "Results Release"),
        ("News Release Teck to Release Third Quarter 2026 Results on October 29, 2026", None),
        ("Management's Discussion and Analysis for the year ended", "MD&A"),
        ("Condensed Interim Consolidated Financial Statements (unaudited)", "Financial Statements"),
        ("Annual Information Form for the year ended December 31, 2025", "Annual Information Form"),
        ("Notice of Meeting and Management Proxy Circular", None),
    ],
)
def test_report_title(opening, expected):
    assert cls.report_title(opening) == expected


@pytest.mark.parametrize(
    "opening, expected",
    [
        (
            "Exhibit 99.1 Execution Copy TECK RESOURCES LIMITED, TECK METALS LTD., as Guarantor and THE BANK OF "
            "NEW YORK MELLON, as Trustee FIFTH SUPPLEMENTAL INDENTURE dated as of August 8, 2012 Supplemental to "
            "Indenture dated as of August 17, 2010 Creating series of Securities designated 2.500% Notes due 2018 "
            "3.750% Notes due 2023 5.400% Notes due 2043 TABLE OF CONTENTS Page 1. INTERPRETATIONS 2 1.1 Fifth "
            "Supplemental Indenture 2 6.25% Notes due 2099",
            "Fifth Supplemental Indenture - 2.500% Notes due 2018, 3.750% Notes due 2023, 5.400% Notes due 2043",
        ),
        (
            "EXHIBIT 99.1 EXECUTION VERSION TECK RESOURCES LIMITED, as Issuer 3.900% Notes Due 2030 INDENTURE "
            "Dated as of June 30, 2020 THE BANK OF NEW YORK MELLON, as Trustee CROSS-REFERENCE TABLE TIA Section",
            "Indenture - 3.900% Notes Due 2030",
        ),
        (
            "Exhibit 7.1 TECK RESOURCES LIMITED AND THE BANK OF NEW YORK MELLON, as Trustee INDENTURE Dated as of "
            "[ ] TECK RESOURCES LIMITED Reconciliation and tie between Trust Indenture Act of 1939 and Indenture",
            "Indenture",
        ),
        (
            "EXECUTION VERSION CREDIT AGREEMENT dated as of August 14, 2026, among Carvana Co., the lenders party "
            "thereto and Barclays Bank PLC, as administrative agent TABLE OF CONTENTS",
            "Credit Agreement - Barclays Bank PLC, administrative agent",
        ),
    ],
)
def test_cover_description(opening, expected):
    label, pos = cls.classify_at(opening)
    assert cls.cover_description(opening, pos) == expected


def test_real_indenture_cover_not_excluded_as_t1():
    # Indenture covers cite the Trust Indenture Act; only the T-1 eligibility statement is excluded.
    cover = "TECK RESOURCES LIMITED AND THE BANK OF NEW YORK MELLON, as Trustee INDENTURE Dated as of [ ] " \
            "Reconciliation and tie between Trust Indenture Act of 1939 and Indenture"
    assert cls.classify(cover) == cls.INDENTURE
    assert cls.is_form_of(cover[cover.index("INDENTURE"):])


def test_cover_title_stops_at_dated():
    opening = "TECK RESOURCES LIMITED FIRST SUPPLEMENTAL INDENTURE DATED JUNE 25, 2009 TABLE OF CONTENTS"
    label, pos = cls.classify_at(opening)
    assert cls.cover_description(opening, pos) == "First Supplemental Indenture"
    assert cls.agreement_date(opening[pos:]) == date(2009, 6, 25)
