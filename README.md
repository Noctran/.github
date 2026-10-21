# Noctran/.github

Conventions shared by every Noctran repository unless that repository says otherwise.
Public on purpose: Renovate reads presets from it without a token, and GitHub uses it as the
community-health fallback for repositories that carry no files of their own.

## Renovate

| File | Purpose |
|------|---------|
| `renovate-config.json` | Default preset. Renovate offers it automatically when it onboards a repository. Extend with `github>Noctran/.github:renovate-config`. |
| `soak.json` | 3-day soak for images outside Docker Hub, which have no release dates. Extend with `github>Noctran/.github:soak` only together with the caller workflow, or those updates never get a pull request. |
| `actions/renovate-soak/` | Composite action that sets the `renovate-soak` status on every `renovate/` branch. Input `exempt`: image prefixes that never soak (a repository's own releases), one per line; pair it with `prCreation: 'immediate'` for the same images in that repository's Renovate config. |
| `examples/renovate-soak.yml` | The caller workflow each repository with the soak preset carries in `.github/workflows/`; replace `<commit-sha>` with a commit of this repository. |

A project repository's `renovate.json5`:

```json5
{
  extends: ['github>Noctran/.github:renovate-config', 'github>Noctran/.github:soak'],
}
```

`actions/renovate-soak/renovate_soak.py` is a copy of Homelab's `scripts/utils/renovate_soak.py` at
commit 1cec803; once Homelab uses this action, its own copy goes.

## Checks

`.github/workflows/ci.yml` runs on every pull request and push to main: the soak script's self-test, ruff
(`ruff.toml`) and Renovate's config validator on both presets. Repositories pin the action to a commit and
extend the presets from main, so change main through a pull request with CI green. This repository's own
`renovate.json` extends its preset, so Renovate keeps the CI pins current once the App is installed here.
