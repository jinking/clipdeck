from __future__ import annotations

from sci_radar.ingestion.metadata import extract_identifiers


def test_extract_identifiers_empty_and_none() -> None:
    assert extract_identifiers("") == []
    assert extract_identifiers("   ") == []


def test_extract_academic_identifiers_comprehensive() -> None:
    sample_text = """
    # Research Paper
    DOI: 10.1016/j.cell.2024.01.001.
    Duplicate DOI: https://doi.org/10.1016/j.cell.2024.01.001
    PubMed ID: 38245678, also PMID: 38245678.
    Indexed in PMC87654321.
    Trial registered under NCT04567890 and ChiCTR2100045678.
    Author ORCID: 0000-0002-1825-0097.
    """
    results = extract_identifiers(sample_text)
    types_and_values = {(item["type"], item["value"]) for item in results}

    assert ("DOI", "10.1016/j.cell.2024.01.001") in types_and_values
    assert ("PMID", "38245678") in types_and_values
    assert ("PMCID", "PMC87654321") in types_and_values
    assert ("NCT", "NCT04567890") in types_and_values
    assert ("ChiCTR", "ChiCTR2100045678") in types_and_values
    assert ("ORCID", "0000-0002-1825-0097") in types_and_values
    assert len(results) == 6
