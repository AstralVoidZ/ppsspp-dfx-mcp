<!-- See CONTRIBUTING.md → Pull requests / Merging to main. The machine-checked
     part is enforced by the required `ci-ok` check; this list is what a reviewer
     would otherwise have to ask. -->

## What this changes

## Checklist

- [ ] Based on the current `main`, and the base commit's CI run was green.
- [ ] Ran the gate locally — `python scripts/check_gate.py` — and every step
      passed (not just `pytest`).
- [ ] If the tool surface changed, `tool_surface_baseline.json` was regenerated
      by `scripts/dump_tool_surface.py` in the same commit (never hand-edited).

## Merge method

- [ ] Squash merge (default), or a merge commit only if every commit on the
      branch builds.
