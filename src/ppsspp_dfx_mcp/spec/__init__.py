"""Spec package — wire-contract definitions and the manifest-driven script registry.

Modules:
- `script_manifest` — manifest-driven script registry.
- `error_codes` — the canonical error-code registry.
- `output_contract` — the tool output-contract compiler
  (`derive_output_contract` / `flatten_union`).
- `tool_surface_policy` — governance registries (dynamic-input exemptions,
  multi-shape output tools, conditional-required params, destructive-hint
  policy).

These are contract layers, not tool implementations: the guard tests and the
schema-surface gate script read the same registries, so there is a single
source of truth.
"""
