# ipa-dlp

A small macOS wrapper around [ipatool](https://github.com/majd/ipatool) and
[UnfairPlay](https://github.com/DrWhax/unfairplay) that turns this:

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

It prompts for your Apple ID through native dialogs (including 2FA), installs
pinned copies of both tools under your home directory, recovers the reusable
FairPlay model on first use with one admin prompt, downloads the requested app
version, decrypts it, strips the App Store account data, and verifies the
plaintext before reporting success.

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

This installs `~/.local/bin/ipa-dlp` and then runs `ipa-dlp setup`. No `sudo`
is needed to install the wrapper; the first decryption asks for admin once.

If `~/.local/bin` is not on your `PATH`:

```sh
./install.sh --add-path
```

or call the full path.

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
The wrapper checks the decryption report and the output archive
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

### Output templates and hooks

`-o` and `--exec` accept tokens expanded from the downloaded app's
`Info.plist`:

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

`ipa-dlp decrypt` supports `-o` templates and `--exec` too; `%(external_id)s`
is empty there because the input's version ID is not resolved.

Other commands:

```sh
ipa-dlp status
ipa-dlp decrypt /path/to/App.ipa
ipa-dlp setup --update          # reinstall pinned ipatool/UnfairPlay
ipa-dlp setup --reauth          # force a new Apple ID login
ipa-dlp setup --seed spotify    # create the model now instead of on first get
ipa-dlp setup --seed spotify --version 9.1.88
```

### Version selection

`--version` matches the IPA's `CFBundleShortVersionString`. The wrapper
maps it to Apple's external version identifier by asking the App Store for
metadata, newest versions first, and caches the mapping in
`~/Library/Caches/ipa-dlp/versions/`. Very old versions can take longer to
resolve. If the App Store will not serve a version to your account, you get an
error; `--external-version-id` lets you bypass the lookup once you know the ID.

### Version history

`--list-versions` (`-F`) prints the App Store's version history for an app,
newest first, without downloading anything:

```sh
ipa-dlp get spotify --list-versions        # newest 30
ipa-dlp get spotify -F --limit 100         # newest 100
```

The table goes to stdout:

```text
EXT_ID     VERSION  RELEASED
871294812  9.1.88   2026-03-14
870912300  9.1.86   2026-03-07
```

Metadata for versions that were not resolved before is fetched in parallel and
stored in the same version cache used by `--version`, so later runs are
instant. Pipe an `EXT_ID` into `--external-version-id` to download a version
that is no longer listed.

### Licenses and paid apps

Free apps are licensed automatically when needed. For a paid app, buy it in the
App Store first; downloading an owned paid app works without extra flags.
`--purchase` asks Apple to obtain a missing license, which can charge your
account, so the wrapper asks for confirmation before using it on a paid app.

```sh
ipa-dlp get some-paid-app --purchase
```

## On first run

1. Pinned `ipatool 2.7.0` and `UnfairPlay 0.2.0` releases are downloaded and
   SHA-256 verified into `~/.local/share/ipa-dlp/bin/`.
2. An Apple ID/password dialog appears. The password is fed to `ipatool` over a
   PTY, not as a command-line argument, and is not stored. If Apple asks for a
   2FA code, a second dialog appears.
3. `setup` checks the App Store session and reports the signed-in account.
4. On the first `get`, the IPA is downloaded, its app bundle is unpacked, and
   `UnfairPlay` recovers the enrollment model. macOS shows one admin password
   dialog for this step. The model is saved under
   `~/Library/Application Support/unfairplay/models` and reused for later IPAs
   from the same enrollment/account.
5. The IPA is decrypted, account data is stripped, and the output is checked
   against its pre-encryption page hashes before success is reported.

Later `get` calls do not need the admin password.

## Important limitations

- **Spotify's iOS app is not in the Mac App Store.** The wrapper does not need
  a Mac App Store-installed app: it uses the app bundle from the downloaded IPA
  as the enrollment sample. This is what made the Spotify 9.1.88 flow work.
- **UnfairPlay is build-sensitive.** It may fail closed after a macOS update if
  Apple changes the FairPlay state or `fairplayd`. The wrapper reports the error
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
- **The wrapper only handles store-owned apps.** It cannot bypass Apple's
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
python3 -m py_compile ipa-dlp
ipa-dlp status
ipa-dlp get spotify --version 9.1.88 --dry-run
```
