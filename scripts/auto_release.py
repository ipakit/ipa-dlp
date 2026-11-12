#!/usr/bin/env python3
"""Create a GitHub release from a push to main.

The version in pyproject.toml is the source of truth:

* If the tag for that version does not exist, release it as-is.
* Otherwise bump the requested component (``auto`` means patch), commit the
  version change, tag it, push, and create the GitHub release.

The release commit is marked ``[skip ci]``. Pushes made with GITHUB_TOKEN do
not trigger another workflow run, but the marker keeps the intent explicit.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
VERSION_RE = re.compile(r'(?m)^version\s*=\s*"([^"]+)"\s*$')
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
RELEASE_TAG_RE = re.compile(r"^chore\(release\): (v\d+\.\d+\.\d+) ")


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=capture,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SystemExit(
            "command failed: %s\n%s" % (" ".join(cmd), detail)
        )
    return result


def git(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return run(["git", *args], **kwargs)


def output(**values: object) -> None:
    """Print and optionally append to GITHUB_OUTPUT."""
    target = os.environ.get("GITHUB_OUTPUT")
    lines = []
    for key, value in values.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        line = "%s=%s" % (key, value)
        lines.append(line)
        print(line)
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def read_version() -> str:
    match = VERSION_RE.search(PYPROJECT.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("could not find project.version in pyproject.toml")
    version = match.group(1)
    if not SEMVER_RE.match(version):
        raise SystemExit(
            "project.version must be X.Y.Z, found %r" % version
        )
    return version


def write_version(version: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    updated, count = VERSION_RE.subn(
        'version = "%s"' % version, text, count=1
    )
    if count != 1:
        raise SystemExit("could not update project.version in pyproject.toml")
    PYPROJECT.write_text(updated, encoding="utf-8")


def bump(version: str, component: str) -> str:
    match = SEMVER_RE.match(version)
    if not match:
        raise SystemExit("invalid version %r" % version)
    major, minor, patch = (int(part) for part in match.groups())
    if component == "major":
        return "%d.0.0" % (major + 1)
    if component == "minor":
        return "%d.%d.0" % (major, minor + 1)
    return "%d.%d.%d" % (major, minor, patch + 1)


def ensure_release(tag: str, version: str) -> None:
    view = run(["gh", "release", "view", tag], check=False)
    if view.returncode != 0:
        run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--title",
                "ipa-dlp %s" % version,
                "--generate-notes",
            ]
        )
        print("Created GitHub release %s" % tag)
    else:
        print("GitHub release %s already exists" % tag)


def tags() -> set[str]:
    result = git("tag", "--list")
    return set(result.stdout.splitlines())


def release_commit_for(trigger_sha: str) -> Optional[Tuple[str, str]]:
    """Return (commit, tag) for a release commit whose parent is the push."""
    if git("rev-parse", "--verify", "origin/main", check=False).returncode != 0:
        return None
    log = git(
        "log",
        "--format=%H%x09%P%x09%s",
        "-n",
        "40",
        "origin/main",
    )
    for line in log.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha, parents, subject = parts
        if trigger_sha not in parents.split():
            continue
        match = RELEASE_TAG_RE.match(subject)
        if match:
            return sha, match.group(1)
    return None


def recover_release(sha: str, tag: str) -> int:
    """Finish a partially-created release after a failed run."""
    print("Found an existing release commit for this push: %s" % tag)
    if tag not in tags():
        git("tag", tag, sha)
        git("push", "origin", tag)
        print("Recreated and pushed tag %s" % tag)
    ensure_release(tag, tag.lstrip("v"))
    output(skip=True, tag=tag)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bump",
        choices=("auto", "current", "patch", "minor", "major"),
        default="auto",
        help="version component to bump (auto keeps an unreleased current version)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without changing git or GitHub",
    )
    parser.add_argument(
        "--trigger-sha",
        default="",
        help="original push SHA, used to detect an already-created release",
    )
    args = parser.parse_args()

    git("fetch", "origin", "main", "--tags", "--force")
    trigger_sha = args.trigger_sha.strip() or git("rev-parse", "HEAD").stdout.strip()
    existing = release_commit_for(trigger_sha)
    if existing:
        sha, tag = existing
        if args.dry_run:
            print("would recover existing release %s at %s" % (tag, sha))
            output(skip=True, tag=tag)
            return 0
        return recover_release(sha, tag)

    current = read_version()
    known_tags = tags()
    current_tag = "v%s" % current

    # A re-run of the first release, or a tag created without a release,
    # leaves the current commit already tagged. Recover that release instead
    # of bumping again. Explicit patch/minor/major requests still bump.
    head = git("rev-parse", "HEAD").stdout.strip()
    if current_tag in known_tags and args.bump in ("auto", "current"):
        tagged_sha = git(
            "rev-list", "-n", "1", current_tag
        ).stdout.strip()
        if tagged_sha == head:
            if args.dry_run:
                print(
                    "would ensure the existing release for %s"
                    % current_tag
                )
                output(skip=True, tag=current_tag)
                return 0
            ensure_release(current_tag, current)
            output(skip=True, tag=current_tag)
            return 0

    if args.bump == "current":
        target = current
    elif args.bump == "auto":
        target = (
            current
            if current_tag not in known_tags
            else bump(current, "patch")
        )
    else:
        target = bump(current, args.bump)

    tag = "v%s" % target
    if tag in known_tags and args.bump == "auto":
        while tag in known_tags:
            target = bump(target, "patch")
            tag = "v%s" % target
    elif tag in known_tags and args.bump != "current":
        raise SystemExit(
            "computed tag %s already exists; refusing to overwrite" % tag
        )

    changed = target != current
    if args.dry_run:
        print(
            "would release %s (current %s, bump=%s, changed=%s)"
            % (tag, current, args.bump, changed)
        )
        output(version=target, tag=tag, changed=changed, skip=False)
        return 0

    if changed:
        write_version(target)
        git("add", "pyproject.toml")
        git("commit", "-m", "chore(release): %s [skip ci]" % tag)

    if tag not in tags():
        git("tag", tag)

    if changed:
        git("push", "origin", "HEAD:main")
    git("push", "origin", tag)

    ensure_release(tag, target)
    output(version=target, tag=tag, changed=changed, skip=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
