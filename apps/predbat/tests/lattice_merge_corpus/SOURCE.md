# Vendored Lattice merge corpus

Copied verbatim from the canonical source:
`Predictive-Cloud-Ltd/lattice-spec` → `conformance/merge/{cases.json,expected.json}`.

The golden is **normalized**: the merged `site.docVersion` is removed (it is an
implementation-defined content digest). batpred's `merge` is pinned to produce the same
`{site, warnings}` after the same normalization. Refresh by re-copying both files when the
canonical corpus changes.
