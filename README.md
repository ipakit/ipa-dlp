# ipa-dlp

A macOS CLI and Python library for fetching, decrypting, and extracting App
Store IPAs and Mach-O binaries (powered by
[ipatool](https://github.com/majd/ipatool) and
[UnfairPlay](https://github.com/DrWhax/unfairplay)). It turns this:

```sh
ipatool auth login ...
ipatool download ...
unfairplay setup ...
unfairplay decrypt ...
```

into this:

```sh
ipa-dlp get spotify --version 9.1.88
```

It prompts for your Apple ID in the terminal (including 2FA) by default;
`--gui` switches to native macOS dialogs. It installs pinned copies of both
tools under your home directory, recovers the reusable FairPlay model on first
use with one admin prompt, downloads the requested app version, decrypts it,
strips the App Store account data, and verifies the plaintext before reporting
success.

## Requirements

- Apple Silicon Mac (UnfairPlay's FairPlay recovery is arm64-only)
- macOS with a working native App Store / FairPlay enrollment
- Python 3.9+ (`/usr/bin/python3` from the Xcode Command Line Tools is enough)
- Internet access
- An Apple ID that owns or can obtain the requested apps
- Admin rights for the one-time model recovery

## Install

### Homebrew (recommended)

```sh
brew tap ipakit/ipa-dlp
brew install ipa-dlp
```

If Homebrew complains that the tap is untrusted (some Homebrew 7 builds gate
third-party taps), run `brew trust ipakit/ipa-dlp` and retry.

Then:

```sh
ipa-dlp setup
```

### From source

```sh
git clone https://github.com/ipakit/ipa-dlp.git
cd ipa-dlp
./install.sh
```

This installs the `ipa-dlp` entrypoint and its `ipa_dlp.py` module into
`~/.local/bin/`, registers that directory in the Python user site-packages
(`ipa_dlp.pth`) so `import ipa_dlp` works from any script, then runs
`ipa-dlp setup`. No `sudo` is needed; the first decryption asks for admin
once.

If `~/.local/bin` is not on your `PATH`:

```sh
./install.sh --add-path
```

or call the full path.

### pipx / pip

`pyproject.toml` describes a PEP 621 package with a `flit_core` backend and
no runtime dependencies, so the CLI can also be installed like any other
Python tool:

```sh
pipx install ipa-dlp
# or
python3 -m pip install ipa-dlp
```

## Use

### CLI

```sh
ipa-dlp setup
ipa-dlp get spotify
ipa-dlp get spotify --version 9.1.88
ipa-dlp get com.spotify.client --version 9.1.88
ipa-dlp get 324684580 --version 9.1.88
ipa-dlp get spotify --version 9.1.88 -o ~/Desktop/Spotify.ipa
```

The final path is printed on stdout; progress and diagnostics go to stderr.
The default output is:

```text
~/Downloads/Spotify-9.1.88.ipa
```

Account data is stripped by default: `SC_Info` licenses are removed and the
Apple ID/purchaser fields are scrubbed from `iTunesMetadata.plist`.
ipa-dlp checks the decryption report and the output archive
before printing `account data stripped: yes`; if either check fails,
it deletes the output and exits with an error.

Use `--keep-account-data` if you need the licenses for further analysis, and
`--full` to keep the extra artifacts:

```sh
ipa-dlp get spotify --version 9.1.88 --keep-account-data
ipa-dlp get spotify --version 9.1.88 --full
```

`--full` also keeps:

```text
Spotify-9.1.88.encrypted.ipa
Spotify-9.1.88.report.json
```

For `ipa-dlp decrypt`, `--full` keeps the report; the encrypted input
already exists where you put it.

### Extracting the executable

`--extract-binary` also writes the app's arm64 Mach-O executable next to the
decrypted IPA, ready for a disassembler or symbol dumper:

```sh
ipa-dlp get spotify --version 9.1.88 --extract-binary
ipa-dlp decrypt /path/to/App.ipa --extract-binary
```

```text
~/Downloads/Spotify-9.1.88.ipa
~/Downloads/Spotify-9.1.88.bin
```

The slice is read from the decrypted archive, so it is already FairPlay-free.
Universal ("fat") executables are reduced to the arm64 slice (falling back to
x86_64), written with mode `0755`, and the `.bin` path is what gets printed on
stdout. The IPA is still written; pass `--force` to replace an existing `.bin`.

### Extracting from an existing IPA, .app, or binary

`ipa-dlp extract` works on files you already have and does not need an App
Store account:

```sh
ipa-dlp extract ~/Downloads/Spotify-9.1.88.ipa        # -> Spotify-9.1.88.bin
ipa-dlp extract ~/Applications/MyApp.app -o MyApp.bin
ipa-dlp extract ./SomeBinary -o SomeBinary.arm64
```

For an IPA it reads `CFBundleExecutable`, checks the Mach-O encryption command,
and extracts directly when the payload is already decrypted. If `cryptid` is
non-zero, the command requires `SC_Info`, decrypts through UnfairPlay into a
temporary directory (the usual model/admin rules apply), and extracts from
that copy. A `.app` or raw Mach-O that is still encrypted cannot be decrypted
in place; it is extracted with a warning. `-o` templates and `--exec` work as
described below.

`extract` accepts `--json` like the other commands.

### Output templates and hooks

`-o` on `get`, `decrypt`, and `extract`, plus `--exec` on all three, accept
tokens expanded from the app's `Info.plist`:

| Token | Value |
| --- | --- |
| `%(name)s` | app display name |
| `%(bundle_id)s` | bundle identifier |
| `%(version)s` | `CFBundleShortVersionString` |
| `%(build)s` | `CFBundleVersion` |
| `%(external_id)s` | Apple external version ID, when known |

```sh
ipa-dlp get spotify --version 9.1.88 \
  -o '~/Research/spotify/%(version)s-%(build)s.ipa'

ipa-dlp get spotify --version 9.1.88 --extract-binary \
  --exec 'cp "{}" ~/Research/spotify/current.bin'
```

`--exec` runs after the artifacts are written. `{}` is replaced with the final
artifact path — the `.bin` when `--extract-binary` is set, otherwise the
`.ipa` — and the command runs through the shell. Expansion uses Python
`%`-formatting, so write `%%` for a literal `%`. A failing hook exits non-zero
after the artifacts are kept.

For `extract`, tokens are read from an IPA or `.app`; a raw Mach-O only has
`%(name)s` (its filename stem). `%(external_id)s` is empty for `decrypt` and
`extract` because the input's version ID is not resolved.

### JSON output

`--json` prints exactly one JSON object on stdout and keeps all progress and
diagnostics on stderr, so a pipeline can parse it without stripping noise:

```sh
ipa-dlp --json get spotify --version 9.1.88 > result.json
ipa-dlp --json get spotify --list-versions --limit 5
ipa-dlp --json status
```

Results carry `ok`, `command`, the resolved app, `external_id`, the
`artifact` path, `ipa`/`binary` paths where relevant, `verified`, and
`account_data_stripped`. `--list-versions` returns a `versions` array. A
failure still prints one object, `{"ok": false, "error": "..."}`, and
exits non-zero. With `--exec`, the hook's stdout is redirected to stderr so
the object stays alone on stdout.

### Library API

`ipa_dlp` is importable and has no third-party dependencies:

```python
from ipa_dlp import download, decrypt, extract_binary, load_macho

ipa = download("spotify", version="9.1.88")    # -> Path (decrypted IPA)
ipa = decrypt("/path/to/App.ipa", output="App.ipa")
bin_path = extract_binary(ipa)                   # -> Path (<base>.bin)
data = load_macho("spotify", version="9.1.88")  # -> bytes (arm64 slice)
```

Each function returns its result instead of printing it. Pass `exec_hook=` or
`reveal=True` only when the side effects are wanted.

Other commands:

```sh
ipa-dlp status
ipa-dlp decrypt /path/to/App.ipa
ipa-dlp extract /path/to/App.ipa
ipa-dlp setup --update          # reinstall pinned ipatool/UnfairPlay
ipa-dlp setup --reauth          # force a new Apple ID login
ipa-dlp setup --seed spotify    # create the model now instead of on first get
ipa-dlp setup --seed spotify --version 9.1.88
```

### Version selection

`--version` matches the IPA's `CFBundleShortVersionString`. ipa-dlp
maps it to Apple's external version identifier by asking the App Store for
metadata, newest versions first, and caches the mapping in
`~/Library/Caches/ipa-dlp/versions/`. ipa-dlp binary-searches the
chronologically ordered version IDs, so even very old versions resolve in a
handful of metadata requests. If the App Store will not serve a version to
your account, you get an error; `--external-version-id` lets you bypass the
lookup once you know the ID.

The version can be given as `--version 9.1.88` (or `-v`), as a second
positional (`ipa-dlp get spotify 9.1.88`), or appended to the query
(`ipa-dlp get spotify@9.1.88`). The `@` form also works with `load_macho`.

### Version history

`--list-versions` (`-F`) prints the App Store's version history for an app,
newest first, without downloading anything:

```sh
ipa-dlp get spotify --list-versions        # newest 15
ipa-dlp get spotify -F --limit 100         # newest 100
```

The table goes to stdout:

```text
EXTERNAL_ID  VERSION  RELEASE_DATE
871294812    9.1.88   2026-03-14
870912300    9.1.86   2026-03-07
```

Metadata for versions that were not resolved before is fetched in parallel and
stored in the same version cache used by `--version`, so later runs are
instant. Pipe an `EXTERNAL_ID` into `--external-version-id` to download a
version that is no longer listed.

### Licenses and paid apps

Free apps are licensed automatically when needed. For a paid app, buy it in the
App Store first; downloading an owned paid app works without extra flags.
`--purchase` asks Apple to obtain a missing license, which can charge your
account, so ipa-dlp asks for confirmation before using it on a paid app.

```sh
ipa-dlp get some-paid-app --purchase
```

## On first run

1. Pinned `ipatool 2.7.0` and `UnfairPlay 0.2.0` releases are downloaded and
   SHA-256 verified into `~/.local/share/ipa-dlp/bin/`.
2. You are prompted for your Apple ID and password in the terminal. The
   password is fed to `ipatool` over a PTY, not as a command-line argument, and
   is not stored. If Apple asks for a 2FA code, you are prompted for it. Pass
   `--gui` to use native macOS dialogs instead.
3. `setup` checks the App Store session and reports the signed-in account.
4. On the first `get`, the IPA is downloaded, its app bundle is unpacked, and
   `UnfairPlay` recovers the enrollment model. You are asked for your login
   password once (in the terminal by default, or in a macOS dialog with
   `--gui`). The model is saved under
   `~/Library/Application Support/unfairplay/models` and reused for later IPAs
   from the same enrollment/account.
5. The IPA is decrypted, account data is stripped, and the output is checked
   against its pre-encryption page hashes before success is reported.

Later `get` calls do not need the admin password.

## Important limitations

- **Spotify's iOS app is not in the Mac App Store.** ipa-dlp does not need
  a Mac App Store-installed app: it uses the app bundle from the downloaded IPA
  as the enrollment sample. This is what made the Spotify 9.1.88 flow work.
- **UnfairPlay is build-sensitive.** It may fail closed after a macOS update if
  Apple changes the FairPlay state or `fairplayd`. ipa-dlp reports the error
  instead of faking success.
- **The model is tied to the Mac's enrollment and Apple ID.** If you sign in to
  a different account, use `ipa-dlp setup --reauth`; a different enrollment
  needs its own model.
- **Old versions are not guaranteed.** Apple can refuse to serve a version that
  was never associated with your account, or remove it from the version list.
- **The output is decrypted but unsigned.** It will not install by
  double-clicking. Use TrollStore, a signing tool, or PlayCover depending on
  your target. UnfairPlay does not install apps.
- **Account data is stripped by default.** `SC_Info` licenses are removed and
  the Apple ID/purchaser fields are removed from `iTunesMetadata.plist`. Use
  `--keep-account-data` only when you need those licenses; such an IPA can
  identify your Apple ID.
- **Only store-owned apps are supported.** It cannot bypass Apple's
  licensing; if the account cannot obtain the app/version, the download fails.
- **No App Store password is stored.** `ipatool` stores its own session token in
  the macOS Keychain. Revoke it with `ipatool auth revoke` if needed.

## Privacy and legal use

Use this only with apps and accounts you lawfully control, and follow Apple's
terms. Decrypting an app does not grant redistribution rights. Do not share
personalized or decrypted IPAs. The default output is stripped, but
`--keep-account-data` output still contains your account licenses.

The model file is private. Keep it on encrypted storage and never share it.

## Development / testing

Environment overrides:

```sh
IPADLP_IPATOOL=/path/to/ipatool
IPADLP_UNFAIRPLAY=/path/to/unfairplay
IPADLP_BIN_DIR=/tmp/ipa-dlp-bin
IPADLP_CACHE_DIR=/tmp/ipa-dlp-cache
IPADLP_DATA_DIR=/tmp/ipa-dlp-data
UNFAIRPLAY_MODEL_DIR=/tmp/unfairplay-models
```

Terminal prompts are the default, which works over SSH and in scripts. Pass
`--gui` to use native macOS dialogs instead.

Useful checks:

```sh
python3 -m py_compile ipa_dlp.py ipa-dlp
ipa-dlp status
ipa-dlp --json status
ipa-dlp get spotify --version 9.1.88 --dry-run
python3 -m build          # wheel + sdist (needs flit_core)
```
