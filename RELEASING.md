# Releasing ipa-dlp

Pushing a commit to `main` starts the automated release pipeline:

1. `.github/workflows/auto-release.yml` reads the version from
   `pyproject.toml`.
   - If `v<version>` does not exist yet, it releases that version as-is.
   - Otherwise it bumps the patch version, writes `pyproject.toml`, commits
     `chore(release): vX.Y.Z [skip ci]`, tags it, pushes `main`, and creates a
     published GitHub release.
2. After creating the release, auto-release dispatches
   `.github/workflows/release.yml` for that tag with `gh workflow run`.
   The publish workflow:
   - verifies the tag matches `pyproject.toml`,
   - builds the sdist/wheel and publishes to PyPI using trusted publishing,
   - updates the `ipakit/homebrew-ipa-dlp` formula and README with the new
     tag and archive SHA-256, then commits and pushes the tap.

   The explicit dispatch is required because events created with
   `GITHUB_TOKEN` do not start new workflow runs. `release.yml` also listens
   for `release: published`, so a manually published GitHub release still
   publishes normally.

The Homebrew job waits for PyPI, so a failed PyPI publish does not leave brew
users pointing at an unpublished version.

## One-time setup

### 1. PyPI trusted publisher

The `ipa-dlp` project does not exist on PyPI yet. Add a **pending trusted
publisher** at <https://pypi.org/manage/account/publishing/>:

- PyPI project name: `ipa-dlp`
- Owner: `ipakit`
- Repository name: `ipa-dlp`
- Workflow filename: `release.yml`
- Environment: leave blank

The workflow uses OIDC (`id-token: write`), so no PyPI API token is needed.

Alternative: add a repository secret `PYPI_API_TOKEN` instead. If it is set,
the workflow uses it and skips the OIDC step. For a brand-new project, an
account-scoped PyPI token is simplest; switch to a project-scoped token after
the first publish.

If the name `ipa-dlp` is unavailable on PyPI, change `[project].name` in
`pyproject.toml` and the trusted publisher / token scope to match.

### 2. Tap write token

Create a GitHub token that can write to `ipakit/homebrew-ipa-dlp`:

- Preferred: a fine-grained PAT with **Contents: Read and write** on only
  that repository.
- A classic PAT with `repo` scope also works.

Add it as a repository secret in `ipakit/ipa-dlp` named `TAP_GITHUB_TOKEN`
(Settings → Secrets and variables → Actions → New repository secret).

If `ipakit/homebrew-ipa-dlp` protects `main`, allow this token to bypass the
tap's branch protection rule as well.

### 3. Allow the release commit to reach main

The auto-release job pushes the version-bump commit and tag with the workflow's
`GITHUB_TOKEN`. If `main` is protected, allow GitHub Actions to bypass the
branch protection rule, or the job will stop when it tries to push. If `main`
requires signed commits, the default bot commit is unsigned, so either disable
that rule for the release commit or use a signing setup.

Also check Settings → Actions → General → Workflow permissions: the workflow
declares `contents: write`, which is sufficient when the repository policy
allows write access.

## First release

Before pushing the workflow commit:

1. Configure the PyPI pending publisher.
2. Add `TAP_GITHUB_TOKEN`.
3. Confirm `pyproject.toml` has the version you want to release
   (currently `0.3.0`).

Then push to `main`. The first run will release `v0.3.0` because that tag does
not exist yet. Watch **Actions → Auto release → Publish release**.

Verify afterward:

```sh
pip index versions ipa-dlp
pip install --upgrade ipa-dlp
brew update && brew upgrade ipa-dlp
brew info ipa-dlp
```

`brew upgrade` only sees the new version after the tap push completes, so
allow a minute and run `brew update` again if needed.

## Manual operations

- **Dry run:** Actions → Auto release → Run workflow → set `dry_run` to true.
  This prints the version/tag plan without changing git or GitHub.
- **Manual release:** Actions → Auto release → Run workflow and choose a bump
  (`auto`, `current`, `patch`, `minor`, or `major`).
- **Retry PyPI/tap:** Actions → Publish release → Run workflow with the tag
  (for example `v0.3.0`). The PyPI upload uses `skip-existing: true`, and the
  tap job is a no-op if the formula already points at that tag.
- **Manual GitHub release:** publishing a release normally also triggers
  `release.yml`; the explicit auto-release dispatch is only needed because
  `GITHUB_TOKEN`-created releases do not emit workflow-triggering events.
- **Recover a partial auto-release:** Re-run Auto release for the same push.
  If the version-bump commit already exists, the script recreates the missing
  tag/release instead of bumping again.

## Versioning rules

- `pyproject.toml` is the source of truth.
- `auto` uses the current version when its tag is missing, otherwise bumps
  patch.
- `current` releases the current version and can create a missing release for
  an existing tag.
- `patch`, `minor`, and `major` always bump that component.
- PyPI versions are immutable; `skip-existing` makes retries safe.
- The release commit is marked `[skip ci]`, and pushes made with
  `GITHUB_TOKEN` do not start another workflow run.

## Tap formula layout

The generated formula installs `ipa-dlp` and `ipa_dlp.py` into `libexec`,
rewrites the wrapper shebang to Homebrew's Python, and creates a `bin/ipa-dlp`
exec shim. This keeps `import ipa_dlp` working without a site-packages install.

The old double-clickable `ipa-dlp.command` wizard is not part of the current
source tree, so the generated tap README no longer documents it. If you want
to restore the wizard, add it back to the repo and extend the formula and
`scripts/update_homebrew.py`.
