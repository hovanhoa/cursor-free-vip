import os
import sys
import json
import requests
import sqlite3
from typing import Dict, Optional, Tuple
import platform
from colorama import Fore, Style, init
import logging
import re

# Initialize colorama for cross-platform color support
init(autoreset=True)

# Setup logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Emoji constants
EMOJI = {
    "USER": "👤",
    "USAGE": "📊",
    "PREMIUM": "⭐",
    "BASIC": "📝",
    "SUBSCRIPTION": "💳",
    "INFO": "ℹ️",
    "ERROR": "❌",
    "SUCCESS": "✅",
    "WARNING": "⚠️",
    "TIME": "🕒",
}

# ANSI escape code pattern (compiled once, used everywhere)
_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    NAME_LOWER = "cursor"
    NAME_CAPITALIZE = "Cursor"
    BASE_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.124 Safari/537.36"
        ),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    REQUEST_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def get_platform_paths() -> Optional[Dict[str, str]]:
    """
    Return a dict with keys ``storage_path``, ``sqlite_path``, and
    ``session_path`` for the current OS, or *None* if the OS is unsupported.

    Tries the config file first; falls back to well-known default locations so
    the tool still works even when no config.ini exists.
    """
    # --- try config file first ---
    try:
        from config import get_config  # type: ignore
        cfg = get_config()
        if cfg:
            system = platform.system()
            section_map = {
                "Windows": "WindowsPaths",
                "Darwin": "MacPaths",
                "Linux": "LinuxPaths",
            }
            section = section_map.get(system)
            if section and cfg.has_section(section):
                session_defaults = {
                    "Windows": os.path.join(
                        os.getenv("APPDATA", ""), "Cursor", "Session Storage"
                    ),
                    "Darwin": os.path.expanduser(
                        "~/Library/Application Support/Cursor/Session Storage"
                    ),
                    "Linux": os.path.expanduser(
                        "~/.config/Cursor/Session Storage"
                    ),
                }
                return {
                    "storage_path": cfg.get(section, "storage_path"),
                    "sqlite_path": cfg.get(section, "sqlite_path"),
                    "session_path": session_defaults.get(system, ""),
                }
    except Exception as exc:
        logger.debug("Config file unavailable, using defaults: %s", exc)

    # --- fall back to well-known paths ---
    system = platform.system()
    if system == "Windows":
        appdata = os.getenv("APPDATA", "")
        base = os.path.join(appdata, "Cursor", "User")
        return {
            "storage_path": os.path.join(base, "globalStorage", "storage.json"),
            "sqlite_path": os.path.join(base, "globalStorage", "state.vscdb"),
            "session_path": os.path.join(appdata, "Cursor", "Session Storage"),
        }
    elif system == "Darwin":
        base = os.path.expanduser(
            "~/Library/Application Support/Cursor/User"
        )
        return {
            "storage_path": os.path.join(base, "globalStorage", "storage.json"),
            "sqlite_path": os.path.join(base, "globalStorage", "state.vscdb"),
            "session_path": os.path.expanduser(
                "~/Library/Application Support/Cursor/Session Storage"
            ),
        }
    elif system == "Linux":
        base = os.path.expanduser("~/.config/Cursor/User")
        return {
            "storage_path": os.path.join(base, "globalStorage", "storage.json"),
            "sqlite_path": os.path.join(base, "globalStorage", "state.vscdb"),
            "session_path": os.path.expanduser(
                "~/.config/Cursor/Session Storage"
            ),
        }

    logger.error("Unsupported platform: %s", system)
    return None


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

def _get_proxy() -> Optional[Dict[str, str]]:
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    return {"http": proxy, "https": proxy} if proxy else None


def get_token_from_storage(storage_path: str) -> Optional[str]:
    """Extract auth token from storage.json."""
    if not os.path.isfile(storage_path):
        return None
    try:
        with open(storage_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Preferred key
        token = data.get("cursorAuth/accessToken")
        if token and isinstance(token, str) and len(token) > 20:
            return token
        # Fallback: any key whose name contains "token" with a long string value
        for key, value in data.items():
            if "token" in key.lower() and isinstance(value, str) and len(value) > 20:
                return value
    except Exception as exc:
        logger.error("get_token_from_storage failed: %s", exc)
    return None


def get_token_from_sqlite(sqlite_path: str) -> Optional[str]:
    """Extract auth token from the VS Code / Cursor SQLite database."""
    if not os.path.isfile(sqlite_path):
        return None
    conn = None
    try:
        conn = sqlite3.connect(sqlite_path)
        cur = conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key LIKE '%token%'")
        for (value,) in cur.fetchall():
            if isinstance(value, str):
                if len(value) > 20 and "{" not in value:
                    return value
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        tok = parsed.get("token") or parsed.get("accessToken")
                        if tok and isinstance(tok, str) and len(tok) > 20:
                            return tok
                except json.JSONDecodeError:
                    pass
    except Exception as exc:
        logger.error("get_token_from_sqlite failed: %s", exc)
    finally:
        if conn:
            conn.close()
    return None


def get_token_from_session(session_path: str) -> Optional[str]:
    """Scan Chromium-style session log files for a token."""
    if not os.path.isdir(session_path):
        return None
    try:
        for fname in os.listdir(session_path):
            if not fname.endswith(".log"):
                continue
            fpath = os.path.join(session_path, fname)
            try:
                with open(fpath, "rb") as fh:
                    content = fh.read().decode("utf-8", errors="ignore")
                match = re.search(r'"token"\s*:\s*"([^"]{20,})"', content)
                if match:
                    return match.group(1)
            except Exception:
                continue
    except Exception as exc:
        logger.error("get_token_from_session failed: %s", exc)
    return None


def get_token() -> Optional[str]:
    """Try all known token sources in priority order."""
    paths = get_platform_paths()
    if not paths:
        return None
    return (
        get_token_from_storage(paths["storage_path"])
        or get_token_from_sqlite(paths["sqlite_path"])
        or get_token_from_session(paths["session_path"])
    )


# ---------------------------------------------------------------------------
# Email extraction
# ---------------------------------------------------------------------------

def get_email_from_storage(storage_path: str) -> Optional[str]:
    if not os.path.isfile(storage_path):
        return None
    try:
        with open(storage_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        email = data.get("cursorAuth/cachedEmail")
        if email and "@" in email:
            return email
        for key, value in data.items():
            if "email" in key.lower() and isinstance(value, str) and "@" in value:
                return value
    except Exception as exc:
        logger.error("get_email_from_storage failed: %s", exc)
    return None


def get_email_from_sqlite(sqlite_path: str) -> Optional[str]:
    if not os.path.isfile(sqlite_path):
        return None
    conn = None
    try:
        conn = sqlite3.connect(sqlite_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM ItemTable WHERE key LIKE '%email%' OR key LIKE '%cursorAuth%'"
        )
        for (value,) in cur.fetchall():
            if isinstance(value, str):
                if "@" in value and len(value) < 254:
                    return value
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        email = parsed.get("email") or parsed.get("cachedEmail")
                        if email and "@" in email:
                            return email
                except json.JSONDecodeError:
                    pass
    except Exception as exc:
        logger.error("get_email_from_sqlite failed: %s", exc)
    finally:
        if conn:
            conn.close()
    return None


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def get_usage(token: str) -> Optional[Dict]:
    """Fetch request-usage statistics from the Cursor API."""
    url = f"https://www.{Config.NAME_LOWER}.com/api/usage"
    headers = {
        **Config.BASE_HEADERS,
        "Cookie": (
            f"Workos{Config.NAME_CAPITALIZE}SessionToken="
            f"user_01OOOOOOOOOOOOOOOOOOOOOOOO%3A%3A{token}"
        ),
    }
    try:
        resp = requests.get(
            url, headers=headers, timeout=Config.REQUEST_TIMEOUT, proxies=_get_proxy()
        )
        resp.raise_for_status()
        data = resp.json()

        gpt4 = data.get("gpt-4", {})
        gpt35 = data.get("gpt-3.5-turbo", {})

        premium_usage = gpt4.get("numRequestsTotal") or 0
        max_premium = gpt4.get("maxRequestUsage")          # None → unlimited
        basic_usage = gpt35.get("numRequestsTotal") or 0

        return {
            "premium_usage": premium_usage,
            "max_premium_usage": max_premium if max_premium is not None else "No Limit",
            "basic_usage": basic_usage,
            "max_basic_usage": "No Limit",
        }
    except requests.HTTPError as exc:
        logger.error("get_usage HTTP error %s: %s", exc.response.status_code, exc)
    except Exception as exc:
        logger.error("get_usage failed: %s", exc)
    return None


def get_stripe_profile(token: str) -> Optional[Dict]:
    """Fetch full Stripe subscription profile."""
    url = f"https://api2.{Config.NAME_LOWER}.sh/auth/full_stripe_profile"
    headers = {**Config.BASE_HEADERS, "Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(
            url, headers=headers, timeout=Config.REQUEST_TIMEOUT, proxies=_get_proxy()
        )
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        logger.error("get_stripe_profile HTTP error %s: %s", exc.response.status_code, exc)
    except Exception as exc:
        logger.error("get_stripe_profile failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_subscription_type(data: Optional[Dict]) -> str:
    """Return a human-readable subscription label from the Stripe profile."""
    if not data:
        return "Free"

    # Modern response shape
    membership = (data.get("membershipType") or "").lower()
    status = (data.get("subscriptionStatus") or "").lower()
    if membership or status:
        label_map = {
            "pro": "Pro",
            "free_trial": "Free Trial",
            "pro_trial": "Pro Trial",
            "team": "Team",
            "enterprise": "Enterprise",
        }
        if status == "active":
            return label_map.get(membership, membership.capitalize() or "Active Subscription")
        if membership:
            suffix = f" ({status})" if status else ""
            return label_map.get(membership, membership.capitalize()) + suffix

    # Legacy response shape
    subscription = data.get("subscription")
    if subscription:
        plan_name = (subscription.get("plan") or {}).get("nickname", "Unknown")
        sub_status = subscription.get("status", "unknown")
        plan_lower = plan_name.lower()
        if sub_status == "active":
            for keyword, label in [
                ("enterprise", "Enterprise"),
                ("team", "Team"),
                ("pro_trial", "Pro Trial"),
                ("free_trial", "Free Trial"),
                ("pro", "Pro"),
            ]:
                if keyword in plan_lower:
                    return label
            return plan_name
        return f"{plan_name} ({sub_status})"

    return "Free"


def _display_width(text: str) -> int:
    """
    Return the terminal display width of *text*, stripping ANSI codes and
    counting CJK / emoji characters as width 2.
    """
    clean = _ANSI_ESCAPE.sub("", text)
    width = 0
    for ch in clean:
        width += 2 if ord(ch) > 127 else 1
    return width


def _format_usage_line(label_color: str, emoji_key: str, label: str,
                       used: int, limit, translator) -> str:
    """
    Build a coloured usage line such as:
        ⭐ Fast Response: 42/500 (8.4%)
    """
    if isinstance(limit, str) and limit == "No Limit":
        value_color = Fore.GREEN
        display = f"{used}/{limit}"
    else:
        limit = limit if (limit and limit > 0) else 999
        pct = (used / limit) * 100
        value_color = Fore.GREEN
        if pct > 90:
            value_color = Fore.RED
        elif pct > 70:
            value_color = Fore.YELLOW
        display = f"{used}/{limit} ({pct:.1f}%)"

    return (
        f"{label_color}{EMOJI[emoji_key]} {label}: "
        f"{value_color}{display}{Style.RESET_ALL}"
    )


# ---------------------------------------------------------------------------
# Main display function
# ---------------------------------------------------------------------------

def display_account_info(translator=None) -> None:
    """Print a neatly formatted account & usage panel to stdout."""

    def t(key: str, fallback: str) -> str:
        """Safe translator lookup with a hard-coded fallback."""
        if translator:
            try:
                return translator.get(key) or fallback
            except Exception:
                pass
        return fallback

    sep = f"{Fore.CYAN}{'─' * 70}{Style.RESET_ALL}"
    print(f"\n{sep}")
    print(f"{Fore.CYAN}{EMOJI['USER']} {t('account_info.title', 'Cursor Account Information')}{Style.RESET_ALL}")
    print(sep)

    # --- token ---
    token = get_token()
    if not token:
        print(
            f"{Fore.RED}{EMOJI['ERROR']} "
            f"{t('account_info.token_not_found', 'Token not found. Please login to Cursor first.')}"
            f"{Style.RESET_ALL}"
        )
        print(sep)
        return

    paths = get_platform_paths()
    if not paths:
        print(
            f"{Fore.RED}{EMOJI['ERROR']} "
            f"{t('account_info.config_not_found', 'Configuration not found.')}"
            f"{Style.RESET_ALL}"
        )
        print(sep)
        return

    # --- email ---
    email = (
        get_email_from_storage(paths["storage_path"])
        or get_email_from_sqlite(paths["sqlite_path"])
    )

    # --- subscription ---
    subscription_info = get_stripe_profile(token)

    # Last-chance email from Stripe profile
    if not email and subscription_info:
        customer = subscription_info.get("customer") or {}
        email = customer.get("email")

    # --- usage ---
    usage_info = get_usage(token)

    # ── Build left column (account info) ────────────────────────────────────
    left: list[str] = []

    if email:
        left.append(
            f"{Fore.GREEN}{EMOJI['USER']} "
            f"{t('account_info.email', 'Email')}: "
            f"{Fore.WHITE}{email}{Style.RESET_ALL}"
        )
    else:
        left.append(
            f"{Fore.YELLOW}{EMOJI['WARNING']} "
            f"{t('account_info.email_not_found', 'Email not found')}"
            f"{Style.RESET_ALL}"
        )

    if subscription_info:
        sub_type = format_subscription_type(subscription_info)
        left.append(
            f"{Fore.GREEN}{EMOJI['SUBSCRIPTION']} "
            f"{t('account_info.subscription', 'Subscription')}: "
            f"{Fore.WHITE}{sub_type}{Style.RESET_ALL}"
        )
        days_remaining = subscription_info.get("daysRemainingOnTrial")
        if days_remaining is not None and int(days_remaining) > 0:
            left.append(
                f"{Fore.GREEN}{EMOJI['TIME']} "
                f"{t('account_info.trial_remaining', 'Remaining Pro Trial')}: "
                f"{Fore.WHITE}{days_remaining} "
                f"{t('account_info.days', 'days')}{Style.RESET_ALL}"
            )
    else:
        left.append(
            f"{Fore.YELLOW}{EMOJI['WARNING']} "
            f"{t('account_info.subscription_not_found', 'Subscription information not found')}"
            f"{Style.RESET_ALL}"
        )

    # ── Build right column (usage info) ─────────────────────────────────────
    right: list[str] = []

    if usage_info:
        right.append(
            f"{Fore.GREEN}{EMOJI['USAGE']} "
            f"{t('account_info.usage', 'Usage Statistics')}:"
            f"{Style.RESET_ALL}"
        )
        right.append(
            _format_usage_line(
                Fore.YELLOW, "PREMIUM",
                t("account_info.premium_usage", "Fast Response"),
                usage_info["premium_usage"],
                usage_info["max_premium_usage"],
                translator,
            )
        )
        right.append(
            _format_usage_line(
                Fore.BLUE, "BASIC",
                t("account_info.basic_usage", "Slow Response"),
                usage_info["basic_usage"],
                usage_info["max_basic_usage"],
                translator,
            )
        )

    # ── Print columns side by side ───────────────────────────────────────────
    max_left_w = max((_display_width(s) for s in left), default=0)
    padding = 4
    right_start = max_left_w + padding

    rows = max(len(left), len(right))
    for i in range(rows):
        left_str = left[i] if i < len(left) else ""
        right_str = right[i] if i < len(right) else ""
        gap = right_start - _display_width(left_str)
        print(left_str + " " * gap + right_str)

    print(sep)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(translator=None) -> None:
    try:
        display_account_info(translator)
    except Exception as exc:
        print(
            f"{Fore.RED}{EMOJI['ERROR']} "
            f"{'Error' if not translator else translator.get('account_info.error', 'Error')}"
            f": {exc}{Style.RESET_ALL}"
        )


if __name__ == "__main__":
    main()
