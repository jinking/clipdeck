from __future__ import annotations

import re


_DOI_REGEX = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", re.IGNORECASE)
_PMID_REGEX = re.compile(r"\b(?:PMID|PubMed(?:\s*ID)?)[ \t:]*(\d{6,9})\b", re.IGNORECASE)
_PMCID_REGEX = re.compile(r"\b(PMC\d{6,8})\b", re.IGNORECASE)
_NCT_REGEX = re.compile(r"\b(NCT\d{8})\b", re.IGNORECASE)
_CHICTR_REGEX = re.compile(r"\b(ChiCTR(?:-[A-Za-z0-9]+)?\d{6,10})\b", re.IGNORECASE)
_ORCID_REGEX = re.compile(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b", re.IGNORECASE)


def extract_identifiers(text: str) -> list[dict[str, str]]:
    """Extract academic and clinical trial identifiers from text.

    Supports DOI, PMID, PMCID, ClinicalTrials (NCT), ChiCTR, and ORCID.
    Returns a deduplicated list of structured records.
    """
    if not text:
        return []

    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    # 1. DOI
    for match in _DOI_REGEX.finditer(text):
        val = match.group(0).rstrip(".,;)>]")
        key = ("DOI", val.lower())
        if key not in seen:
            seen.add(key)
            results.append({"type": "DOI", "value": val})

    # 2. PMID
    for match in _PMID_REGEX.finditer(text):
        val = match.group(1)
        key = ("PMID", val)
        if key not in seen:
            seen.add(key)
            results.append({"type": "PMID", "value": val})

    # 3. PMCID
    for match in _PMCID_REGEX.finditer(text):
        val = match.group(1).upper()
        key = ("PMCID", val)
        if key not in seen:
            seen.add(key)
            results.append({"type": "PMCID", "value": val})

    # 4. NCT
    for match in _NCT_REGEX.finditer(text):
        val = match.group(1).upper()
        key = ("NCT", val)
        if key not in seen:
            seen.add(key)
            results.append({"type": "NCT", "value": val})

    # 5. ChiCTR
    for match in _CHICTR_REGEX.finditer(text):
        val = match.group(1)
        key = ("ChiCTR", val.upper())
        if key not in seen:
            seen.add(key)
            results.append({"type": "ChiCTR", "value": val})

    # 6. ORCID
    for match in _ORCID_REGEX.finditer(text):
        val = match.group(1)
        key = ("ORCID", val)
        if key not in seen:
            seen.add(key)
            results.append({"type": "ORCID", "value": val})

    return results
