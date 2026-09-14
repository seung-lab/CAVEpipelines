# Releasing `cave-pipeline`

The package publishes to PyPI from a manual CI dispatch, via GitHub Actions OIDC trusted
publishing (no API token). CI computes the next version from the latest `vX.Y.Z` tag, and
`setuptools_scm` derives the package version from the tag it pushes.

## Cut a release
1. Land the changes on `main`; CI green.
2. Dispatch the release, naming the SemVer part to bump:
   ```bash
   gh workflow run ci.yml -f part=patch   # or minor / major; add -f dry-run=true to preview
   ```
3. CI (`ci.yml`) runs the tests, `version` computes the next tag, and `publish` tags, pushes,
   builds and uploads to PyPI. The published version equals the tag without the `v`.

## Versioning discipline (it's a public dependency)
PyChunkedGraph and PCGL2Cache pin `cave-pipeline[distribution]`, so `cave_pipeline.distribution`
is a public API: the `grid` functions, the `harness.run` signature, `exit_codes`, and
`run_and_exit`. Treat changes to it under SemVer — a breaking change is a **major** bump,
removals get a deprecation cycle. The operator-only modules (`cli`, `manifest`, `cost`, ...)
are not a stability contract for external consumers.

`cave_pipeline.contract` is a public standard too: PCG images are built to it, and the operator
checks each image against it rather than against any version. Changing a clause, or what
workers receive, breaks every image built to the old one, so it is a major change.

## One-time setup (already done if releases exist)
- **PyPI** → project `cave-pipeline` → *Trusted Publishers* → add: owner/repo of this
  repository, workflow `ci.yml`, environment `pypi`. For the very first upload (project does
  not exist yet) use PyPI's *pending publisher* flow.
- **GitHub** → repo *Settings → Environments* → create `pypi` (optionally add a required
  reviewer so each release is approved by a human).
