#!/usr/bin/env python3
"""ipa-dlp - fetch, decrypt, and extract App Store IPAs and Mach-O binaries.

Python API:
    from ipa_dlp import download, decrypt, extract_binary, load_macho

    ipa = download("spotify", version="9.1.88")          # -> Path (.ipa)
    bin_path = extract_binary(ipa)                       # -> Path (arm64 .bin)
    blob = load_macho("spotify", version="9.1.88")       # -> bytes (arm64 Mach-O)

CLI:
    ipa-dlp get spotify --version 9.1.88 --extract-binary
    ipa-dlp extract ./Spotify.ipa -o Spotify.bin

Only use this with apps and accounts you lawfully control.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import hashlib
import json
import os
import platform
import plistlib
import pty
import re
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import termios
import threading
import time
import tty
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

__all__ = [
    "download",
    "decrypt",
    "extract_binary",
    "load_macho",
    "list_versions_table",
    "CliError",
]

APP = "ipa-dlp"
VERSION = "0.3.0"
IPATOOL_VERSION = "2.7.0"
IPATOOL_SHA256 = "5ae100d4520cf4b3f4374239e5925a9ad9a8dc02abc16bae27479e7ae99aca33"
UNFAIRPLAY_VERSION = "0.2.0"
UNFAIRPLAY_SHA256 = "e37d230c771fff884c4055a006645d0756c8d9d0c52bfd7f1d02df8fd71cb052"

HOME = Path.home()
DATA_DIR = Path(
    os.environ.get("IPADLP_DATA_DIR")
    or (HOME / "Library" / "Application Support" / "ipa-dlp")
)
BIN_DIR = Path(
    os.environ.get("IPADLP_BIN_DIR")
    or (HOME / ".local" / "share" / "ipa-dlp" / "bin")
)
CACHE_DIR = Path(
    os.environ.get("IPADLP_CACHE_DIR")
    or (HOME / "Library" / "Caches" / "ipa-dlp")
)
MODEL_DIR = Path(
    os.environ.get("UNFAIRPLAY_MODEL_DIR")
    or (HOME / "Library" / "Application Support" / "unfairplay" / "models")
)
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_PLATFORM = "iphone"
USE_GUI = False
VERBOSE = False
JSON_OUTPUT = False

# Kept in sync with UnfairPlay's --strip-account-data scrubber.
ACCOUNT_METADATA_KEYS = frozenset(
    {
        "apple-id",
        "appleId",
        "AppleID",
        "userName",
        "UserName",
        "purchaserID",
        "DSPersonID",
        "dsPersonId",
        "AltDSID",
        "altDsid",
        "FamilyID",
        "familyId",
    }
)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _use_color() -> bool:
    """Return True when stderr is a TTY and NO_COLOR is not set."""
    return sys.stderr.isatty() and not os.environ.get("NO_COLOR")


# Tag colors: cyan for ipa-dlp, green for ipatool, magenta for unfairplay,
# blue for download, yellow for versions.
_TAG_COLORS: Dict[str, str] = {
    "ipa-dlp": "\x1b[36m",     # cyan
    "ipatool": "\x1b[32m",     # green
    "unfairplay": "\x1b[35m",  # magenta
    "download": "\x1b[34m",    # blue
    "versions": "\x1b[33m",    # yellow
}
_RESET = "\x1b[0m"


class CliError(Exception):
    pass


class Cancelled(CliError):
    pass


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def eprint(*args: Any, **kwargs: Any) -> None:
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


def log(message: str, tag: str = "ipa-dlp") -> None:
    if _use_color():
        color = _TAG_COLORS.get(tag, "")
        reset = _RESET if color else ""
        eprint("%s[%s]%s %s" % (color, tag, reset, message))
    else:
        eprint("[%s] %s" % (tag, message))


def _progress_line(message: str, tag: str = "download") -> None:
    """Overwrite the current line with a progress message (TTY only)."""
    if JSON_OUTPUT and not VERBOSE:
        return
    if sys.stderr.isatty():
        if _use_color():
            color = _TAG_COLORS.get(tag, "")
            reset = _RESET if color else ""
            sys.stderr.write("\r\x1b[K%s[%s]%s %s" % (color, tag, reset, message))
        else:
            sys.stderr.write("\r\x1b[K[%s] %s" % (tag, message))
        sys.stderr.flush()
    else:
        eprint("[%s] %s" % (tag, message))


def _finish_progress() -> None:
    """End a progress line by writing a newline (TTY only)."""
    if sys.stderr.isatty():
        sys.stderr.write("\n")
        sys.stderr.flush()


def vlog(message: str) -> None:
    if VERBOSE:
        eprint(message)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def tail(text: str, limit: int = 4000) -> str:
    text = strip_ansi(text).strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def parse_json(text: str) -> Optional[Dict[str, Any]]:
    """ipatool logs JSON objects, sometimes surrounded by other log lines."""
    if not text:
        return None
    clean = strip_ansi(text)
    for line in reversed(clean.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    try:
        value = json.loads(clean)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def run(
    argv: Sequence[Any],
    timeout: int = 300,
    check: bool = True,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    cmd = [str(item) for item in argv]
    vlog("+ " + " ".join(cmd))
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise CliError("missing command: %s" % cmd[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError("command timed out after %ss: %s" % (timeout, cmd[0])) from exc
    if check and completed.returncode != 0:
        detail = tail(completed.stderr or completed.stdout)
        raise CliError(
            "%s failed (exit %s)%s"
            % (cmd[0], completed.returncode, (": " + detail) if detail else "")
        )
    return completed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def write_private(path: Path, data: str) -> None:
    ensure_private_dir(path.parent)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(data)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def read_config() -> Dict[str, Any]:
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_config(config: Dict[str, Any]) -> None:
    write_private(CONFIG_PATH, json.dumps(config, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# native dialogs / terminal fallback
# ---------------------------------------------------------------------------


def osa(script: str, *args: Any, timeout: int = 900) -> str:
    cmd = ["osascript", "-e", script]
    cmd.extend(str(arg) for arg in args)
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise CliError("dialog timed out") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        if "-128" in detail or "User canceled" in detail or "cancelled" in detail.lower():
            raise Cancelled()
        raise CliError("dialog failed: %s" % detail)
    return completed.stdout.rstrip("\n")


def gui_enabled() -> bool:
    return (
        USE_GUI
        and sys.platform == "darwin"
        and shutil.which("osascript") is not None
        and not os.environ.get("SSH_CONNECTION")
    )


ASK_TEXT = r"""
on run argv
    set theTitle to item 1 of argv
    set theMessage to item 2 of argv
    set theDefault to item 3 of argv
    set theHidden to item 4 of argv
    if theHidden is "1" then
        display dialog theMessage default answer theDefault with title theTitle buttons {"Cancel", "Continue"} default button "Continue" with hidden answer
    else
        display dialog theMessage default answer theDefault with title theTitle buttons {"Cancel", "Continue"} default button "Continue"
    end if
    return text returned of result
end run
"""

CHOOSE_ITEM = r"""
on run argv
    set theTitle to item 1 of argv
    set thePrompt to item 2 of argv
    set theItems to paragraphs of item 3 of argv
    set chosen to choose from list theItems with title theTitle with prompt thePrompt OK button name "Select" cancel button name "Cancel"
    if chosen is false then error number -128
    return item 1 of chosen
end run
"""

ASK_YES_NO = r"""
on run argv
    set theTitle to item 1 of argv
    set theMessage to item 2 of argv
    display dialog theMessage with title theTitle buttons {"No", "Yes"} default button "Yes"
    return button returned of result
end run
"""


def ask_text(
    title: str,
    message: str,
    default: str = "",
    secret: bool = False,
    allow_empty: bool = False,
) -> str:
    while True:
        if gui_enabled():
            try:
                value = osa(
                    ASK_TEXT,
                    title,
                    message,
                    default,
                    "1" if secret else "0",
                )
            except CliError as exc:
                if isinstance(exc, Cancelled):
                    raise
                log("dialog unavailable (%s); falling back to Terminal" % exc)
                value = _terminal_prompt(message, secret, default)
        else:
            value = _terminal_prompt(message, secret, default)
        if not secret:
            value = value.strip()
        if value or allow_empty:
            return value
        log("A value is required.")


def _terminal_prompt(message: str, secret: bool, default: str) -> str:
    if secret:
        return getpass.getpass("%s: " % message)
    prompt = "%s" % message
    if default:
        prompt += " [%s]" % default
    prompt += ": "
    value = input(prompt)
    return value if value else default


def ask_yes_no(title: str, message: str) -> bool:
    if gui_enabled():
        try:
            return osa(ASK_YES_NO, title, message).strip() == "Yes"
        except Cancelled:
            return False
        except CliError:
            pass
    answer = input("%s [y/N]: " % message)
    return answer.strip().lower() in ("y", "yes")


def choose_item(title: str, prompt: str, items: List[str]) -> str:
    if not items:
        raise CliError("nothing to choose from")
    if len(items) == 1:
        return items[0]
    if gui_enabled():
        try:
            selected = osa(CHOOSE_ITEM, title, prompt, "\n".join(items))
            if selected in items:
                return selected
        except Cancelled:
            raise
        except CliError:
            pass

    # Arrow-key selector when stdin is a TTY.
    if sys.stdin.isatty():
        return _arrow_select(prompt, items)

    # Dumb fallback for pipes / non-TTY.
    eprint(prompt)
    for index, item in enumerate(items, 1):
        eprint("  %d. %s" % (index, item))
    while True:
        try:
            answer = input("Select [1-%d]: " % len(items)).strip()
        except EOFError as exc:
            raise CliError("selection canceled") from exc
        if answer.isdigit() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]


def _arrow_select(prompt: str, items: List[str]) -> str:
    """Interactive ↑/↓/Enter selector drawn on stderr."""
    selected = 0
    # How many items to show at once (scrollable window).
    max_visible = min(len(items), 12)
    use_color = _use_color()

    def _render(sel: int) -> None:
        # Determine the visible window around the selection.
        if len(items) <= max_visible:
            start = 0
        else:
            start = max(0, min(sel - max_visible // 2, len(items) - max_visible))
        end = start + max_visible
        lines: list[str] = []
        for i in range(start, end):
            marker = "▸ " if i == sel else "  "
            if i == sel and use_color:
                lines.append("  \x1b[7m%s%s\x1b[0m" % (marker, items[i]))
            else:
                lines.append("  %s%s" % (marker, items[i]))
        if len(items) > max_visible:
            lines.append("  (%d items, ↑/↓ to scroll)" % len(items))
        sys.stderr.write("\r\x1b[K" + prompt + "\n")
        for line in lines:
            sys.stderr.write("\r\x1b[K" + line + "\n")
        # Move cursor back up to keep the selector in place.
        total = 1 + len(lines)
        sys.stderr.write("\x1b[%dA" % total)
        sys.stderr.flush()

    def _clear(line_count: int) -> None:
        for _ in range(line_count):
            sys.stderr.write("\r\x1b[K\n")
        sys.stderr.write("\x1b[%dA" % line_count)
        sys.stderr.flush()

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        _render(selected)
        while True:
            ch = os.read(fd, 1)
            if ch == b"\r" or ch == b"\n":
                break
            if ch == b"\x03":  # Ctrl-C
                raise KeyboardInterrupt
            if ch == b"\x1b":
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    # A bare Esc is a cancel, not the start of an arrow key.
                    raise Cancelled()
                seq = os.read(fd, 2)
                if seq == b"[A":  # Up
                    selected = max(0, selected - 1)
                elif seq == b"[B":  # Down
                    selected = min(len(items) - 1, selected + 1)
                _render(selected)
            elif ch == b"k":
                selected = max(0, selected - 1)
                _render(selected)
            elif ch == b"j":
                selected = min(len(items) - 1, selected + 1)
                _render(selected)
            elif ch == b"q":
                raise Cancelled()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        # Clear the selector UI.
        extra = 1 if len(items) > max_visible else 0
        _clear(1 + max_visible + extra)
    return items[selected]


# ---------------------------------------------------------------------------
# tool bootstrap
# ---------------------------------------------------------------------------


def ipatool_path() -> Path:
    override = os.environ.get("IPADLP_IPATOOL")
    return Path(override).expanduser() if override else BIN_DIR / "ipatool"


def unfairplay_path() -> Path:
    override = os.environ.get("IPADLP_UNFAIRPLAY")
    return Path(override).expanduser() if override else BIN_DIR / "unfairplay"


def tool_version(path: Path) -> str:
    if not path.exists():
        return ""
    completed = run([path, "--version"], timeout=30, check=False)
    return strip_ansi((completed.stdout or "") + (completed.stderr or "")).strip()


def download_file(url: str, destination: Path, expected: Optional[str] = None) -> None:
    ensure_private_dir(destination.parent)
    if destination.exists() and expected:
        if sha256_file(destination) == expected.lower():
            return
        destination.unlink()
    part = destination.with_name(destination.name + ".part")
    if part.exists():
        part.unlink()
    log("downloading %s" % destination.name, tag="download")
    completed = subprocess.run(
        [
            "curl",
            "-fL",
            "--retry",
            "3",
            "--connect-timeout",
            "20",
            "--silent",
            "--show-error",
            "-o",
            str(part),
            url,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        raise CliError(
            "download failed: %s" % (completed.stderr.strip() or url)
        )
    if expected:
        actual = sha256_file(part)
        if actual != expected.lower():
            part.unlink(missing_ok=True)
            raise CliError(
                "checksum mismatch for %s (expected %s, got %s)"
                % (destination.name, expected, actual)
            )
    os.replace(str(part), str(destination))
    try:
        destination.chmod(0o600)
    except OSError:
        pass


def fetch_text(url: str) -> str:
    completed = subprocess.run(
        ["curl", "-fsSL", "--retry", "3", "--connect-timeout", "20", url],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise CliError("could not fetch %s" % url)
    return completed.stdout


def extract_tar(archive: Path) -> Path:
    destination = Path(tempfile.mkdtemp(prefix="ipa-dlp-extract-"))
    with tarfile.open(archive, "r:*") as handle:
        for member in handle.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise CliError("unsafe archive member: %s" % name)
            if member.issym() or member.islnk() or member.isdev():
                raise CliError("unsupported archive member: %s" % name)
            if member.isdir() or member.isfile():
                handle.extract(member, destination)
    return destination


def find_binary(root: Path, name: str) -> Path:
    for candidate in root.rglob(name):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    prefixed = [
        candidate
        for candidate in root.rglob(name + "-*")
        if candidate.is_file() and not candidate.is_symlink()
    ]
    if len(prefixed) == 1:
        return prefixed[0]
    raise CliError("release archive did not contain %s" % name)


def install_binary(source: Path, destination: Path) -> None:
    ensure_private_dir(destination.parent)
    shutil.copy2(str(source), str(destination))
    destination.chmod(0o755)


def install_ipatool() -> None:
    name = "ipatool-%s-macos-arm64.tar.gz" % IPATOOL_VERSION
    base = "https://github.com/majd/ipatool/releases/download/v%s" % IPATOOL_VERSION
    archive = CACHE_DIR / "downloads" / name
    download_file(base + "/" + name, archive, IPATOOL_SHA256)
    root = extract_tar(archive)
    try:
        install_binary(find_binary(root, "ipatool"), ipatool_path())
    finally:
        shutil.rmtree(root, ignore_errors=True)
    log("installed ipatool %s at %s" % (IPATOOL_VERSION, ipatool_path()), tag="ipatool")


def install_unfairplay() -> None:
    name = "unfairplay-v%s-darwin-arm64.tar.gz" % UNFAIRPLAY_VERSION
    base = "https://github.com/DrWhax/unfairplay/releases/download/v%s" % UNFAIRPLAY_VERSION
    archive = CACHE_DIR / "downloads" / name
    download_file(base + "/" + name, archive, UNFAIRPLAY_SHA256)
    root = extract_tar(archive)
    try:
        install_binary(find_binary(root, "unfairplay"), unfairplay_path())
        license_dir = DATA_DIR / "licenses"
        ensure_private_dir(license_dir)
        for item in root.rglob("*"):
            if item.is_file() and (
                item.name.startswith("LICENSE") or item.name == "NOTICE"
            ):
                shutil.copy2(str(item), str(license_dir / item.name))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    log("installed UnfairPlay %s at %s" % (UNFAIRPLAY_VERSION, unfairplay_path()), tag="unfairplay")


def _tool_ready(path: Path) -> bool:
    """Return True when the pinned tool exists and is executable.

    Deliberately avoids spawning ``--version`` on every run: the install path
    is private to ipa-dlp and `setup --update` is the way to refresh it.
    """
    return path.is_file() and os.access(str(path), os.X_OK)


def ensure_tools(update: bool = False) -> None:
    if platform.system() != "Darwin":
        raise CliError("this tool only supports macOS")
    if platform.machine() != "arm64":
        raise CliError(
            "UnfairPlay requires an Apple Silicon Mac (found %s)" % platform.machine()
        )

    ipatool = ipatool_path()
    unfairplay = unfairplay_path()

    if update or not _tool_ready(ipatool):
        if os.environ.get("IPADLP_IPATOOL"):
            raise CliError(
                "IPADLP_IPATOOL points to %s, but it is missing or not executable"
                % ipatool
            )
        install_ipatool()

    if update or not _tool_ready(unfairplay):
        if os.environ.get("IPADLP_UNFAIRPLAY"):
            raise CliError(
                "IPADLP_UNFAIRPLAY points to %s, but it is missing or not executable"
                % unfairplay
            )
        install_unfairplay()


# ---------------------------------------------------------------------------
# Apple ID authentication
# ---------------------------------------------------------------------------


def auth_info() -> Optional[Dict[str, Any]]:
    ipatool = ipatool_path()
    if not ipatool.exists():
        return None
    completed = run(
        [ipatool, "--format", "json", "auth", "info"], timeout=90, check=False
    )
    if completed.returncode != 0:
        return None
    value = parse_json((completed.stdout or "") + "\n" + (completed.stderr or ""))
    if value and value.get("success"):
        return value
    return None


def login_pty(
    email: str,
    password: str,
    code_prompt: Callable[[], str],
) -> Tuple[int, str]:
    """Run interactive `ipatool auth login` behind a PTY.

    This avoids putting the password in argv and lets ipatool's own 2FA prompt
    work. The password is written once; 2FA codes are requested through the
    supplied callback whenever ipatool asks again.
    """
    argv = [str(ipatool_path()), "auth", "login", "--email", email]
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        os.close(slave)

    output = ""
    sent_password = False
    sent_codes = 0
    code_values: List[str] = []
    started = time.time()
    try:
        while True:
            if process.poll() is not None:
                # drain whatever is left
                while True:
                    ready, _, _ = select.select([master], [], [], 0.1)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    output += chunk.decode("utf-8", "replace")
                break

            if time.time() - started > 600:
                process.kill()
                raise CliError("login timed out")

            ready, _, _ = select.select([master], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                continue
            output += chunk.decode("utf-8", "replace")
            clean = strip_ansi(output)

            if not sent_password and "enter password:" in clean:
                os.write(master, (password + "\n").encode())
                sent_password = True

            code_prompts = clean.count("enter 2FA code:")
            while sent_codes < code_prompts:
                code = code_prompt()
                code_values.append(code)
                os.write(master, (code + "\n").encode())
                sent_codes += 1
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()
    safe = strip_ansi(output).replace(password, "[redacted]")
    for code in code_values:
        safe = safe.replace(code, "[redacted]")
    return process.returncode, safe


def ensure_auth(force: bool = False) -> Dict[str, Any]:
    if not force:
        info = auth_info()
        if info:
            return info

    config = read_config()
    email = ask_text(
        "ipa-dlp - Apple ID",
        "Apple ID email address:",
        default=str(config.get("email", "")),
    )
    password = ask_text(
        "ipa-dlp - Apple ID",
        "Apple ID password (used only for this login, never stored):",
        secret=True,
    )

    def ask_code() -> str:
        code = ask_text(
            "ipa-dlp - Two-Factor Authentication",
            "Enter the 6-digit code from your trusted device:",
        ).strip()
        if not re.fullmatch(r"\d{6}", code):
            raise CliError("2FA code must be six digits")
        return code

    last_output = ""
    for attempt in range(1, 4):
        log("signing in to the App Store...", tag="ipatool")
        code, output = login_pty(email, password, ask_code)
        last_output = output
        info = auth_info()
        if info:
            config["email"] = str(info.get("email") or email)
            save_config(config)
            log(
                "signed in as %s"
                % (info.get("email") or info.get("name") or email)
            )
            return info
        if attempt < 3:
            log("login failed; %s" % tail(output, 1200), tag="ipatool")
            if not ask_yes_no(
                "ipa-dlp",
                "Apple ID login failed. Try again with a new password/code?",
            ):
                break
            password = ask_text(
                "ipa-dlp - Apple ID",
                "Apple ID password:",
                secret=True,
            )
    raise CliError("Apple ID login failed: %s" % tail(last_output, 2000))


# ---------------------------------------------------------------------------
# App Store app / version resolution
# ---------------------------------------------------------------------------


def ipatool_json(args: Sequence[Any], timeout: int = 90) -> Optional[Dict[str, Any]]:
    completed = run(
        [ipatool_path(), "--format", "json"] + list(args),
        timeout=timeout,
        check=False,
    )
    return parse_json((completed.stdout or "") + "\n" + (completed.stderr or ""))


def search_apps(term: str, limit: int = 12) -> List[Dict[str, Any]]:
    value = ipatool_json(["search", term, "--limit", str(limit)])
    if not value:
        return []
    apps = value.get("apps") or []
    return [app for app in apps if isinstance(app, dict)]


def itunes_lookup(
    params: Dict[str, str], platform_name: str = DEFAULT_PLATFORM
) -> Optional[Dict[str, Any]]:
    """Public App Store lookup, used for exact bundle IDs and numeric IDs."""
    try:
        query = "&".join(
            "%s=%s" % (key, value) for key, value in params.items()
        )
        text = fetch_text("https://itunes.apple.com/lookup?" + query)
        value = json.loads(text)
    except Exception:
        return None
    results = value.get("results") if isinstance(value, dict) else None
    if not results:
        return None
    item = results[0]
    if not isinstance(item, dict) or not item.get("trackId"):
        return None
    try:
        size = int(item.get("fileSizeBytes") or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "id": str(item.get("trackId")),
        "bundleID": str(item.get("bundleId") or ""),
        "name": str(item.get("trackName") or item.get("bundleId") or "App"),
        "platforms": [platform_name],
        "price": item.get("price"),
        "size": size,
    }


def resolve_app(query: str, platform_name: str = DEFAULT_PLATFORM) -> Dict[str, Any]:
    query = query.strip()
    if not query:
        raise CliError("app name, bundle ID, or App Store ID is required")

    if query.isdigit():
        found = itunes_lookup({"id": query}, platform_name)
        if found:
            return found
        return {
            "id": query,
            "bundleID": "",
            "name": "App %s" % query,
            "platforms": [platform_name],
        }

    bundle_like = bool(
        re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", query)
    )
    if bundle_like:
        found = itunes_lookup({"bundleId": query}, platform_name)
        if found:
            return found
        # Do not fall back to fuzzy name search for an explicit bundle ID.
        raise CliError(
            "bundle ID %r was not found in the App Store" % query
        )

    apps = search_apps(query)
    if not apps:
        raise CliError("no App Store results for %r" % query)

    compatible = [
        app
        for app in apps
        if not app.get("platforms") or platform_name in app.get("platforms", [])
    ]
    if compatible:
        apps = compatible

    needle = query.lower()
    strong = [
        app
        for app in apps
        if needle in str(app.get("name", "")).lower()
        or needle in str(app.get("bundleID", "")).lower()
    ]
    if strong:
        apps = strong

    if len(apps) == 1:
        return apps[0]

    exact = [
        app
        for app in apps
        if query.lower()
        in (
            str(app.get("bundleID", "")).lower(),
            str(app.get("name", "")).lower(),
        )
    ]
    if len(exact) == 1:
        return exact[0]

    labels = [
        "%s  [%s]  id=%s"
        % (app.get("name", "?"), app.get("bundleID", "?"), app.get("id", "?"))
        for app in apps
    ]
    selected = choose_item(
        "ipa-dlp - Choose app",
        "Multiple App Store results for %r:" % query,
        labels,
    )
    return apps[labels.index(selected)]


def _split_query_version(query: str, version: str) -> Tuple[str, str]:
    """Split a trailing ``@version`` off *query* when no version was given."""
    if "@" in query and not version:
        query, version = query.rsplit("@", 1)
    return query.strip(), version.strip()


def version_cache_path(app_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", app_id)
    return CACHE_DIR / "versions" / (safe + ".json")


def load_version_cache(app_id: str) -> Dict[str, Any]:
    path = version_cache_path(app_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"app_id": app_id, "versions": {}}
    if not isinstance(value, dict) or not isinstance(value.get("versions"), dict):
        return {"app_id": app_id, "versions": {}}
    value["app_id"] = app_id
    return value


def save_version_cache(app_id: str, cache: Dict[str, Any]) -> None:
    cache["app_id"] = app_id
    cache["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_private(
        version_cache_path(app_id),
        json.dumps(cache, indent=2, sort_keys=True) + "\n",
    )


def list_external_versions(app_id: str) -> List[str]:
    value = ipatool_json(["list-versions", "-i", app_id], timeout=120)
    if not value or not value.get("success"):
        raise CliError("could not list versions for app %s" % app_id)
    raw = value.get("externalVersionIdentifiers") or []
    ids = [str(item) for item in raw if str(item).isdigit()]
    ids.sort(key=lambda item: int(item), reverse=True)
    return ids


def fetch_version_metadata(app_id: str, external_id: str) -> Optional[Dict[str, Any]]:
    value = ipatool_json(
        [
            "get-version-metadata",
            "-i",
            app_id,
            "--external-version-id",
            external_id,
        ],
        timeout=60,
    )
    if not value or not value.get("success"):
        return None
    version = value.get("displayVersion")
    if not version:
        return None
    return {
        "displayVersion": str(version),
        "releaseDate": value.get("releaseDate", ""),
        "externalVersionID": external_id,
    }


def _fetch_metadata_missing(
    app_id: str,
    ids: Sequence[str],
    versions: Dict[str, Any],
    progress: Optional[Callable[[int, int], None]] = None,
) -> None:
    """Fetch metadata for the *ids* that are missing from *versions*.

    Lookups run in a small thread pool so they overlap; *progress* is called
    with ``(done, total)`` after each one completes.
    """
    missing = [ext for ext in ids if ext not in versions]
    if not missing:
        return
    total = len(missing)
    done = 0

    def fetch(ext: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        try:
            return ext, fetch_version_metadata(app_id, ext)
        except Exception:
            return ext, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for ext, metadata in pool.map(fetch, missing):
            if metadata:
                versions[ext] = metadata
            done += 1
            if progress is not None:
                progress(done, total)


def _release_date(value: Any) -> str:
    """Render an App Store release timestamp as a plain date."""
    text = str(value or "").strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else (text or "-")


def render_versions_table(rows: Sequence[Dict[str, str]]) -> None:
    """Print an aligned EXTERNAL_ID / VERSION / RELEASE_DATE table."""
    headers = ("EXTERNAL_ID", "VERSION", "RELEASE_DATE")
    width_id = max([len(headers[0])] + [len(row["external_id"]) for row in rows])
    width_version = max([len(headers[1])] + [len(row["version"]) for row in rows])
    print(
        "%-*s  %-*s  %s"
        % (width_id, headers[0], width_version, headers[1], headers[2])
    )
    for row in rows:
        print(
            "%-*s  %-*s  %s"
            % (
                width_id,
                row["external_id"],
                width_version,
                row["version"],
                row["released"],
            )
        )


def list_versions_table(
    app_id: str,
    limit: int = 15,
    render: bool = True,
) -> List[Dict[str, str]]:
    """Fetch the newest *limit* versions of *app_id*.

    Metadata for versions that are not in the local cache is fetched in
    parallel with a single-line progress status. The table is printed to
    stdout unless *render* is false (used by --json).
    """
    if limit < 1:
        raise CliError("--limit must be at least 1")
    ids = list_external_versions(app_id)[:limit]
    if not ids:
        raise CliError("App Store returned no versions for app %s" % app_id)

    cache = load_version_cache(app_id)
    versions: Dict[str, Any] = cache["versions"]
    pending = [ext for ext in ids if ext not in versions]
    if pending:
        def progress(done: int, total: int) -> None:
            _progress_line(
                "Fetching metadata: %d/%d" % (done, total),
                tag="versions",
            )

        try:
            _fetch_metadata_missing(app_id, ids, versions, progress)
        finally:
            _finish_progress()
        save_version_cache(app_id, cache)

    rows: List[Dict[str, str]] = []
    for ext in ids:
        metadata = versions.get(ext)
        if not isinstance(metadata, dict):
            metadata = {}
        rows.append(
            {
                "external_id": ext,
                "version": str(metadata.get("displayVersion") or "-"),
                "released": _release_date(metadata.get("releaseDate")),
            }
        )
    if render:
        render_versions_table(rows)
    return rows


def _version_compare(left: str, right: str) -> int:
    """Compare display versions by their numeric components.

    Display versions are chronological strings such as ``9.1.88``. Comparing
    the integer runs keeps ``9.1.88`` newer than ``9.1.9``; strings without
    numbers fall back to a case-insensitive text comparison.
    """
    left_numbers = tuple(int(part) for part in re.findall(r"\d+", left))
    right_numbers = tuple(int(part) for part in re.findall(r"\d+", right))
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return (left_numbers > right_numbers) - (left_numbers < right_numbers)
    left_text = left.strip().lower()
    right_text = right.strip().lower()
    return (left_text > right_text) - (left_text < right_text)


def _cached_version_metadata(
    app_id: str,
    ext: str,
    versions: Dict[str, Any],
    cache: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Return (metadata, fetched) for one external version ID."""
    metadata = versions.get(ext)
    if isinstance(metadata, dict) and metadata.get("displayVersion"):
        return metadata, False
    try:
        metadata = fetch_version_metadata(app_id, ext)
    except Exception:
        metadata = None
    if metadata:
        versions[ext] = metadata
        save_version_cache(app_id, cache)
    return metadata, True


def resolve_version(
    app_id: str,
    wanted: str,
    refresh: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """Return (external version id, metadata) for an exact display version.

    External version IDs are ordered newest-first and their display versions
    decrease monotonically, so the lookup binary-searches the list instead of
    probing every entry. A version among 500 IDs takes about nine requests.
    """
    wanted = wanted.strip()
    if wanted[:1].lower() == "v" and wanted[1:2].isdigit():
        wanted = wanted[1:]
    if not wanted or wanted.lower() == "latest":
        return "", {}

    cache = load_version_cache(app_id)
    if refresh:
        cache = {"app_id": app_id, "versions": {}}
    versions: Dict[str, Any] = cache["versions"]

    # Fast path: return immediately if the wanted version is already cached.
    for ext, metadata in versions.items():
        if isinstance(metadata, dict) and str(metadata.get("displayVersion")) == wanted:
            return ext, metadata

    ids = list_external_versions(app_id)
    if not ids:
        raise CliError("App Store returned no versions for app %s" % app_id)

    probes = 0
    low, high = 0, len(ids) - 1
    try:
        while low <= high:
            mid = (low + high) // 2
            ext = ids[mid]
            metadata, fetched = _cached_version_metadata(
                app_id, ext, versions, cache
            )
            if fetched:
                probes += 1
                _progress_line(
                    "Checking metadata: probe %d" % probes,
                    tag="versions",
                )
            if metadata is None:
                # A failed response hides the ordering; scan the remaining
                # window rather than silently missing a match.
                for index in range(low, high + 1):
                    if index == mid:
                        continue
                    ext = ids[index]
                    metadata, fetched = _cached_version_metadata(
                        app_id, ext, versions, cache
                    )
                    if fetched:
                        probes += 1
                    if metadata and (
                        str(metadata.get("displayVersion")) == wanted
                    ):
                        return ext, metadata
                break
            display = str(metadata.get("displayVersion") or "")
            if display == wanted:
                return ext, metadata
            if _version_compare(display, wanted) > 0:
                # The probe is newer than the target, so the target is older
                # (further down the newest-first list).
                low = mid + 1
            else:
                # The probe is older than the target, so search newer.
                high = mid - 1
    finally:
        _finish_progress()

    latest, _ = _cached_version_metadata(app_id, ids[0], versions, cache)
    raise CliError(
        "version %s was not found for app %s (latest available: %s); "
        "you can pass --external-version-id directly"
        % (
            wanted,
            app_id,
            (latest or {}).get("displayVersion", "unknown"),
        )
    )


# ---------------------------------------------------------------------------
# IPA download and inspection
# ---------------------------------------------------------------------------


def confirm_purchase(app: Dict[str, Any], purchase: bool) -> None:
    if not purchase or app_is_free(app):
        return
    if not ask_yes_no(
        "ipa-dlp",
        "This is a paid app. --purchase may charge your Apple ID. Continue?",
    ):
        raise Cancelled()


def app_is_free(app: Dict[str, Any]) -> bool:
    value = app.get("price")
    if value is None:
        return False
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return str(value).strip() in ("0", "0.00", "Free")


def _clear_progress_line() -> None:
    """Erase the live progress line so streamed output can be written."""
    if JSON_OUTPUT or not sys.stderr.isatty():
        return
    sys.stderr.write("\r\x1b[K")
    sys.stderr.flush()


def _percent_from_line(line: str) -> Optional[float]:
    matches = re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%", line)
    if not matches:
        return None
    try:
        value = float(matches[-1])
    except ValueError:
        return None
    return value if 0.0 <= value <= 100.0 else None


def _is_json_line(line: str) -> bool:
    stripped = strip_ansi(line).strip()
    if not stripped.startswith("{"):
        return False
    try:
        json.loads(stripped)
    except Exception:
        return False
    return True


def _stream_line(line: str, tag: str, lock: "threading.Lock") -> None:
    text = strip_ansi(line).rstrip()
    if not text:
        return
    with lock:
        _clear_progress_line()
        log(text, tag=tag)


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3600:
        return "%d:%02d:%02d" % (
            total // 3600,
            (total % 3600) // 60,
            total % 60,
        )
    return "%02d:%02d" % (total // 60, total % 60)


def _progress_text(
    megabytes: float,
    speed: float,
    elapsed: float,
    percent: Optional[float],
    total_megabytes: Optional[float] = None,
) -> str:
    if percent is not None:
        percent = max(0.0, min(100.0, percent))
        width = 24
        filled = max(0, min(width, int(width * percent / 100.0)))
        bar = "█" * filled + "░" * (width - filled)
        if total_megabytes:
            if speed > 0:
                eta = _format_duration(
                    max(0.0, total_megabytes - megabytes) / speed
                )
            else:
                eta = "--:--"
            return "[%s] %5.1f%% of %.1fMiB at %.1fMiB/s ETA %s" % (
                bar,
                percent,
                total_megabytes,
                speed,
                eta,
            )
        return "[%s] %5.1f%% at %.1fMiB/s (%s)" % (
            bar,
            percent,
            speed,
            _format_duration(elapsed),
        )
    return "%.1fMiB at %.1fMiB/s (%s)" % (
        megabytes,
        speed,
        _format_duration(elapsed),
    )


def _watch_size(watch: Optional[Path]) -> Optional[int]:
    """Return the current download size, falling back to ipatool's .tmp file."""
    if watch is None:
        return None
    for candidate in (watch, watch.with_name(watch.name + ".tmp")):
        try:
            if candidate.exists():
                return candidate.stat().st_size
        except OSError:
            continue
    return None


def run_with_progress(
    argv: Sequence[Any],
    watch: Optional[Path],
    label: str,
    timeout: int = 1800,
    total_bytes: Optional[int] = None,
    tag: str = "download",
) -> Tuple[int, str]:
    """Run *argv* while streaming stdout/stderr and reporting progress.

    Both pipes are read line by line on reader threads, so tool output appears
    immediately instead of only on failure. The output file (or ipatool's
    ``.tmp`` streaming file) is watched for live bytes; a known *total_bytes*
    or a percentage in the tool's output drives a single progress line with a
    bar, percentage, speed, and ETA. The line is cleared with
    ``\\r\\x1b[K`` before any streamed line is written.
    """
    cmd = [str(item) for item in argv]
    vlog("+ " + " ".join(cmd))
    started = time.time()
    last_report = 0.0
    last_size = 0.0
    last_speed_time = started
    output_chunks: list[str] = []
    lock = threading.Lock()
    state: Dict[str, Optional[float]] = {"percent": None}
    total_megabytes = total_bytes / (1024 * 1024) if total_bytes else None

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
    except FileNotFoundError as exc:
        raise CliError("missing command: %s" % cmd[0]) from exc
    assert process.stdout is not None and process.stderr is not None

    def reader(stream: Any, stream_tag: str) -> None:
        try:
            for raw in stream:
                line = raw.rstrip("\n")
                with lock:
                    output_chunks.append(line + "\n")
                percent = _percent_from_line(line)
                if percent is not None:
                    state["percent"] = percent
                    continue
                if _is_json_line(line):
                    # ipatool's machine-readable result line is parsed later.
                    continue
                _stream_line(line, stream_tag, lock)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    threads = [
        threading.Thread(target=reader, args=(process.stdout, tag), daemon=True),
        threading.Thread(target=reader, args=(process.stderr, tag), daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        while process.poll() is None:
            now = time.time()
            if now - started > timeout:
                raise CliError("%s timed out" % label)
            interval = 0.5 if sys.stderr.isatty() else 15
            if now - last_report >= interval:
                last_report = now
                elapsed = now - started
                size = _watch_size(watch)
                if size is not None:
                    megabytes = size / (1024 * 1024)
                    dt = now - last_speed_time
                    speed = (megabytes - last_size) / dt if dt > 0 else 0.0
                    last_size = megabytes
                    last_speed_time = now
                    percent = state["percent"]
                    if total_bytes:
                        percent = 100.0 * size / total_bytes
                    _progress_line(
                        _progress_text(
                            megabytes,
                            speed,
                            elapsed,
                            percent,
                            total_megabytes,
                        ),
                        tag=tag,
                    )
                else:
                    _progress_line("%.0fs" % elapsed, tag=tag)
            time.sleep(0.05)
        process.wait()
    except Exception:
        process.kill()
        process.wait()
        raise
    finally:
        for thread in threads:
            thread.join(timeout=5)
        _finish_progress()

    return process.returncode, "".join(output_chunks)


def download_ipa(
    app: Dict[str, Any],
    external_id: str,
    platform_name: str,
    destination: Path,
    purchase: bool = False,
) -> Dict[str, Any]:
    app_id = str(app.get("id", ""))
    total_bytes = 0
    try:
        total_bytes = int(app.get("size") or 0)
    except (TypeError, ValueError):
        total_bytes = 0
    if not total_bytes and app_id:
        # Search results from ipatool do not carry a size; the public lookup
        # does, and it is what makes the bar and ETA possible. For a pinned
        # older version this is the current version's size, so the bar is an
        # estimate, but still far more useful than no ETA.
        looked_up = itunes_lookup({"id": app_id}, platform_name)
        if looked_up:
            try:
                total_bytes = int(looked_up.get("size") or 0)
            except (TypeError, ValueError):
                total_bytes = 0
            if total_bytes:
                app["size"] = total_bytes
    cmd: List[Any] = [
        ipatool_path(),
        "download",
        "-i",
        app_id,
        "-o",
        str(destination),
        "--non-interactive",
    ]
    if purchase:
        cmd.append("--purchase")
    if external_id:
        cmd.extend(["--external-version-id", external_id])
    if platform_name:
        cmd.extend(["--platform", platform_name])

    code, output = run_with_progress(
        cmd,
        destination,
        "downloading IPA",
        timeout=1800,
        total_bytes=total_bytes or None,
    )
    parsed = parse_json(output)
    if code != 0 or (parsed and parsed.get("success") is False):
        raise CliError(
            "ipatool download failed (exit %s): %s" % (code, tail(output))
        )
    if not destination.exists() or destination.stat().st_size < 1024:
        raise CliError("download produced no IPA: %s" % tail(output))
    destination.chmod(0o600)
    return read_ipa_info(destination)


def _plist_info(
    plist: Dict[str, Any],
    path: Path,
    root: str,
) -> Dict[str, Any]:
    """Build the shared info dict from a parsed app Info.plist."""
    executable = str(plist.get("CFBundleExecutable") or "")
    if not executable:
        raise CliError("app Info.plist has no CFBundleExecutable")
    name = (
        plist.get("CFBundleDisplayName")
        or plist.get("CFBundleName")
        or plist.get("CFBundleIdentifier")
        or "App"
    )
    return {
        "path": str(path),
        "root": root,
        "bundle_id": str(plist.get("CFBundleIdentifier") or ""),
        "name": str(name),
        "version": str(plist.get("CFBundleShortVersionString") or ""),
        "build": str(plist.get("CFBundleVersion") or ""),
        "executable": executable,
        "minimum_os": str(plist.get("MinimumOSVersion") or ""),
        "sinf": None,
    }


def read_ipa_info(ipa: Path) -> Dict[str, Any]:
    try:
        archive = zipfile.ZipFile(str(ipa))
    except Exception as exc:
        raise CliError("not a readable IPA: %s" % ipa) from exc
    with archive:
        names = archive.namelist()
        infos = [
            name
            for name in names
            if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)
        ]
        if len(infos) != 1:
            raise CliError(
                "IPA must contain exactly one root app (found %d)" % len(infos)
            )
        info_member = infos[0]
        root = info_member[: -len("/Info.plist")]
        try:
            plist = plistlib.loads(archive.read(info_member))
        except Exception as exc:
            raise CliError("could not parse app Info.plist") from exc
        if not isinstance(plist, dict):
            raise CliError("app Info.plist is not a dictionary")
        info = _plist_info(plist, ipa, root)
        sinf = "%s/SC_Info/%s.sinf" % (root, info["executable"])
        # A decrypted/stripped IPA legitimately has no license; callers that
        # need one (extraction of an encrypted binary) check this field.
        info["sinf"] = sinf if sinf in names else None
        return info


def read_app_info(app_dir: Path) -> Dict[str, Any]:
    """Read the same info dict from an unpacked .app bundle."""
    app_dir = app_dir.expanduser()
    plist_path = app_dir / "Info.plist"
    if not plist_path.is_file():
        raise CliError("app bundle has no Info.plist: %s" % app_dir)
    try:
        plist = plistlib.loads(plist_path.read_bytes())
    except Exception as exc:
        raise CliError("could not parse app Info.plist") from exc
    if not isinstance(plist, dict):
        raise CliError("app Info.plist is not a dictionary")
    info = _plist_info(plist, app_dir, str(app_dir))
    executable = app_dir / info["executable"]
    if not executable.is_file():
        raise CliError("app bundle has no executable at %s" % executable)
    sinf = app_dir / "SC_Info" / (info["executable"] + ".sinf")
    info["sinf"] = str(sinf) if sinf.is_file() else None
    return info


def extract_app(ipa: Path) -> Tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="ipa-dlp-ipa-"))
    completed = run(["ditto", "-x", "-k", ipa, root], timeout=600, check=False)
    if completed.returncode != 0:
        shutil.rmtree(root, ignore_errors=True)
        raise CliError("could not unpack IPA: %s" % tail(completed.stderr))
    apps = [
        item
        for item in (root / "Payload").glob("*.app")
        if item.is_dir() and (item / "Info.plist").exists()
    ]
    if len(apps) != 1:
        shutil.rmtree(root, ignore_errors=True)
        raise CliError("IPA payload has %d root apps" % len(apps))
    return apps[0], root


# ---------------------------------------------------------------------------
# Mach-O slicing
# ---------------------------------------------------------------------------

# On-disk Mach-O magics.
MH_MAGIC = b"\xce\xfa\xed\xfe"        # 32-bit
MH_MAGIC_64 = b"\xcf\xfa\xed\xfe"     # 64-bit
MH_CIGAM = b"\xfe\xed\xfa\xce"
MH_CIGAM_64 = b"\xfe\xed\xfa\xcf"
FAT_MAGIC = b"\xca\xfe\xba\xbe"       # fat header, big-endian fields
FAT_MAGIC_64 = b"\xca\xfe\xba\xbf"    # fat header, 64-bit offsets
FAT_CIGAM = b"\xbe\xba\xfe\xca"
FAT_CIGAM_64 = b"\xbf\xba\xfe\xca"
CPU_TYPE_X86_64 = 0x01000007
CPU_TYPE_ARM64 = 0x0100000C

FAT_HEADER_SIZE = 8
FAT_ENTRY_SIZE = 20
FAT_ENTRY_SIZE_64 = 32

# Load commands that carry the FairPlay cryptid.
LC_ENCRYPTION_INFO = 0x21
LC_ENCRYPTION_INFO_64 = 0x2C
MH_HEADER_SIZE = 28
MH_HEADER_SIZE_64 = 32


def _uint(blob: bytes, offset: int, width: int, big_endian: bool) -> int:
    return int.from_bytes(
        blob[offset : offset + width],
        "big" if big_endian else "little",
    )


def macho_slice(blob: bytes) -> Tuple[int, int]:
    """Return (offset, size) of the arm64 (or first 64-bit) Mach-O slice."""
    magic = blob[:4]
    if magic in (MH_MAGIC, MH_MAGIC_64, MH_CIGAM, MH_CIGAM_64):
        return 0, len(blob)

    if magic in (FAT_MAGIC, FAT_MAGIC_64):
        big_endian = True
    elif magic in (FAT_CIGAM, FAT_CIGAM_64):
        big_endian = False
    else:
        raise CliError(
            "not a Mach-O binary (magic %s); is the IPA decrypted?" % magic.hex()
        )

    wide = magic in (FAT_MAGIC_64, FAT_CIGAM_64)
    entry_size = FAT_ENTRY_SIZE_64 if wide else FAT_ENTRY_SIZE
    count = _uint(blob, 4, 4, big_endian)
    fallback: Optional[Tuple[int, int]] = None
    for index in range(count):
        base = FAT_HEADER_SIZE + index * entry_size
        if base + entry_size > len(blob):
            raise CliError("truncated fat Mach-O header")
        cpu = _uint(blob, base, 4, big_endian)
        if wide:
            offset = _uint(blob, base + 8, 8, big_endian)
            size = _uint(blob, base + 16, 8, big_endian)
        else:
            offset = _uint(blob, base + 8, 4, big_endian)
            size = _uint(blob, base + 12, 4, big_endian)
        if size <= 0 or offset + size > len(blob):
            continue
        if cpu == CPU_TYPE_ARM64:
            return offset, size
        if cpu == CPU_TYPE_X86_64 and fallback is None:
            fallback = (offset, size)
    if fallback is not None:
        return fallback
    raise CliError("no arm64 or x86_64 slice in fat Mach-O binary")


def _write_binary(data: bytes, dest: Path) -> Path:
    """Write *data* to *dest* as an executable file."""
    dest = Path(dest).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        handle.write(data)
    dest.chmod(0o755)
    return dest


def read_ipa_executable(ipa: Path, info: Dict[str, Any]) -> bytes:
    """Return the arm64 executable slice stored in *ipa*."""
    member = "%s/%s" % (info.get("root", ""), info.get("executable", ""))
    try:
        archive = zipfile.ZipFile(str(ipa))
    except Exception as exc:
        raise CliError("not a readable IPA: %s" % ipa) from exc
    with archive:
        try:
            blob = archive.read(member)
        except KeyError as exc:
            raise CliError("IPA has no executable at %s" % member) from exc
    offset, size = macho_slice(blob)
    return blob[offset : offset + size]


def is_macho_encrypted(slice_data: bytes) -> bool:
    """Return True when a Mach-O slice still carries a FairPlay cryptid.

    Walks the load commands of a thin image looking for
    ``LC_ENCRYPTION_INFO_64`` (and the 32-bit ``LC_ENCRYPTION_INFO``) and
    reports whether ``cryptid`` is non-zero. A slice without an encryption
    command is treated as decrypted.
    """
    magic = slice_data[:4]
    if magic in (MH_MAGIC, MH_MAGIC_64):
        big_endian = False
    elif magic in (MH_CIGAM, MH_CIGAM_64):
        big_endian = True
    else:
        raise CliError(
            "not a Mach-O image (magic %s)" % magic.hex()
        )
    header_size = (
        MH_HEADER_SIZE_64
        if magic in (MH_MAGIC_64, MH_CIGAM_64)
        else MH_HEADER_SIZE
    )
    if len(slice_data) < header_size:
        raise CliError("truncated Mach-O header")
    count = _uint(slice_data, 16, 4, big_endian)
    command_bytes = _uint(slice_data, 20, 4, big_endian)
    offset = header_size
    end = min(len(slice_data), header_size + command_bytes)
    for _ in range(count):
        if offset + 8 > end:
            raise CliError("truncated Mach-O load commands")
        command = _uint(slice_data, offset, 4, big_endian)
        command_size = _uint(slice_data, offset + 4, 4, big_endian)
        if command_size < 8 or offset + command_size > end:
            raise CliError("invalid Mach-O load command size")
        if command in (LC_ENCRYPTION_INFO, LC_ENCRYPTION_INFO_64):
            if command_size < 20:
                raise CliError("truncated Mach-O encryption command")
            cryptid = _uint(slice_data, offset + 16, 4, big_endian)
            return cryptid != 0
        offset += command_size
    return False


def extract_macho_binary(ipa: Path, info: Dict[str, Any], dest: Path) -> Path:
    """Write the arm64 executable slice from the decrypted *ipa* to *dest*."""
    return _write_binary(read_ipa_executable(ipa, info), dest)


# ---------------------------------------------------------------------------
# FairPlay model and decryption
# ---------------------------------------------------------------------------


def doctor(ipa: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    cmd: List[Any] = [
        unfairplay_path(),
        "doctor",
        "--model",
        str(MODEL_DIR),
    ]
    if ipa is not None:
        cmd.extend(["--ipa", str(ipa)])
    completed = run(cmd, timeout=180, check=False)
    return parse_json((completed.stdout or "") + "\n" + (completed.stderr or ""))


def run_admin(argv: Sequence[Any], timeout: int = 1800) -> subprocess.CompletedProcess:
    if subprocess.run(
        ["sudo", "-n", "true"], capture_output=True, text=True
    ).returncode == 0:
        return run(["sudo"] + list(argv), timeout=timeout, check=False)

    if gui_enabled():
        askpass_dir = Path(tempfile.mkdtemp(prefix="ipa-dlp-askpass-"))
        askpass = askpass_dir / "askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            "exec /usr/bin/osascript -e 'display dialog \"ipa-dlp needs "
            "administrator access to read this Mac FairPlay enrollment. "
            "Enter your login password to continue.\" default answer \"\" "
            "with hidden answer buttons {\"Cancel\", \"Continue\"} "
            "default button \"Continue\" with title \"ipa-dlp Setup\" "
            "with icon caution' -e 'text returned of result'\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        env = os.environ.copy()
        env["SUDO_ASKPASS"] = str(askpass)
        env["SUDO_ASKPASS_REQUIRE"] = "force"
        try:
            return run(["sudo", "-A"] + list(argv), timeout=timeout, check=False, env=env)
        finally:
            shutil.rmtree(askpass_dir, ignore_errors=True)

    return run(["sudo"] + list(argv), timeout=timeout, check=False)


def ensure_model(
    ipa: Path,
    info: Dict[str, Any],
    app_path: Optional[Path] = None,
) -> Dict[str, Any]:
    current = doctor(ipa)
    if current and current.get("decrypt_ready"):
        return current

    log("no matching FairPlay model; recovering the enrollment model", tag="unfairplay")
    log("macOS will ask for your login password once", tag="unfairplay")
    cleanup: Optional[Path] = None
    if app_path is None:
        app_path, cleanup = extract_app(ipa)
    try:
        completed = run_admin(
            [
                unfairplay_path(),
                "setup",
                "--app",
                str(app_path),
                "--ipa",
                str(ipa),
                "--model-dir",
                str(MODEL_DIR),
            ]
        )
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)

    parsed = parse_json((completed.stdout or "") + "\n" + (completed.stderr or ""))
    if completed.returncode != 0 or not parsed or not parsed.get("success"):
        raise CliError(
            "FairPlay model recovery failed: %s"
            % tail((completed.stderr or "") + "\n" + (completed.stdout or ""))
        )
    if parsed.get("plaintext_verified") is False:
        log(
            "warning: plaintext_verified=false; the model was saved but this "
            "IPA has no pre-encryption hash table"
        )
    log("model saved: %s" % parsed.get("model", MODEL_DIR), tag="unfairplay")

    final = doctor(ipa)
    if not final or not final.get("decrypt_ready"):
        raise CliError(
            "model was saved but the IPA still does not validate; "
            "the Mac App Store/FairPlay enrollment may belong to a different "
            "Apple ID than the ipatool account"
        )
    return final


def default_output(info: Dict[str, Any]) -> Path:
    name = re.sub(r"[^0-9A-Za-z._ -]+", "", info.get("name", "")).strip()
    name = re.sub(r"\s+", "-", name) or info.get("bundle_id") or "App"
    version = info.get("version") or "unknown"
    return Path.home() / "Downloads" / ("%s-%s.ipa" % (name, version))


def output_base(output: Path) -> Path:
    return output.with_suffix("") if output.suffix.lower() == ".ipa" else output


def report_path(output: Path) -> Path:
    base = output_base(output)
    return base.parent / (base.name + ".report.json")


def encrypted_path(output: Path) -> Path:
    base = output_base(output)
    return base.parent / (base.name + ".encrypted.ipa")


def binary_path(output: Path) -> Path:
    base = output_base(output)
    return base.parent / (base.name + ".bin")


def format_template(
    template: str,
    info: Dict[str, Any],
    external_id: str = "",
) -> str:
    """Expand %(name)s-style tokens from *info* in an output or hook string."""
    values = {
        "name": info.get("name", ""),
        "bundle_id": info.get("bundle_id", ""),
        "version": info.get("version", ""),
        "build": info.get("build", ""),
        "external_id": external_id,
    }
    try:
        return template % values
    except (KeyError, TypeError, ValueError) as exc:
        raise CliError(
            "could not expand template %r: %s (known tokens: %%(name)s, "
            "%%(bundle_id)s, %%(version)s, %%(build)s, %%(external_id)s; "
            "use %%%% for a literal %%)" % (template, exc)
        ) from exc


def run_exec_hook(
    cmd_template: str,
    target_path: Path,
    info: Dict[str, Any],
    external_id: str = "",
) -> None:
    """Run a shell hook with templates expanded and {} = *target_path*."""
    cmd = format_template(cmd_template, info, external_id).replace(
        "{}", str(target_path)
    )
    log("running hook: %s" % cmd)
    vlog("+ " + cmd)
    options: Dict[str, Any] = {}
    if JSON_OUTPUT:
        # --json promises a single object on stdout; hooks must not add to it.
        options["stdout"] = sys.stderr
        options["stderr"] = sys.stderr
    try:
        subprocess.run(cmd, shell=True, check=True, **options)
    except subprocess.CalledProcessError as exc:
        raise CliError(
            "--exec hook failed (exit %s): %s" % (exc.returncode, cmd)
        ) from exc


def verify_stripped_ipa(path: Path) -> None:
    """Fail closed when a supposedly stripped IPA still has account data."""
    try:
        archive = zipfile.ZipFile(str(path))
    except Exception as exc:
        raise CliError("could not inspect stripped IPA: %s" % path) from exc
    with archive:
        names = archive.namelist()
        licenses = [
            name
            for name in names
            if any(part.lower() == "sc_info" for part in name.split("/") if part)
        ]
        if licenses:
            raise CliError(
                "stripped IPA still contains SC_Info licenses: %s"
                % ", ".join(licenses[:3])
            )
        if "iTunesMetadata.plist" in names:
            try:
                metadata = plistlib.loads(archive.read("iTunesMetadata.plist"))
            except Exception as exc:
                raise CliError(
                    "could not verify stripped iTunesMetadata.plist"
                ) from exc
            if not isinstance(metadata, dict):
                raise CliError("stripped iTunesMetadata.plist is not a dictionary")
            remaining = sorted(set(metadata) & ACCOUNT_METADATA_KEYS)
            download = metadata.get("com.apple.iTunesStore.downloadInfo")
            if isinstance(download, dict) and "accountInfo" in download:
                remaining.append("com.apple.iTunesStore.downloadInfo.accountInfo")
            if remaining:
                raise CliError(
                    "stripped IPA still exposes account metadata: %s"
                    % ", ".join(remaining)
                )


def decrypt_ipa(
    ipa: Path,
    info: Dict[str, Any],
    output: Path,
    keep_account_data: bool = False,
    force: bool = False,
    full_output: bool = False,
    app_path: Optional[Path] = None,
) -> Dict[str, Any]:
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not force:
        raise CliError("output already exists (use --force): %s" % output)
    if full_output and report_path(output).exists() and not force:
        raise CliError(
            "report already exists (use --force): %s" % report_path(output)
        )

    ensure_model(ipa, info, app_path)

    temporary_report_dir: Optional[Path] = None
    if full_output:
        report = report_path(output)
    else:
        temporary_report_dir = Path(
            tempfile.mkdtemp(prefix="ipa-dlp-report-")
        )
        report = temporary_report_dir / "report.json"

    cmd: List[Any] = [
        unfairplay_path(),
        "decrypt",
        "--input",
        str(ipa),
        "--output",
        str(output),
        "--report",
        str(report),
    ]
    if not keep_account_data:
        cmd.append("--strip-account-data")
    if force:
        cmd.append("--force")

    log("decrypting %s" % ipa.name, tag="unfairplay")
    try:
        completed = run(cmd, timeout=1800, check=False)
        parsed = parse_json(
            (completed.stdout or "") + "\n" + (completed.stderr or "")
        )
        if completed.returncode != 0 or not parsed:
            raise CliError(
                "decryption failed: %s"
                % tail(
                    (completed.stderr or "") + "\n" + (completed.stdout or "")
                )
            )
        if not output.exists():
            raise CliError("decryption reported success but output is missing")
        output.chmod(0o600)
        if full_output:
            report.chmod(0o600)
        if not parsed.get("PlaintextVerified"):
            log("warning: plaintext verification was false for this IPA", tag="unfairplay")

        if not keep_account_data:
            if parsed.get("AccountDataStripped") is not True:
                output.unlink(missing_ok=True)
                if full_output:
                    report.unlink(missing_ok=True)
                raise CliError(
                    "UnfairPlay did not confirm account-data stripping; "
                    "no output was kept"
                )
            try:
                verify_stripped_ipa(output)
            except CliError:
                output.unlink(missing_ok=True)
                if full_output:
                    report.unlink(missing_ok=True)
                raise
        return parsed
    finally:
        if temporary_report_dir is not None:
            shutil.rmtree(temporary_report_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    ensure_tools(update=args.update)
    info = ensure_auth(force=args.reauth)
    log("account: %s" % (info.get("email") or info.get("name") or "unknown"))

    if args.seed:
        log("warming the FairPlay model with %r" % args.seed)
        app = resolve_app(args.seed, args.platform)
        external_id = ""
        if args.version:
            external_id, _ = resolve_version(str(app.get("id")), args.version)
        work = Path(tempfile.mkdtemp(prefix="ipa-dlp-seed-"))
        try:
            ipa = work / "App.ipa"
            seed_purchase = args.purchase or app_is_free(app)
            confirm_purchase(app, seed_purchase)
            downloaded = download_ipa(
                app,
                external_id,
                args.platform,
                ipa,
                purchase=seed_purchase,
            )
            app_path, app_root = extract_app(ipa)
            try:
                ensure_model(ipa, downloaded, app_path)
            finally:
                shutil.rmtree(app_root, ignore_errors=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        log("FairPlay model is ready")
    else:
        state = doctor()
        if state and (
            state.get("decrypt_ready")
            or (state.get("checks") or {}).get("model_valid")
        ):
            log("FairPlay model is ready")
        else:
            log(
                "no FairPlay model yet; it will be created automatically on "
                "the first `ipa-dlp get`, with one admin prompt"
            )

    log("")
    log("Installed and authenticated. Try:")
    log("  ipa-dlp get spotify")
    log("  ipa-dlp get spotify --version 9.1.88")
    if JSON_OUTPUT:
        _emit_json(
            {
                "ok": True,
                "command": "setup",
                "version": VERSION,
                "account": (info or {}).get("email") or "unknown",
                "ipatool_version": IPATOOL_VERSION,
                "unfairplay_version": UNFAIRPLAY_VERSION,
            }
        )
    return 0


def _app_summary(app: Dict[str, Any]) -> Dict[str, str]:
    return {
        "id": str(app.get("id", "")),
        "bundle_id": str(app.get("bundleID", "")),
        "name": str(app.get("name", "")),
    }


def _info_summary(info: Dict[str, Any]) -> Dict[str, str]:
    return {
        "name": str(info.get("name", "")),
        "bundle_id": str(info.get("bundle_id", "")),
        "version": str(info.get("version", "")),
        "build": str(info.get("build", "")),
    }


def _emit_json(value: Dict[str, Any]) -> None:
    """Print exactly one JSON object on stdout."""
    print(json.dumps(value, indent=2, sort_keys=True))


def _get_result(
    query: str,
    version: str,
    output: Optional[str],
    keep_account_data: bool = False,
    force: bool = False,
    external_version_id: str = "",
    platform_name: str = DEFAULT_PLATFORM,
    dry_run: bool = False,
    reveal: bool = False,
    purchase: bool = False,
    full_output: bool = False,
    list_versions: bool = False,
    limit: int = 15,
    extract_binary: bool = False,
    exec_hook: str = "",
) -> Dict[str, Any]:
    """Run the get flow and return a structured result.

    Nothing is written to stdout here: the CLI decides between a plain path and
    a JSON object, and the library API returns paths only.
    """
    query, version = _split_query_version(query, version)
    ensure_tools()
    ensure_auth()

    app = resolve_app(query, platform_name)
    app_id = str(app.get("id", ""))
    log(
        "app: %s [%s] id=%s"
        % (app.get("name", "?"), app.get("bundleID", "?"), app_id)
    )

    if list_versions:
        rows = list_versions_table(app_id, limit, render=False)
        return {
            "ok": True,
            "command": "versions",
            "query": query,
            "app": _app_summary(app),
            "versions": rows,
        }

    external_id = external_version_id.strip()
    metadata: Dict[str, Any] = {}
    if version and not external_id:
        external_id, metadata = resolve_version(app_id, version)
        log(
            "version: %s (external id %s)"
            % (metadata.get("displayVersion", version), external_id)
        )

    if dry_run:
        log("dry run: would download and decrypt this app")
        return {
            "ok": True,
            "command": "get",
            "query": query,
            "dry_run": True,
            "app": _app_summary(app),
            "external_id": external_id,
            "artifact": str(Path(output).expanduser()) if output else "",
            "ipa": None,
            "binary": None,
        }

    work = Path(tempfile.mkdtemp(prefix="ipa-dlp-get-"))
    try:
        ipa = work / "App.ipa"
        purchase = purchase or app_is_free(app)
        confirm_purchase(app, purchase)
        if purchase:
            log("obtaining a free App Store license if needed", tag="ipatool")
        info = download_ipa(app, external_id, platform_name, ipa, purchase)
        log("downloaded: %s %s" % (info.get("name"), info.get("version")), tag="download")
        if version and info.get("version") != version:
            raise CliError(
                "downloaded version %s does not match requested %s"
                % (info.get("version"), version)
            )
        destination = (
            Path(format_template(output, info, external_id)).expanduser()
            if output
            else default_output(info)
        )
        binary_dest = binary_path(destination) if extract_binary else None
        if binary_dest is not None and binary_dest.exists() and not force:
            raise CliError(
                "binary already exists (use --force): %s" % binary_dest
            )
        if full_output:
            encrypted = encrypted_path(destination)
            if encrypted.exists() and not force:
                raise CliError(
                    "encrypted IPA already exists (use --force): %s" % encrypted
                )
        report = decrypt_ipa(
            ipa,
            info,
            destination,
            keep_account_data=keep_account_data,
            force=force,
            full_output=full_output,
        )
        if full_output:
            encrypted = encrypted_path(destination)
            shutil.move(str(ipa), str(encrypted))
            encrypted.chmod(0o600)
        if binary_dest is not None:
            extract_macho_binary(destination, info, binary_dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    artifact = binary_dest if binary_dest is not None else destination
    result: Dict[str, Any] = {
        "ok": True,
        "command": "get",
        "query": query,
        "app": _app_summary(app),
        "external_id": external_id,
        **_info_summary(info),
        "artifact": str(artifact),
        "ipa": str(destination),
        "binary": str(binary_dest) if binary_dest is not None else None,
        "encrypted_ipa": str(encrypted_path(destination)) if full_output else None,
        "report": str(report_path(destination)) if full_output else None,
        "verified": bool(report.get("PlaintextVerified")),
        "account_data_stripped": not keep_account_data,
    }
    log("decrypted IPA: %s" % destination)
    if binary_dest is not None:
        log("binary: %s" % binary_dest)
    log("verified: %s" % ("yes" if report.get("PlaintextVerified") else "no"))
    if keep_account_data:
        log("account data stripped: no (kept by request)")
    else:
        log("account data stripped: yes")
    if full_output:
        log("encrypted IPA: %s" % encrypted_path(destination))
        log("report: %s" % report_path(destination))
    log("done.")
    if exec_hook:
        run_exec_hook(exec_hook, artifact, info, external_id)
    if reveal:
        subprocess.run(["open", "-R", str(artifact)], check=False)
    return result


def cmd_get(args: argparse.Namespace) -> int:
    if args.version and args.pos_version and args.version != args.pos_version:
        raise CliError(
            "conflicting versions: --version %s and positional %s"
            % (args.version, args.pos_version)
        )
    version = args.version or args.pos_version or ""
    result = _get_result(
        args.query,
        version,
        args.output,
        keep_account_data=args.keep_account_data,
        force=args.force,
        external_version_id=args.external_version_id or "",
        platform_name=args.platform,
        dry_run=args.dry_run,
        reveal=args.reveal,
        purchase=args.purchase,
        full_output=args.full,
        list_versions=args.list_versions,
        limit=args.limit,
        extract_binary=args.extract_binary,
        exec_hook=args.exec_cmd or "",
    )
    if args.list_versions:
        if JSON_OUTPUT:
            _emit_json(result)
        else:
            render_versions_table(result["versions"])
        return 0
    if JSON_OUTPUT:
        _emit_json(result)
    elif not result.get("dry_run"):
        print(result["artifact"])
    return 0


def _decrypt_result(
    ipa: Path,
    output: Optional[str],
    keep_account_data: bool = False,
    force: bool = False,
    full_output: bool = False,
    extract_binary: bool = False,
    exec_hook: str = "",
    reveal: bool = False,
) -> Dict[str, Any]:
    """Run the decrypt flow and return a structured result (stderr only)."""
    ensure_tools()
    ipa = Path(ipa).expanduser()
    if not ipa.exists():
        raise CliError("IPA not found: %s" % ipa)
    info = read_ipa_info(ipa)
    destination = (
        Path(format_template(output, info)).expanduser()
        if output
        else default_output(info)
    )
    binary_dest = binary_path(destination) if extract_binary else None
    if binary_dest is not None and binary_dest.exists() and not force:
        raise CliError("binary already exists (use --force): %s" % binary_dest)
    report = decrypt_ipa(
        ipa,
        info,
        destination,
        keep_account_data=keep_account_data,
        force=force,
        full_output=full_output,
    )
    if binary_dest is not None:
        extract_macho_binary(destination, info, binary_dest)
    artifact = binary_dest if binary_dest is not None else destination
    result: Dict[str, Any] = {
        "ok": True,
        "command": "decrypt",
        "input": str(ipa),
        "external_id": "",
        **_info_summary(info),
        "artifact": str(artifact),
        "ipa": str(destination),
        "binary": str(binary_dest) if binary_dest is not None else None,
        "report": str(report_path(destination)) if full_output else None,
        "verified": bool(report.get("PlaintextVerified")),
        "account_data_stripped": not keep_account_data,
    }
    log("decrypted IPA: %s" % destination)
    if binary_dest is not None:
        log("binary: %s" % binary_dest)
    log("verified: %s" % ("yes" if report.get("PlaintextVerified") else "no"))
    if keep_account_data:
        log("account data stripped: no (kept by request)")
    else:
        log("account data stripped: yes")
    if full_output:
        log("report: %s" % report_path(destination))
    log("done.")
    if exec_hook:
        run_exec_hook(exec_hook, artifact, info)
    if reveal:
        subprocess.run(["open", "-R", str(artifact)], check=False)
    return result


def cmd_decrypt(args: argparse.Namespace) -> int:
    result = _decrypt_result(
        Path(args.ipa),
        args.output,
        keep_account_data=args.keep_account_data,
        force=args.force,
        full_output=args.full,
        extract_binary=args.extract_binary,
        exec_hook=args.exec_cmd or "",
        reveal=args.reveal,
    )
    if JSON_OUTPUT:
        _emit_json(result)
    else:
        print(result["artifact"])
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    result = _extract_local(
        Path(args.target),
        args.output,
        force=args.force,
        exec_hook=args.exec_cmd or "",
    )
    if JSON_OUTPUT:
        _emit_json(result)
    else:
        print(result["artifact"])
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ensure_tools()
    info = auth_info()
    state = doctor()
    model_ready = bool(
        state
        and (
            state.get("decrypt_ready")
            or (state.get("checks") or {}).get("model_valid")
        )
    )
    payload = {
        "ok": True,
        "command": "status",
        "version": VERSION,
        "ipatool": str(ipatool_path()),
        "ipatool_version": tool_version(ipatool_path()),
        "unfairplay": str(unfairplay_path()),
        "unfairplay_version": tool_version(unfairplay_path()),
        "account": (info or {}).get("email") or "not signed in",
        "models": str(MODEL_DIR),
        "model_ready": model_ready,
        "data": str(DATA_DIR),
        "cache": str(CACHE_DIR),
    }
    if JSON_OUTPUT:
        _emit_json(payload)
        return 0
    print("ipa-dlp %s" % VERSION)
    print("ipatool:    %s (%s)" % (payload["ipatool"], payload["ipatool_version"]))
    print(
        "unfairplay: %s (%s)"
        % (payload["unfairplay"], payload["unfairplay_version"])
    )
    print("account:    %s" % payload["account"])
    print("models:     %s" % payload["models"])
    print(
        "decrypt:    %s"
        % ("model ready" if model_ready else "setup on first get")
    )
    print("data:       %s" % payload["data"])
    print("cache:      %s" % payload["cache"])
    return 0


# ---------------------------------------------------------------------------
# library API
# ---------------------------------------------------------------------------


def download(
    query: str,
    version: Optional[str] = None,
    output: Optional[str] = None,
    *,
    keep_account_data: bool = False,
    force: bool = False,
    external_version_id: str = "",
    platform_name: str = DEFAULT_PLATFORM,
    purchase: bool = False,
    full_output: bool = False,
    extract_binary: bool = False,
    exec_hook: str = "",
    dry_run: bool = False,
    reveal: bool = False,
) -> Path:
    """Download and decrypt *query*; return the final artifact path."""
    result = _get_result(
        query,
        version or "",
        output,
        keep_account_data=keep_account_data,
        force=force,
        external_version_id=external_version_id,
        platform_name=platform_name,
        dry_run=dry_run,
        reveal=reveal,
        purchase=purchase,
        full_output=full_output,
        extract_binary=extract_binary,
        exec_hook=exec_hook,
    )
    return Path(result["artifact"])


def decrypt(
    ipa_path: Union[str, Path],
    output: Optional[str] = None,
    *,
    keep_account_data: bool = False,
    force: bool = False,
    full_output: bool = False,
    extract_binary: bool = False,
    exec_hook: str = "",
    reveal: bool = False,
) -> Path:
    """Decrypt a local IPA; return the final artifact path."""
    result = _decrypt_result(
        Path(ipa_path),
        output,
        keep_account_data=keep_account_data,
        force=force,
        full_output=full_output,
        extract_binary=extract_binary,
        exec_hook=exec_hook,
        reveal=reveal,
    )
    return Path(result["artifact"])


def _slice_blob(blob: bytes) -> bytes:
    offset, size = macho_slice(blob)
    return blob[offset : offset + size]


def _default_extract_output(target: Path) -> Path:
    if target.is_dir():
        return target.parent / (target.stem + ".bin")
    if target.suffix.lower() == ".ipa" or zipfile.is_zipfile(str(target)):
        return binary_path(target)
    dest = target.parent / (target.stem + ".bin")
    if dest == target:
        dest = target.parent / (target.name + ".bin")
    return dest


def _decrypt_for_extract(ipa: Path, info: Dict[str, Any]) -> bytes:
    """Decrypt *ipa* into a temp directory and return its arm64 slice."""
    ensure_tools()
    work = Path(tempfile.mkdtemp(prefix="ipa-dlp-extract-"))
    try:
        decrypted = work / "App.ipa"
        decrypt_ipa(ipa, info, decrypted, keep_account_data=False, force=True)
        data = read_ipa_executable(decrypted, info)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if is_macho_encrypted(data):
        raise CliError("decrypted IPA still reports FairPlay encryption")
    return data


def _target_info(target: Path) -> Dict[str, Any]:
    """Read app metadata for an IPA, .app bundle, or raw Mach-O."""
    if target.is_dir():
        if target.suffix.lower() != ".app":
            raise CliError("input is not an .app bundle: %s" % target)
        return read_app_info(target)
    if zipfile.is_zipfile(str(target)):
        return read_ipa_info(target)
    return {
        "path": str(target),
        "root": str(target.parent),
        "bundle_id": "",
        "name": target.stem or target.name,
        "version": "",
        "build": "",
        "executable": target.name,
        "minimum_os": "",
        "sinf": None,
    }


def _target_macho_bytes(
    target: Path,
    info: Optional[Dict[str, Any]] = None,
) -> Tuple[bytes, bool, bool, Dict[str, Any]]:
    """Return (arm64 bytes, was_encrypted, was_decrypted, info)."""
    if info is None:
        info = _target_info(target)
    if target.is_dir():
        data = _slice_blob((target / info["executable"]).read_bytes())
        return data, is_macho_encrypted(data), False, info
    if zipfile.is_zipfile(str(target)):
        data = read_ipa_executable(target, info)
        encrypted = is_macho_encrypted(data)
        if encrypted:
            if not info.get("sinf"):
                raise CliError(
                    "IPA is encrypted but has no SC_Info license; "
                    "cannot decrypt %s" % target
                )
            data = _decrypt_for_extract(target, info)
            return data, True, True, info
        return data, False, False, info
    data = _slice_blob(target.read_bytes())
    return data, is_macho_encrypted(data), False, info


def _extract_local(
    target: Union[str, Path],
    output: Optional[str] = None,
    force: bool = False,
    exec_hook: str = "",
) -> Dict[str, Any]:
    """Extract the arm64 slice from an IPA, .app bundle, or raw Mach-O."""
    target = Path(target).expanduser()
    if not target.exists():
        raise CliError("input not found: %s" % target)
    info = _target_info(target)
    dest = (
        Path(format_template(output, info)).expanduser()
        if output
        else _default_extract_output(target)
    )
    if dest.exists() and not force:
        raise CliError("output already exists (use --force): %s" % dest)
    data, encrypted, decrypted, info = _target_macho_bytes(target, info)
    if encrypted and not decrypted:
        log("warning: the extracted image is still FairPlay-encrypted")
    _write_binary(data, dest)
    log("extracted arm64 slice: %s" % dest)
    if exec_hook:
        run_exec_hook(exec_hook, dest, info)
    return {
        "ok": True,
        "command": "extract",
        "input": str(target),
        "artifact": str(dest),
        "encrypted": encrypted,
        "decrypted": decrypted,
        **_info_summary(info),
        "external_id": "",
    }


def extract_binary(
    path: Union[str, Path],
    output: Optional[str] = None,
    *,
    force: bool = False,
    exec_hook: str = "",
) -> Path:
    """Extract the arm64 slice from a local IPA, .app, or Mach-O file."""
    return Path(_extract_local(path, output, force, exec_hook)["artifact"])


def load_macho(
    path_or_query: Union[str, Path],
    version: Optional[str] = None,
) -> bytes:
    """Return arm64 Mach-O bytes for a local path or an App Store query."""
    path_or_query, version = _split_query_version(
        str(path_or_query), version or ""
    )
    target = Path(path_or_query).expanduser()
    if target.exists():
        data, encrypted, decrypted, _ = _target_macho_bytes(target)
        if encrypted and not decrypted:
            log("warning: the Mach-O image is still FairPlay-encrypted")
        return data
    with tempfile.TemporaryDirectory(prefix="ipa-dlp-macho-") as work:
        ipa = download(
            str(path_or_query),
            version=version or None,
            output=str(Path(work) / "App.ipa"),
            force=True,
        )
        return read_ipa_executable(ipa, read_ipa_info(ipa))


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # These flags are valid before or after the subcommand. SUPPRESS keeps a
    # subparser from overwriting a value parsed by the root parser (and vice
    # versa); main() falls back to False when neither parser saw the flag.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--gui",
        action="store_true",
        default=argparse.SUPPRESS,
        help="use macOS dialogs instead of terminal prompts",
    )
    common.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="verbose command logging",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit one structured JSON object on stdout",
    )

    parser = argparse.ArgumentParser(
        prog=APP,
        description="Fetch, decrypt, and extract App Store IPAs and Mach-O binaries.",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  ipa-dlp get spotify --version 9.1.88\n"
            "  ipa-dlp get spotify -v 9.1.88 --extract-binary\n"
            "  ipa-dlp get spotify -F --limit 15\n"
            "  ipa-dlp extract ./Spotify.ipa -o Spotify.bin\n"
            "  ipa-dlp get spotify -v 9.1.88 --json\n\n"
            "python api:\n"
            "  from ipa_dlp import load_macho, download, extract_binary\n"
            "  blob = load_macho('spotify', version='9.1.88')  # arm64 Mach-O bytes\n"
        ),
    )
    parser.add_argument("--version", action="version", version="%s %s" % (APP, VERSION))
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser(
        "setup",
        help="install tools and sign in to the App Store",
        parents=[common],
    )
    setup.add_argument("--update", action="store_true", help="reinstall pinned tools")
    setup.add_argument("--reauth", action="store_true", help="force a new login")
    setup.add_argument("--seed", help="optional app to warm the FairPlay model now")
    setup.add_argument(
        "-v", "--version", dest="version", help="version for --seed"
    )
    setup.add_argument(
        "--purchase",
        action="store_true",
        help="allow obtaining a missing license for --seed",
    )
    setup.add_argument("--platform", default=DEFAULT_PLATFORM)
    setup.set_defaults(func=cmd_setup)

    get = subparsers.add_parser(
        "get", help="download and decrypt an app", parents=[common]
    )
    get.add_argument("query", help="app name, bundle ID, or App Store ID")
    get.add_argument("pos_version", nargs="?", help=argparse.SUPPRESS)
    get.add_argument("-v", "--version", help="exact app version, e.g. 9.1.88")
    get.add_argument("--external-version-id", help="skip version lookup with an App Store external version ID")
    get.add_argument("-o", "--output", help="output IPA path")
    get.add_argument(
        "-F",
        "--list-versions",
        action="store_true",
        help="list available App Store versions and exit",
    )
    get.add_argument(
        "--limit",
        type=int,
        default=15,
        metavar="N",
        help="number of versions to show with --list-versions (default: 15)",
    )
    get.add_argument(
        "--extract-binary",
        action="store_true",
        help="also extract the arm64 Mach-O executable to <output>.bin",
    )
    get.add_argument(
        "--exec",
        dest="exec_cmd",
        metavar="CMD",
        help="run CMD after a successful fetch; {} becomes the final artifact "
        "path and %%(name)s, %%(bundle_id)s, %%(version)s, %%(build)s and "
        "%%(external_id)s are expanded (use %%%% for a literal %%)",
    )
    get.add_argument("--platform", default=DEFAULT_PLATFORM)
    get.add_argument(
        "--keep-account-data",
        action="store_true",
        help="keep SC_Info licenses and account fields (default: strip them)",
    )
    get.add_argument(
        "--full",
        action="store_true",
        help="also keep the encrypted IPA and decryption report",
    )
    get.add_argument("--force", action="store_true")
    get.add_argument(
        "--purchase",
        action="store_true",
        help="obtain a license if missing (free apps are automatic)",
    )
    get.add_argument("--dry-run", action="store_true")
    get.add_argument("--reveal", action="store_true")
    get.set_defaults(func=cmd_get)

    decrypt = subparsers.add_parser(
        "decrypt",
        help="decrypt an IPA already downloaded to this Mac",
        parents=[common],
    )
    decrypt.add_argument("ipa")
    decrypt.add_argument("-o", "--output")
    decrypt.add_argument(
        "--keep-account-data",
        action="store_true",
        help="keep SC_Info licenses and account fields (default: strip them)",
    )
    decrypt.add_argument(
        "--full",
        action="store_true",
        help="keep the decryption report next to the output",
    )
    decrypt.add_argument(
        "--extract-binary",
        action="store_true",
        help="also extract the arm64 Mach-O executable to <output>.bin",
    )
    decrypt.add_argument(
        "--exec",
        dest="exec_cmd",
        metavar="CMD",
        help="run CMD after success; {} becomes the final artifact path and "
        "app tokens like %%(name)s are expanded",
    )
    decrypt.add_argument("--force", action="store_true")
    decrypt.add_argument("--reveal", action="store_true")
    decrypt.set_defaults(func=cmd_decrypt)

    extract = subparsers.add_parser(
        "extract",
        help="extract the arm64 Mach-O slice from an IPA, .app, or binary",
        parents=[common],
    )
    extract.add_argument("target", help="path to an .ipa, .app, or Mach-O file")
    extract.add_argument(
        "-o",
        "--output",
        help="output binary path; supports %%(name)s-style tokens",
    )
    extract.add_argument(
        "--exec",
        dest="exec_cmd",
        metavar="CMD",
        help="run CMD after success; {} becomes the binary path and app "
        "tokens like %%(name)s are expanded",
    )
    extract.add_argument("--force", action="store_true")
    extract.set_defaults(func=cmd_extract)

    status = subparsers.add_parser(
        "status", help="show tools, account, and model state", parents=[common]
    )
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    global USE_GUI, VERBOSE, JSON_OUTPUT
    if sys.version_info < (3, 9):
        eprint("error: Python 3.9 or newer is required")
        return 1
    parser = build_parser()
    args = parser.parse_args(argv)
    USE_GUI = bool(getattr(args, "gui", False))
    VERBOSE = bool(getattr(args, "verbose", False))
    JSON_OUTPUT = bool(getattr(args, "json", False))
    if os.geteuid() == 0:
        message = (
            "run ipa-dlp as your normal user; it asks for admin only when needed"
        )
        eprint("error: %s" % message)
        if JSON_OUTPUT:
            _emit_json({"ok": False, "error": message})
        return 1
    ensure_private_dir(DATA_DIR)
    ensure_private_dir(CACHE_DIR)
    try:
        return int(args.func(args))
    except Cancelled:
        log("canceled")
        if JSON_OUTPUT:
            _emit_json({"ok": False, "error": "canceled"})
        return 130
    except CliError as exc:
        eprint("error: %s" % exc)
        if JSON_OUTPUT:
            _emit_json({"ok": False, "error": str(exc)})
        return 1
    except EOFError:
        log("canceled (no input)")
        if JSON_OUTPUT:
            _emit_json({"ok": False, "error": "canceled (no input)"})
        return 130
    except KeyboardInterrupt:
        log("interrupted")
        if JSON_OUTPUT:
            _emit_json({"ok": False, "error": "interrupted"})
        return 130


if __name__ == "__main__":
    sys.exit(main())
