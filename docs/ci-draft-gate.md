# CI draft gate

Standard for repositories that Copier does not manage: CI does not run on draft pull
requests. An agent works in a draft against local gates and promotes the PR to ready
when done, which starts the first CI run. Best case is one run per PR. Projects
generated from these templates already have it; run `copier update` to pick it up.

## Workflow trigger

Add `ready_for_review` to the `pull_request` types (keep any branch or path filters):

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
```

Without it, promoting a draft would start nothing. `push`, `schedule`, and
`workflow_dispatch` triggers stay as they are.

## Job condition

Give every job that can run on `pull_request` this condition:

```yaml
jobs:
  checks:
    if: github.event.pull_request.draft != true
```

The expression is also true on `push` and the other events, where there is no pull
request. Combine it with an existing condition using `&&`, and keep the gate job
(the one branch protection requires) on the same condition:

```yaml
  gate:
    if: always() && github.event.pull_request.draft != true
    needs: [checks]
```

A job skipped by `if:` reports as skipped, which a required check accepts, so a draft
shows no failing check. Jobs with `needs:` on skipped jobs are skipped on their own, but
still add the condition so the intent is explicit.

## Concurrency group

When the workflow cancels older runs (`cancel-in-progress: true`), put the draft flag in
the group. Otherwise a push to a draft right before promoting starts an all-skipped run
that can cancel the `ready_for_review` run doing the work:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}-${{ github.event.pull_request.draft == true }}
  cancel-in-progress: true
```

## AGENTS.md rule

```markdown
- Pull requests: open as a draft and work against the local gates; CI does not run
  on drafts. When done, merge the latest `main`, then mark the PR ready, which
  starts the first CI run. Fix red CI while the PR stays ready (never back to
  draft), then request the owner's review and notify them once it is green.
```

## Notes

- The rule never moves a PR back to draft just because CI is red.
- Re-run or push fixes while the PR is ready; each push is one more run, so run the
  local gates before promoting.
