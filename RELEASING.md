# Releasing `cave-pipeline`

The package publishes to PyPI on a version tag, via GitHub Actions OIDC trusted
publishing (no API token). Version is derived from the tag by `setuptools_scm`.

## Cut a release
1. Land the changes on `main`; CI green.
2. Tag and push:
   ```bash
   git tag v0.1.0      # vMAJOR.MINOR.PATCH (SemVer; pre-release e.g. v0.2.0-rc.1)
   git push origin v0.1.0
   ```
3. CI (`ci.yml`) runs `validate-tag` (rejects non-SemVer tags) → `publish` (builds and
   uploads to PyPI). The published version equals the tag without the `v`.

## Versioning discipline (it's a public dependency)
PyChunkedGraph and PCGL2Cache pin `cave-pipeline[distribution]`, so `cave_pipeline.distribution`
is a public API: the `grid` functions, the `harness.run` signature, `exit_codes`, and
`run_and_exit`. Treat changes to it under SemVer — a breaking change is a **major** bump,
removals get a deprecation cycle. The operator-only modules (`cli`, `manifest`, `cost`, ...)
are not a stability contract for external consumers.

## One-time setup (already done if releases exist)
- **PyPI** → project `cave-pipeline` → *Trusted Publishers* → add: owner/repo of this
  repository, workflow `ci.yml`, environment `pypi`. For the very first upload (project does
  not exist yet) use PyPI's *pending publisher* flow.
- **GitHub** → repo *Settings → Environments* → create `pypi` (optionally add a required
  reviewer so each release is approved by a human).
