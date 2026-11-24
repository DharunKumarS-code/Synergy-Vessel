"""Phase 9 -- Excel Location Intelligence.

Standardises the location text found in imported workbooks (Deck Photos headings, register Official Location
cells) against the locations the dataset already uses. Layered: deterministic rules -> existing-location
matching -> optional AI suggestion (separate configuration; mock only in this version). The original Excel text
is never changed; only high-confidence rule-based matches are applied automatically, and every AI suggestion
needs an administrator's approval.

Fully independent of the AI condition-analysis feature (``app.ai``): no shared settings, keys, providers or
key loaders.
"""
