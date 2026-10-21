#!/usr/bin/env python3
"""Holds Renovate branches for images outside Docker Hub until their update is SOAK_DAYS old (REPO-004 section 3).

Renovate's minimumReleaseAge needs a release date, and its docker datasource has one only for Docker Hub. For
ghcr.io and other registries renovate.json5 sets minimumReleaseAgeBehaviour=timestamp-optional, so Renovate pushes
those branches at once. This script holds them instead: it counts SOAK_DAYS from the first time it saw a branch's
exact change and reports the result as the `renovate-soak` commit status on the branch head. The same rule sets
prCreation=not-pending, so Renovate opens the PR only once that status has turned green (or red), and the update
waits as a bare branch under "Pending Status Checks" on the Dependency Dashboard, not in the PR list. Renovate's
own automerge waits for every status too (platformAutomerge is off for these images, renovate.json5).

"The exact change" is a fingerprint of the branch's added lines against the default branch, so a rebase keeps the
count and a new version restarts it. The first-seen times live in one ledger comment on the Dependency Dashboard
issue, written by the workflow's own bot account; a ledger by anyone else is ignored, so a comment cannot shorten
the soak. Entries whose change no longer exists on any Renovate branch are dropped on every run.

Runs in .github/workflows/renovate-soak.yml on every push to a renovate/ branch and every 3 hours.
Local run (needs an authenticated gh CLI with statuses and issues write on the repository):
  GH_REPO=Noctran/Homelab scripts/utils/renovate_soak.py
  scripts/utils/renovate_soak.py --self-test

Exit status: 0 every Renovate branch was processed, 1 a gh or API call failed or the dashboard is missing.
Standard library only.
"""

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse

STATUS_CONTEXT = "renovate-soak"
MARKER_RE = re.compile(r"<!-- renovate-soak fingerprint=(?P<fingerprint>[0-9a-f]+) since=(?P<since>\S+) -->")
# An image reference after `image:` (compose, role defaults written as YAML) or `FROM` (Dockerfiles).
IMAGE_REF_RE = re.compile(
    r"""(?:\bimage:\s*["']?|^\s*FROM\s+(?:--platform=\S+\s+)?)(?P<ref>[^\s"'#]+)""", re.IGNORECASE
)
DOCKER_HUB_HOSTS = {"docker.io", "index.docker.io", "registry-1.docker.io"}
DASHBOARD_TITLE = "Dependency Dashboard"
LEDGER_HEADING = "**Soak ledger** (`renovate-soak`, REPO-004)"


def gh_api(path, method="GET", fields=None):
    """Calls `gh api` and returns the parsed JSON (None for an empty body). Raises on any API error."""
    command = ["gh", "api", "--method", method, path]
    for key, value in (fields or {}).items():
        command += ["-f", f"{key}={value}"]
    output = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    return json.loads(output) if output.strip() else None


def gh_api_pages(path):
    """Collects every page of a list endpoint (100 per page)."""
    items, page = [], 1
    separator = "&" if "?" in path else "?"
    while True:
        batch = gh_api(f"{path}{separator}per_page=100&page={page}")
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def added_lines(files):
    """The added lines of a PR's patches, without the leading `+`, in file order."""
    lines = []
    for changed_file in files:
        for line in (changed_file.get("patch") or "").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:])
    return lines


def registry_host(image_ref):
    """The registry host of an image reference, or None for Docker Hub's short form (`name`, `org/name`)."""
    first_component = image_ref.split("/", 1)[0]
    if "/" in image_ref and ("." in first_component or ":" in first_component or first_component == "localhost"):
        return first_component
    return None


def parse_exempt(text):
    """The caller's exempt prefixes, one per line (the action's `exempt` input); blank lines and `#` lines skipped."""
    stripped = (line.strip() for line in (text or "").splitlines())
    return tuple(line for line in stripped if line and not line.startswith("#"))


def needs_soak(lines, exempt=()):
    """True when an added line names an image on a registry other than Docker Hub (no release dates there).

    An image whose reference starts with one of the `exempt` prefixes never needs the soak: the caller's own
    releases, which the soak protects nothing against (for example `registry.example.com/myapp/`)."""
    for line in lines:
        for match in IMAGE_REF_RE.finditer(line):
            image_ref = match.group("ref")
            host = registry_host(image_ref)
            if host and host not in DOCKER_HUB_HOSTS and not image_ref.startswith(exempt):
                return True
    return False


def fingerprint(lines):
    """Identifies the change itself: stable across rebases, different for every new version."""
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()[:16]


def find_ledger(comments, bot_login):
    """The bot's ledger comment and its entries {fingerprint: since}, or (None, {}) when there is none."""
    for comment in comments:
        if comment["user"]["login"] == bot_login and LEDGER_HEADING in comment["body"]:
            entries = {m["fingerprint"]: m["since"] for m in MARKER_RE.finditer(comment["body"])}
            return comment, entries
    return None, {}


def ledger_body(entries, soak_days):
    """The ledger comment: one hidden marker and one readable line per soaking change, oldest first."""
    lines = [
        LEDGER_HEADING,
        "",
        (
            f"Renovate has no release date for these images, so their branches wait {soak_days} days from the first "
            "time the soak workflow saw each change; Renovate opens the pull request on its next run after that. "
            "Maintained by the workflow, please do not edit."
        ),
        "",
    ]
    for change_fingerprint, (since, branch) in sorted(entries.items(), key=lambda item: (item[1][0], item[0])):
        ready_at = since + dt.timedelta(days=soak_days)
        lines.append(
            f"<!-- renovate-soak fingerprint={change_fingerprint} since={since:%Y-%m-%dT%H:%M:%SZ} -->\n"
            f"- `{branch}`: first seen {since:%Y-%m-%d %H:%M} UTC, ready {ready_at:%Y-%m-%d %H:%M} UTC"
        )
    if not entries:
        lines.append("Nothing is soaking.")
    return "\n".join(lines)


def parse_since(text):
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)


def soak_state(since, now, soak_days):
    """(state, description) for the commit status; a description stays within GitHub's 140 characters."""
    ready_at = since + dt.timedelta(days=soak_days)
    if now >= ready_at:
        return "success", f"Soaked {soak_days} days (first seen {since:%Y-%m-%d %H:%M} UTC)"
    return "pending", f"Soaking until {ready_at:%Y-%m-%d %H:%M} UTC (first seen {since:%Y-%m-%d %H:%M} UTC)"


def current_status(repo, sha):
    """The latest (state, description) of this context on a commit, or None."""
    for status in gh_api(f"repos/{repo}/commits/{sha}/statuses?per_page=100"):
        if status["context"] == STATUS_CONTEXT:
            return status["state"], status["description"]
    return None


def set_status(repo, sha, state, description, target_url):
    if current_status(repo, sha) == (state, description):
        return
    fields = {"state": state, "context": STATUS_CONTEXT, "description": description}
    if target_url:
        fields["target_url"] = target_url
    gh_api(f"repos/{repo}/statuses/{sha}", method="POST", fields=fields)


def branch_lines(repo, default_branch, branch):
    """The added lines of a branch against the default branch (the change its PR would carry)."""
    basehead = urllib.parse.quote(f"{default_branch}...{branch}", safe="/.")
    return added_lines(gh_api(f"repos/{repo}/compare/{basehead}").get("files") or [])


def find_dashboard(repo):
    """The number of the open Dependency Dashboard issue opened by a bot, or None."""
    for issue in gh_api_pages(f"repos/{repo}/issues?state=open"):
        if issue["title"] == DASHBOARD_TITLE and issue["user"]["type"] == "Bot" and "pull_request" not in issue:
            return issue["number"]
    return None


def main():
    repo = os.environ["GH_REPO"]
    soak_days = int(os.environ.get("SOAK_DAYS", "3"))
    exempt = parse_exempt(os.environ.get("SOAK_EXEMPT"))
    bot_login = os.environ.get("SOAK_BOT_LOGIN", "github-actions[bot]")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    default_branch = gh_api(f"repos/{repo}")["default_branch"]
    dashboard = find_dashboard(repo)
    if dashboard is None:
        print(f"::error::no open '{DASHBOARD_TITLE}' issue by a bot; the soak ledger lives there")
        return 1
    comment, recorded = find_ledger(gh_api_pages(f"repos/{repo}/issues/{dashboard}/comments"), bot_login)
    ledger_url = comment["html_url"] if comment else None

    failures, soaking, statuses = 0, {}, []
    for ref in gh_api(f"repos/{repo}/git/matching-refs/heads/renovate/") or []:
        branch, sha = ref["ref"].removeprefix("refs/heads/"), ref["object"]["sha"]
        try:
            lines = branch_lines(repo, default_branch, branch)
        except subprocess.CalledProcessError as error:
            failures += 1
            print(f"::error::{branch}: gh api failed: {error.stderr.strip()}")
            continue
        if not needs_soak(lines, exempt):
            statuses.append(
                (
                    branch,
                    sha,
                    "success",
                    "No soak needed: Renovate's own minimumReleaseAge covers this update, or the caller exempts it",
                )
            )
            continue
        change_fingerprint = fingerprint(lines)
        since = parse_since(recorded[change_fingerprint]) if change_fingerprint in recorded else now
        soaking[change_fingerprint] = (since, branch)
        statuses.append((branch, sha, *soak_state(since, now, soak_days)))

    # The ledger is written before the statuses, so a pending status always links to an entry that exists.
    body = ledger_body(soaking, soak_days)
    try:
        if comment and comment["body"] != body:
            ledger_url = gh_api(f"repos/{repo}/issues/comments/{comment['id']}", method="PATCH", fields={"body": body})[
                "html_url"
            ]
        elif not comment and soaking:
            ledger_url = gh_api(f"repos/{repo}/issues/{dashboard}/comments", method="POST", fields={"body": body})[
                "html_url"
            ]
    except subprocess.CalledProcessError as error:
        print(f"::error::ledger on #{dashboard}: gh api failed: {error.stderr.strip()}")
        return 1

    for branch, sha, state, description in statuses:
        try:
            set_status(repo, sha, state, description, ledger_url if state == "pending" else None)
            print(f"{branch}: {state}, {description}")
        except subprocess.CalledProcessError as error:
            failures += 1
            print(f"::error::{branch}: gh api failed: {error.stderr.strip()}")
    return 1 if failures else 0


def expect(condition, detail=""):
    """An assert that `python -O` cannot strip: the self-test is a gate, so it must never pass by vanishing."""
    if not condition:
        raise AssertionError(detail)


def self_test():
    compose_patch = [
        "    image: ghcr.io/kiwix/kiwix-serve:3.8.3@sha256:" + "a" * 64,
        "    image: linuxserver/sonarr:4.0.21.1-ls327@sha256:" + "b" * 64,
    ]
    expect(needs_soak(compose_patch[:1]))
    expect(not needs_soak(compose_patch[1:]))
    expect(not needs_soak(['    image: "docker.io/library/postgres:17.6"']))
    expect(not needs_soak(["FROM alpine:3.24@sha256:" + "c" * 64]))
    expect(needs_soak(["FROM --platform=linux/arm64 ghcr.io/actions/actions-runner:2.330.0"]))
    expect(not needs_soak(["uses: actions/checkout@abc # v7", "rev: v1.2.3"]))
    exempt = parse_exempt("\n# own releases\nregistry.int.cloudking.at/sleepcalcweb/\n")
    expect(exempt == ("registry.int.cloudking.at/sleepcalcweb/",), exempt)
    own_release = ["    image: registry.int.cloudking.at/sleepcalcweb/sleepcalcweb-backend:0.2.0@sha256:" + "d" * 64]
    expect(needs_soak(own_release))
    expect(not needs_soak(own_release, exempt))
    expect(needs_soak(["    image: registry.int.cloudking.at/other/app:1.0"], exempt))
    expect(needs_soak(own_release + compose_patch[:1], exempt))
    expect(registry_host("localhost:5000/app:1") == "localhost:5000")
    expect(fingerprint(compose_patch) == fingerprint(list(reversed(compose_patch))))
    expect(fingerprint(compose_patch) != fingerprint(compose_patch[:1]))

    since = dt.datetime(2026, 10, 6, 17, 0, tzinfo=dt.UTC)
    body = ledger_body({"0123abcd": (since, "renovate/ghcr.io-kiwix"), "4567ef01": (since, "renovate/other")}, 3)
    comments = [
        {"user": {"login": "someone"}, "body": body.replace("2026-10-06", "2020-01-01")},
        {"user": {"login": "github-actions[bot]"}, "body": body},
    ]
    _, entries = find_ledger(comments, "github-actions[bot]")
    expect(entries == {"0123abcd": "2026-10-06T17:00:00Z", "4567ef01": "2026-10-06T17:00:00Z"}, entries)
    expect(find_ledger(comments[:1], "github-actions[bot]") == (None, {}))
    expect(parse_since(entries["0123abcd"]) == since)
    expect("Nothing is soaking." in ledger_body({}, 3))

    state, description = soak_state(since, since + dt.timedelta(days=2, hours=23), 3)
    expect(state == "pending", description)
    expect(len(description) <= 140, description)
    state, description = soak_state(since, since + dt.timedelta(days=3), 3)
    expect(state == "success", description)
    expect(len(description) <= 140, description)
    print("renovate_soak self-test: ok")
    return 0


if __name__ == "__main__":
    sys.exit(self_test() if sys.argv[1:] == ["--self-test"] else main())
