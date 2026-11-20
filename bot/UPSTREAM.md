# Upstream

The code in this directory is a copy of remnashop.

- Source: https://github.com/remnashop/remnashop
- Commit: 06350c6cb3e1f13b4da316852f102bee6bf26949 (2026-06-23)
- License: MIT, see `LICENSE` in this directory (kept unchanged).

The first commit that adds this directory contains the upstream code without changes.
Every later change to upstream files is a separate commit, so `git log -- bot/` shows the full
difference from upstream.

## Removed from upstream

- `.github/workflows/prod-docker-release.yml`, `.github/workflows/dev-docker-release.yml`: they
  build and push upstream's Docker images to its registry. GitHub ignores workflows outside the
  repository root anyway; StarterKit's CI is `.github/workflows/ci.yml` at the root.
