#!/usr/bin/env python3
"""Update the ipakit/homebrew-ipa-dlp tap for a released tag.

The formula installs the CLI wrapper and module into libexec, then puts a
small exec shim in bin. That keeps ``import ipa_dlp`` working without relying
on a Python site-packages install.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from string import Template

TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")
FORMULA_PATH = Path("Formula/ipa-dlp.rb")

FORMULA = Template(
    '''class IpaDlp < Formula
  include Language::Python::Shebang

  desc "Fetch and decrypt App Store IPAs for apps your Apple ID owns"
  homepage "https://github.com/ipakit/ipa-dlp"
  url "https://github.com/ipakit/ipa-dlp/archive/refs/tags/${tag}.tar.gz"
  sha256 "${sha256}"
  license "MIT"
  head "https://github.com/ipakit/ipa-dlp.git", branch: "main"

  livecheck do
    url :stable
    strategy :github_latest
  end

  depends_on :macos
  depends_on "python@3.14"

  def install
    libexec.install "ipa-dlp", "ipa_dlp.py"
    chmod 0755, libexec/"ipa-dlp"
    rewrite_shebang detected_python_shebang, libexec/"ipa-dlp"
    (bin/"ipa-dlp").write_exec_script libexec/"ipa-dlp"
  end

  test do
    assert_match(/ipa-dlp \\d+\\.\\d+\\.\\d+/, shell_output("#{bin}/ipa-dlp --version"))
  end
end
'''
)

README = """# homebrew-ipa-dlp

Homebrew tap for [ipa-dlp](https://github.com/ipakit/ipa-dlp) — a macOS
wrapper around [ipatool](https://github.com/majd/ipatool) and
[UnfairPlay](https://github.com/DrWhax/unfairplay) that downloads and decrypts
App Store IPAs for apps your Apple ID owns.

## Install

```sh
brew tap ipakit/ipa-dlp
brew install ipa-dlp
```

If Homebrew complains that the tap is untrusted (some Homebrew 7 builds gate
third-party taps), run `brew trust ipakit/ipa-dlp` and retry.

Then create the pinned tool copies and sign in:

```sh
ipa-dlp setup
```

## Use

```sh
ipa-dlp get spotify --version 9.1.88
```

The final path is printed on stdout. Account data is stripped by default.

## Upgrade

```sh
brew update && brew upgrade ipa-dlp
```

## Uninstall

```sh
brew uninstall ipa-dlp
brew untap ipakit/ipa-dlp
```

Removing the formula does not delete anything ipa-dlp wrote under
`~/.local/share/ipa-dlp`, `~/Library/Caches/ipa-dlp`, or
`~/Library/Application Support/unfairplay`. Remove those by hand if you want a
clean slate.

## Requirements

Apple Silicon Mac, macOS with a working App Store / FairPlay enrollment, and an
Apple ID that owns or can obtain the requested apps. See the
[main README](https://github.com/ipakit/ipa-dlp#readme) for limitations and
legal notes.
"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_archive(tag: str) -> Path:
    url = "https://github.com/ipakit/ipa-dlp/archive/refs/tags/%s.tar.gz" % tag
    handle = tempfile.NamedTemporaryFile(
        prefix="ipa-dlp-%s-" % tag,
        suffix=".tar.gz",
        delete=False,
    )
    path = Path(handle.name)
    handle.close()
    try:
        subprocess.run(
            ["curl", "-fsSL", "--retry", "3", "-o", str(path), url],
            check=True,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def validate_archive(path: Path, tag: str) -> None:
    """Fail closed if the release archive is not the 0.3+ layout."""
    version = tag.lstrip("v")
    try:
        with tarfile.open(path, "r:gz") as archive:
            names = archive.getnames()
            if not any(name.endswith("/ipa-dlp") for name in names):
                raise SystemExit("release archive has no ipa-dlp wrapper")
            if not any(name.endswith("/ipa_dlp.py") for name in names):
                raise SystemExit(
                    "release archive has no ipa_dlp.py; the tap formula "
                    "requires the 0.3.0+ layout"
                )
            pyprojects = [
                name
                for name in names
                if re.fullmatch(r"[^/]+/pyproject\.toml", name)
            ]
            if not pyprojects:
                raise SystemExit("release archive has no pyproject.toml")
            handle = archive.extractfile(pyprojects[0])
            if handle is None:
                raise SystemExit("could not read pyproject.toml from archive")
            text = handle.read().decode("utf-8")
            match = re.search(
                r'(?m)^version\s*=\s*"([^"]+)"\s*$', text
            )
            if not match or match.group(1) != version:
                found = match.group(1) if match else "unknown"
                raise SystemExit(
                    "archive version %s does not match tag %s"
                    % (found, tag)
                )
    except tarfile.TarError as exc:
        raise SystemExit("could not inspect release archive: %s" % exc)


def write_if_changed(path: Path, content: str, dry_run: bool) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="release tag, e.g. v0.3.0")
    parser.add_argument(
        "--tap-dir",
        required=True,
        help="path to a checkout of ipakit/homebrew-ipa-dlp",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="download and render, but do not write tap files",
    )
    parser.add_argument(
        "--archive",
        help="use a local release archive instead of downloading the tag",
    )
    args = parser.parse_args()

    match = TAG_RE.match(args.tag.strip())
    if not match:
        raise SystemExit("tag must look like v1.2.3")
    version = match.group(1)
    tag = "v%s" % version
    tap_dir = Path(args.tap_dir).expanduser().resolve()
    if not tap_dir.is_dir():
        raise SystemExit("tap directory not found: %s" % tap_dir)

    cleanup = False
    if args.archive:
        archive = Path(args.archive).expanduser().resolve()
        if not archive.is_file():
            raise SystemExit("archive not found: %s" % archive)
    else:
        archive = fetch_archive(tag)
        cleanup = True
    try:
        validate_archive(archive, tag)
        digest = sha256_file(archive)
    finally:
        if cleanup:
            archive.unlink(missing_ok=True)

    formula = FORMULA.substitute(tag=tag, sha256=digest)
    formula_path = tap_dir / FORMULA_PATH
    readme_path = tap_dir / "README.md"
    changed_formula = write_if_changed(formula_path, formula, args.dry_run)
    changed_readme = write_if_changed(readme_path, README, args.dry_run)

    action = "would update" if args.dry_run else "updated"
    for path, changed in (
        (formula_path, changed_formula),
        (readme_path, changed_readme),
    ):
        if changed:
            print("%s %s" % (action, path))
        else:
            print("unchanged %s" % path)
    print("version=%s" % version)
    print("tag=%s" % tag)
    print("sha256=%s" % digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
