#!/usr/bin/env python3
"""
Pressable End-to-End Migration Automation (Bulk)

Processes a list of origin sites (ORIGIN_URLS) one after another.

What it does, per site:
1. Creates the site on Pressable via the API (if enabled), then fetches the
   new site's staging URL, IPs and SFTP credentials (forcing an SFTP password
   reset so the password can be shown).
2. Uses Playwright (with stealth) to request a WP password reset on the
   origin site for TARGET_EMAIL.
3. Pauses for you to paste the reset link from your email, sets a new
   password (a different one for every site), logs in, optionally turns on
   maintenance mode, installs the Pressable migration plugin and starts the
   migration.

At the end of the run a summary lists every site's details, its new WP
password and what happened to it, so nothing has to be picked out of the
per-site output.

Set SITE_CREATION_ONLY = True to stop after step 1: the sites are created and
their details printed, but no browser is opened and no password resets or
logins are attempted on the origin sites.

Because step 3 needs a human to check email and paste a reset link, this is
still a sequential, hands-on process for each site — it just saves you from
re-running the script and re-entering the config for every URL.
"""

import time
import re
import random
import string
import asyncio
import tempfile
import socket
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

try:
    import tldextract
    HAVE_TLDEXTRACT = True
except ImportError:
    HAVE_TLDEXTRACT = False

# ==========================================
# ---[ CONFIGURATION SETTINGS ]---
# Hardcode your variables here before running.
# Never commit real credentials to the repo.
# ==========================================
CLIENT_ID = ""      # Mandatory: Pressable API client ID
CLIENT_SECRET = ""  # Mandatory: Pressable API client secret

# Origin sites to migrate, one per line, processed in order.
# Each line can optionally say where the site's login page is:
#   example.com                                  -> CUSTOM_LOGIN_URL below, else /wp-login.php
#   example.com/wp-admin/                        -> a path on the URL is used as the login page
#   example.com /my-login/                       -> a second column gives the login path...
#   example.com https://example.com/my-login/    -> ...or the full login URL
ORIGIN_URLS = """
"""

# Datacenter code to create every site in. Must be one of VALID_DATACENTERS below.
# Leave blank ("") to be asked which datacenter to use for EACH site as it is created.
DATACENTER = ""

# Datacenter codes Pressable accepts for site creation (code: location).
# If Pressable adds a new datacenter, add it here.
VALID_DATACENTERS = {
    "AMS": "Amsterdam, NL",
    "BUR": "Los Angeles, CA, USA",
    "DCA": "Washington, DC, USA",
    "DFW": "Dallas, TX, USA",
}

# Set a custom name for the Pressable site.
# If left blank (""), it will automatically extract the name from each origin URL.
CUSTOM_SITE_NAME = ""

# FALLBACK: login URL or path used for any site in ORIGIN_URLS that doesn't give
# its own. Can be a full URL ("https://example.com/my-login/") or just a path
# ("/my-login/"), which is added to each site's own address.
CUSTOM_LOGIN_URL = ""

# Basic HTTP Auth (Used ONLY if the server prompts for it)
BASIC_AUTH_USER = ""
BASIC_AUTH_PASS = ""

# WP Admin account on the origin sites that the password reset and login use.
TARGET_EMAIL = "migrations@pressable.com"

# Email address entered in the Pressable migration plugin (receives migration updates).
MIGRATION_EMAIL = ""

# If set, this single password is used to log in to WP Admin on EVERY site in
# ORIGIN_URLS, and the password reset steps are skipped entirely. Leave blank ("")
# to go through the password reset flow instead.
GLOBAL_WP_ADMIN_PASSWORD = ""

# If True, you're asked per site whether to turn on maintenance mode, which
# WP Admin email to use, and whether you already have a working password (or
# want to skip the site). If False, no per-site questions are asked: every site
# uses ENABLE_MAINTENANCE_MODE and TARGET_EMAIL and goes straight to the
# password reset flow.
VERBOSE_QUESTIONS = False

# Whether to turn on maintenance mode on the origin site before migrating, when
# VERBOSE_QUESTIONS is False. Installs and activates the "maintenance" plugin.
ENABLE_MAINTENANCE_MODE = False

# ==========================================
# ---[ EXECUTION PHASES ]---
# Toggle these booleans to run specific parts
# ==========================================

DO_PHASE_1_CREATE_SITE = True         # Create site (False skips creation but STILL gathers info)
DO_PHASE_2_REQUEST_RESET = True       # Trigger the origin password reset link
DO_PHASE_3_PROCESS_MIGRATION = True   # Input reset link, login, and start migration

# If True, the script ONLY creates the sites and prints their details
# (staging URL, IPs, SFTP username/password), then the summary. Phases 2 and 3
# are forced off regardless of the toggles above — no browser is opened, no
# password prompts are shown, and no password resets or logins are attempted
# on the origin sites.
SITE_CREATION_ONLY = False

# Enforce SITE_CREATION_ONLY by switching off the browser phases here, so the
# rest of the script doesn't need to check both settings everywhere.
if SITE_CREATION_ONLY:
    DO_PHASE_2_REQUEST_RESET = False
    DO_PHASE_3_PROCESS_MIGRATION = False


@dataclass
class Settings:
    """The per-run settings from the configuration above, cleaned up and
    gathered into one object so they can be passed around together."""
    datacenter: str
    target_email: str
    migration_email: str
    custom_site_name: str
    custom_login_url: str
    global_wp_admin_password: str
    verbose_questions: bool
    enable_maintenance_mode: bool


# ---[ PRESSABLE API HELPER FUNCTIONS ]---

API_BASE = "https://my.pressable.com"
V1 = f"{API_BASE}/v1"

def generate_password() -> str:
    """Generate a fresh random password. Called per site so no two sites
    in a bulk run ever share a password."""
    return "".join(secrets.choice(string.ascii_letters + string.digits + "!@#$%^&*_-") for _ in range(16))

def extract_hostname(domain_or_url: str) -> str:
    d = domain_or_url.strip().lower()
    if not d: return ""
    if "://" not in d: parsed = urlparse(f"http://{d}")
    else: parsed = urlparse(d)
    hostname = parsed.netloc.split(":")[0] if parsed.netloc else parsed.path.split("/")[0]
    return hostname or d

def base_label_from_domain(domain_or_url: str) -> str:
    hostname = extract_hostname(domain_or_url)
    if not hostname: return ""

    # Origin sites already hosted on Pressable staging (e.g. "mysite.mystagingwebsite.com")
    # would otherwise produce a name like "mysitemystagingwebsite", which then doubles up as
    # "mysitemystagingwebsite.mystagingwebsite.com". Use only the part before the staging
    # domain instead, joining any nested subdomains together (e.g. "a.b" -> "ab").
    staging_suffix = ".mystagingwebsite.com"
    if hostname.endswith(staging_suffix):
        prefix = hostname[:-len(staging_suffix)]
        if prefix.startswith("www."): prefix = prefix[len("www."):]
        prefix = prefix.replace(".", "")
        if prefix: return prefix

    if HAVE_TLDEXTRACT:
        ext = tldextract.extract(hostname)
        if ext.subdomain and ext.subdomain.lower() != "www": return f"{ext.subdomain}{ext.domain}"
        return ext.domain
    parts = [p for p in hostname.split(".") if p]
    if len(parts) >= 3:
        if parts[-3].lower() == "www": return parts[-2]
        return parts[-3] + parts[-2]
    elif len(parts) >= 2: return parts[-2]
    return parts[0] if parts else ""

def slugify_pressable_name(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    s = s.strip("-")
    if len(s) < 4:
        s = (s + "".join(random.choice(string.digits) for _ in range(4 - len(s)))) or "site0"
    return s

def get_access_token(client_id: str, client_secret: str) -> str:
    url = f"{API_BASE}/auth/token"
    resp = requests.post(
        url,
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

def validate_name(token: str, name: str):
    url = f"{V1}/sites/validate"
    resp = requests.post(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json={"name": name}, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    return bool(payload.get("data", {}).get("valid"))

def next_available_name(token: str, base: str) -> str:
    candidate = slugify_pressable_name(base)
    if candidate.startswith("-") or candidate.endswith("-"): candidate = candidate.strip("-") or "site0"
    if validate_name(token, candidate): return candidate
    for i in range(1, 10000):
        trial = f"{candidate}-{i}"
        if validate_name(token, trial): return trial
    raise RuntimeError(f"Could not find an available name starting from '{base}'")

def create_site(token: str, name: str, datacenter_code: str) -> dict:
    url = f"{V1}/sites"
    body = {"name": name, "datacenter_code": datacenter_code}
    resp = requests.post(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json=body, timeout=120)
    if not resp.ok:
        # Show the API's own explanation (e.g. plan limit reached) before raising
        print(f"  ❌ Site creation failed. API response: {resp.text}")
    resp.raise_for_status()
    return resp.json()

def search_site(token: str, clean_hostname: str, name: str) -> dict:
    url = f"{V1}/sites/search"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Try by domain first
    resp = requests.post(url, headers=headers, json={"filters": [{"key": "domain", "value": clean_hostname}], "per_page": 1, "page": 1}, timeout=60)
    data = resp.json().get("data") or []
    if data: return data[0]

    # Fallback to name
    resp = requests.post(url, headers=headers, json={"filters": [{"key": "name", "value": name}], "per_page": 1, "page": 1}, timeout=60)
    data = resp.json().get("data") or []
    return data[0] if data else None

def get_site_with_ips(token: str, clean_hostname: str, name: str, max_retries: int = 20, delay: int = 5):
    """Poll search_site until the returned record has its ip_addresses populated.
    A freshly created site can take time before the API reports allocated IPs —
    checking immediately after creation was returning an empty list, which caused
    callers to fall back to a DNS lookup that resolves to a shared front-end IP
    pair instead of the site's actual dedicated IPs."""
    site = None
    for attempt in range(max_retries):
        site = search_site(token, clean_hostname, name)
        if site and site.get("ipAddresses"):
            return site
        print(f"  ⏳ Site IP addresses not yet allocated. Waiting {delay} seconds... (Attempt {attempt + 1}/{max_retries})")
        time.sleep(delay)
    return site

def get_sftp_credentials(token: str, site_id: str, max_retries: int = 10, delay: int = 10):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    list_url = f"{V1}/sites/{site_id}/ftp"

    for attempt in range(max_retries):
        resp = requests.get(list_url, headers=headers, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"API Error fetching SFTP users: {resp.text}")

        data = resp.json().get("data")

        # If the user exists, proceed with resetting the password
        if data and isinstance(data, list) and len(data) > 0:
            sftp_user = data[0].get("username")

            reset_url = f"{V1}/sites/{site_id}/ftp/password/{sftp_user}"
            reset_resp = requests.post(reset_url, headers=headers, timeout=60)
            if reset_resp.status_code != 200:
                raise RuntimeError(f"API Error resetting password: {reset_resp.text}")

            # Guard against a 200 response with no password in it, which would
            # otherwise be shown (and used in the migration) as the text "None"
            sftp_password = reset_resp.json().get("data")
            if not sftp_password:
                raise RuntimeError(f"SFTP password reset for '{sftp_user}' returned no password: {reset_resp.text}")
            return sftp_user, str(sftp_password)

        # If the user doesn't exist yet, wait and try again
        print(f"  ⏳ SFTP user not provisioned yet. Waiting {delay} seconds... (Attempt {attempt + 1}/{max_retries})")
        time.sleep(delay)

    # If the loop exhausts all retries, then we throw the error
    raise RuntimeError(f"No SFTP users exist for site {site_id} after waiting {max_retries * delay} seconds.")


# ---[ URL PARSING HELPERS ]---

def _build_login_url(base_url: str, login: str) -> str:
    """Turn a login URL or path into a full URL. Full URLs are used as-is;
    paths (with or without a leading slash) are added to base_url."""
    if login.startswith(("http://", "https://")):
        return login
    path = login if login.startswith("/") else f"/{login}"
    return f"{base_url}{path}"

def parse_origin_line(line: str, global_custom_login: str) -> tuple[str, str]:
    """Split one ORIGIN_URLS line into (base_url, wp_login_url).

    The login page is chosen in this order:
    1. A second column on the line (a full URL or a path)
    2. A path on the origin URL itself (e.g. example.com/wp-admin/)
    3. The global CUSTOM_LOGIN_URL
    4. The WordPress default, /wp-login.php

    Returns ("", "") if no usable URL can be read from the line."""
    parts = line.strip().split()
    if not parts:
        return "", ""

    raw_origin = parts[0]
    full_origin = raw_origin if "://" in raw_origin else f"https://{raw_origin}"
    parsed = urlparse(full_origin)
    if not parsed.netloc:
        return "", ""
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    if len(parts) >= 2:
        return base_url, _build_login_url(base_url, parts[1])
    if parsed.path not in ("", "/"):
        return base_url, full_origin
    if global_custom_login:
        return base_url, _build_login_url(base_url, global_custom_login)
    return base_url, f"{base_url}/wp-login.php"


# ---[ PLAYWRIGHT AUTOMATION FUNCTIONS ]---

async def goto_with_retries(page, url, retries=2, **goto_kwargs):
    """Navigate to url, retrying on failure (e.g. a timeout) up to `retries`
    additional times before giving up, so `retries + 1` attempts total."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            await page.goto(url, **goto_kwargs)
            return
        except Exception as e:
            last_error = e
            if attempt < retries:
                print(f"  ⚠️ Navigation to {url} failed ({str(e)[:80]}), retrying ({attempt + 1}/{retries})...")
    raise last_error

async def click_magic_link_fallback(page):
    """iThemes / Solid Security can replace the login form with a "magic link"
    form. If its fallback link is showing, click it to get the standard form."""
    fallback_selector = "a.itsec-pwls-login-fallback__link"
    if await page.is_visible(fallback_selector):
        print("  ℹ️ Magic link fallback detected. Clicking to reveal the standard form...")
        await page.click(fallback_selector)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)

class UserNotFoundError(Exception):
    """Raised when the origin site's lost-password form reports that no
    account exists for the target email — retrying, or asking for a manually
    requested reset link, is pointless in that case."""
    pass


NO_ACCOUNT_PHRASES = (
    "invalid username or email",
    "invalid username",
    "invalid email",
    "unknown email",
    "no account with that email",
    "is not registered on this site",
    "user not found",
)


async def _attempt_password_reset_request(page, wp_login_url, target_email):
    """A single attempt at requesting a password reset. Raises on failure —
    callers are expected to retry via request_password_reset(). Raises
    UserNotFoundError specifically when the site reports no such account,
    since that's not something a retry (or a manual link) can fix."""
    await page.goto(wp_login_url, wait_until="networkidle", timeout=15000)
    await asyncio.sleep(1.5) # Let security scripts settle

    await click_magic_link_fallback(page)

    if "action=lostpassword" not in page.url:
        lost_password_selector = "a[href*='action=lostpassword']"
        await page.wait_for_selector(lost_password_selector, state="visible", timeout=5000)
        await page.click(lost_password_selector)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1.5)

    await page.wait_for_selector("input[name='user_login']", state="visible", timeout=10000)
    await page.fill("input[name='user_login']", target_email)
    await asyncio.sleep(1) # Pause before submitting
    await page.click("#wp-submit")

    # Extended timeout to 20s to allow manual CAPTCHA clicking if it appears
    await page.wait_for_load_state("networkidle", timeout=20000)

    page_content = (await page.content()).lower()
    if any(phrase in page_content for phrase in NO_ACCOUNT_PHRASES):
        raise UserNotFoundError(f"No account found for {target_email}")


async def request_password_reset(page, wp_login_url, target_email, retries=2, retry_delay=5):
    """Requests a password reset on the origin site. Retries the whole attempt
    up to `retries` additional times (so `retries + 1` attempts total) — e.g. for
    a slow/flaky site, or the browser page having crashed mid-run (which raises
    "Target page, context or browser has been closed"). If the page turns out to
    be closed, a fresh page is opened in the same browser context before retrying;
    if the whole context is dead, retrying is pointless and we give up immediately.
    Likewise, if the site reports no account exists for target_email, we stop
    immediately rather than burning retries on something that can't succeed.
    Returns (status, page) where status is "success", "no_user", or "failed".
    `page` is the same one passed in unless it had to be replaced, so callers
    must keep using the returned page from here on."""
    print(f"\n🚀 [Playwright] Requesting password reset on: {wp_login_url}")

    for attempt in range(retries + 1):
        if page.is_closed():
            try:
                page = await page.context.new_page()
                print("  ℹ️ Page had been closed — opened a new page to continue.")
            except Exception as e:
                print(f"  ❌ Browser context is no longer usable ({str(e)[:80]}). Cannot retry.")
                return "failed", page

        try:
            await _attempt_password_reset_request(page, wp_login_url, target_email)
            print(f"  ✅ Password reset request submitted successfully to {target_email}.")
            return "success", page
        except UserNotFoundError:
            print(f"  ⚠️ No account exists for {target_email} on this site — moving on, retrying won't help.")
            return "no_user", page
        except Exception as e:
            print(f"  ❌ Error requesting reset (attempt {attempt + 1}/{retries + 1}): {str(e)[:100]}")
            if attempt < retries:
                print(f"  ⏳ Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)

    print(f"  ⚠️ Giving up after {retries + 1} attempts.")
    return "failed", page


async def reset_password_on_origin(page, reset_link, new_password):
    print(f"\n🚀 [Playwright] Processing Password Reset Link...")
    await page.goto(reset_link, wait_until="networkidle", timeout=15000)
    await asyncio.sleep(1.5)

    await page.wait_for_selector("input[name='pass1']", state="visible", timeout=10000)
    await page.fill("input[name='pass1']", new_password)

    weak_checkbox = "input[name='pw_weak']"
    if await page.is_visible(weak_checkbox):
        await page.check(weak_checkbox)
        await asyncio.sleep(0.5)

    await page.click("#wp-submit")
    await page.wait_for_load_state("networkidle", timeout=60000)
    print(f"  ✅ Password reset to new credentials successfully.")
    print(f"  🔑 New password: {new_password}")
    await asyncio.sleep(2) # Give the database a second to process the new hash


async def ensure_plugin_active(page, base_url, plugin_slug) -> bool:
    """Make sure a wordpress.org plugin is installed and active on the origin
    site, installing and/or activating it from the plugin search page as needed.
    Returns False (rather than raising) if the plugin can't be found, so the
    caller can decide whether that's fatal."""
    print(f"  ℹ️ Checking plugin: {plugin_slug}...")
    target_plugin_url = f"{base_url}/wp-admin/plugin-install.php?s={plugin_slug}&tab=search&type=term"
    await page.goto(target_plugin_url, wait_until="networkidle")
    await asyncio.sleep(1.5)

    plugin_card = f".plugin-card-{plugin_slug}"
    try:
        await page.wait_for_selector(plugin_card, state="visible", timeout=15000)
    except Exception as e:
        print(f"  ⚠️ Plugin {plugin_slug} not found in search results ({str(e)[:80]}).")
        return False

    install_btn = f"{plugin_card} a.install-now"
    activate_btn = f"{plugin_card} a.activate-now"

    if await page.is_visible(activate_btn):
        print(f"  ℹ️ {plugin_slug} is installed but inactive. Activating...")
        await page.click(activate_btn)
        await page.wait_for_load_state("networkidle")
    elif await page.is_visible(install_btn):
        print(f"  ℹ️ {plugin_slug} is not installed. Installing...")
        await page.click(install_btn)
        await page.wait_for_selector(activate_btn, state="visible", timeout=45000)
        await asyncio.sleep(1) # Pause between install and activation
        print(f"  ℹ️ {plugin_slug} installed. Activating...")
        await page.click(activate_btn)
        await page.wait_for_load_state("networkidle")
    else:
        print(f"  ✅ {plugin_slug} is already installed and active.")

    await asyncio.sleep(1.5)
    return True


async def turn_on_maintenance_mode(page, base_url) -> bool:
    """Install/activate the "maintenance" plugin and switch it on. Failure here
    is reported but not fatal — the migration can still go ahead.
    Returns True if maintenance mode was turned on."""
    print("🚀 [Playwright] Setting up Maintenance Mode...")
    if not await ensure_plugin_active(page, base_url, "maintenance"):
        print("  ⚠️ Maintenance plugin unavailable — continuing without maintenance mode.")
        return False

    await page.goto(f"{base_url}/wp-admin/admin.php?page=maintenance", wait_until="networkidle")
    await asyncio.sleep(1)
    try:
        # Tick the plugin's on/off checkbox directly, then save the settings
        await page.evaluate('document.querySelector("input#state").checked = true')
        await page.click("input#submit")
        await page.wait_for_load_state("networkidle")
        print("  ✅ Maintenance mode turned ON.")
        return True
    except Exception as e:
        print(f"  ⚠️ Could not turn on maintenance mode automatically ({str(e)[:80]}). Turn it on manually if needed.")
        return False


async def login_and_run_migration(page, password, target_email, migration_email, sftp_data,
                                  base_url, wp_login_url, enable_maintenance):
    """Log in to the origin site, optionally turn on maintenance mode, then
    install the Pressable migration plugin and submit the migration.
    Raises on any failure. Returns (migration_url, maintenance_on)."""
    print(f"🚀 [Playwright] Logging into {wp_login_url} as {target_email}...")
    await goto_with_retries(page, wp_login_url, retries=2, wait_until="networkidle", timeout=15000)
    await asyncio.sleep(1.5)

    await click_magic_link_fallback(page)

    await page.wait_for_selector("input[name='log']", state="visible", timeout=10000)
    await page.fill("input[name='log']", target_email)
    await page.fill("input[name='pwd']", password)
    await asyncio.sleep(1)
    await page.click("#wp-submit")

    await page.wait_for_load_state("networkidle", timeout=60000)
    await asyncio.sleep(1.5)

    # Check for Yoast SEO
    page_content = await page.content()
    if "yoast" in page_content.lower():
        print("  ✅ Yoast SEO plugin detected on the dashboard.")
    else:
        print("  ℹ️ Yoast SEO plugin not found on the dashboard.")

    maintenance_on = False
    if enable_maintenance:
        maintenance_on = await turn_on_maintenance_mode(page, base_url)

    # Install & activate the migration plugin — without it nothing else can happen
    print("🚀 [Playwright] Setting up Pressable Migration Plugin...")
    if not await ensure_plugin_active(page, base_url, "pressable-automated-migration"):
        raise RuntimeError("Could not install/activate the pressable-automated-migration plugin.")

    print(f"🚀 [Playwright] Navigating to the migration page...")
    await page.goto(f"{base_url}/wp-admin/admin.php?page=pressable", wait_until="networkidle")
    await asyncio.sleep(1.5)

    # Fill Migration Form
    print(f"🚀 [Playwright] Submitting Migration Details...")
    await page.wait_for_selector("input[name='email']", state="visible", timeout=20000)
    await page.fill("input[name='email']", migration_email)
    await page.check("input[name='consent']")

    await page.click("#migratesubmit")
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(1.5)

    await page.wait_for_selector("input[name='newurl']", state="visible", timeout=20000)

    staging = sftp_data['staging']
    formatted_staging_url = staging if staging.startswith("http") else f"https://{staging}/"

    await page.fill("input[name='newurl']", formatted_staging_url)
    await page.fill("input[name='username']", sftp_data['sftp_user'])
    await page.fill("input[name='passwd']", sftp_data['sftp_password'])
    await asyncio.sleep(1)

    await page.click("input[type='submit'][value='Migrate']")
    await page.wait_for_load_state("networkidle")
    print(f"  ✅ Complete migration initiated to {formatted_staging_url}!")
    # Show the page the plugin lands on after submitting, so it can be checked
    # per site before the bulk loop moves on to the next one.
    print(f"  🔗 Migration URL: {page.url}")
    return page.url, maintenance_on


async def start_migration(page, password, target_email, migration_email, sftp_data,
                          base_url, wp_login_url, enable_maintenance, record):
    """Run login_and_run_migration(), catching any error so one failed site
    doesn't stop the bulk run, and record the outcome in the site's summary."""
    try:
        migration_url, maintenance_on = await login_and_run_migration(
            page, password, target_email, migration_email, sftp_data,
            base_url, wp_login_url, enable_maintenance
        )
        record["status"] = "Migration started"
        record["migration_url"] = migration_url
        if enable_maintenance:
            record["maintenance"] = "ON" if maintenance_on else "FAILED — check manually"
    except Exception as e:
        print(f"  ❌ Error during login and migration sequence: {str(e)[:100]}")
        record["status"] = f"Failed during login/migration: {str(e)[:100]}"


# ---[ PROMPT HELPERS ]---

def prompt_for_datacenter(origin_url: str) -> str:
    """Interactively ask which datacenter to create the site in.

    Used when DATACENTER is left blank. Shows a numbered list of the codes in
    VALID_DATACENTERS and accepts either the number or the code itself
    (case-insensitive). Keeps asking until a valid answer is given."""
    codes = list(VALID_DATACENTERS.keys())
    print(f"\nWhich datacenter should the site for {origin_url} be created in?")
    for number, code in enumerate(codes, start=1):
        print(f"  {number}. {code} - {VALID_DATACENTERS[code]}")

    while True:
        answer = input(" Enter a number or datacenter code: ").strip().upper()

        # Accept the list number (e.g. "2")...
        if answer.isdigit() and 1 <= int(answer) <= len(codes):
            return codes[int(answer) - 1]
        # ...or the code itself (e.g. "BUR")
        if answer in VALID_DATACENTERS:
            return answer

        print(f" ⚠️ '{answer}' is not a valid choice. Please enter 1-{len(codes)} or one of: {', '.join(codes)}")


def prompt_for_sftp_details():
    """Ask for the target site's details by hand, for when they couldn't be
    gathered from the API. Returns None if the staging URL is left blank,
    meaning the site should be skipped."""
    print("⚠️ WARNING: No SFTP credentials were gathered from the API.")
    staging = input(" Enter Target Staging URL (or press Enter to skip this site): ").strip()
    if not staging:
        return None
    return {
        "staging": staging,
        "sftp_user": input(" Enter SFTP Username: ").strip(),
        "sftp_password": input(" Enter SFTP Password: ").strip(),
    }


# ---[ SUMMARY HELPERS ]---

# Summary fields in the order they're printed: (dictionary key, label)
SUMMARY_FIELDS = [
    ("status", "Status"),
    ("staging", "Staging URL"),
    ("ips", "IPs"),
    ("sftp_user", "SFTP Username"),
    ("sftp_password", "SFTP Password"),
    ("wp_password", "New WP Password"),
    ("maintenance", "Maintenance"),
    ("migration_url", "Migration URL"),
]

def print_site_summary(site_summary: list):
    """Print every site's details together at the end of the run, so they
    don't have to be picked out of the per-site output.

    Each entry in site_summary is a dict with an "origin" and a "status" key,
    plus whichever of the other SUMMARY_FIELDS were gathered for that site."""
    print("\n==================== SUMMARY ====================")
    if not site_summary:
        print("No sites were processed.")
        return

    for entry in site_summary:
        print(f"\nOrigin:          {entry['origin']}")
        for key, label in SUMMARY_FIELDS:
            if entry.get(key):
                print(f"{label + ':':<17}{entry[key]}")

    failed = sum(1 for entry in site_summary if entry["status"].startswith("Failed"))
    skipped = sum(1 for entry in site_summary if entry["status"].startswith("Skipped"))
    print(f"\n{len(site_summary) - failed - skipped} OK, {skipped} skipped, {failed} failed.")
    print("=================================================")


# ---[ PER-SITE ORCHESTRATION ]---

async def process_single_site(token, origin_url, wp_login_url, page, settings, record):
    """Runs Phases 1-3 for a single origin site. `page` is the shared
    Playwright page reused across every site in the bulk run (None if no
    browser automation phases are enabled). Details and the outcome are
    stored in `record` (this site's entry in the end-of-run summary).
    Returns the page to use for the next site — normally the same one passed
    in, but a fresh page if the original had to be recreated after the
    browser crashed mid-run."""

    clean_hostname = extract_hostname(origin_url)
    base_name = settings.custom_site_name if settings.custom_site_name else base_label_from_domain(origin_url)
    browser_phases = DO_PHASE_2_REQUEST_RESET or DO_PHASE_3_PROCESS_MIGRATION

    # ---[ Per-site settings: asked here (if VERBOSE_QUESTIONS) so you can answer up front ]---
    target_email = settings.target_email
    enable_maintenance = settings.enable_maintenance_mode
    if browser_phases:
        if settings.verbose_questions:
            print("\n---[ Per-site Questions ]---")
            answer = input(f" Enable maintenance mode on {origin_url} before migrating? [y/N]: ").strip().lower()
            enable_maintenance = answer == "y"
            answer = input(f" WP Admin email to use on {origin_url} (press Enter for '{target_email}'): ").strip()
            target_email = answer or target_email
        print(f"  ℹ️ Login page: {wp_login_url}")
        print(f"  ℹ️ WP Admin email: {target_email}")
        print(f"  ℹ️ Maintenance mode: {'ON' if enable_maintenance else 'off'}")

    sftp_data = None

    # Handle Creation vs. Skip Creation
    if DO_PHASE_1_CREATE_SITE:
        print("\n---[ PHASE 1: Pressable API Site Creation ]---")
        desired_name = next_available_name(token, base_name)
        # No datacenter configured, so ask which one to use for this site
        datacenter = settings.datacenter or prompt_for_datacenter(origin_url)
        print(f"Creating site '{desired_name}' in Datacenter '{datacenter}'...")
        create_site(token, desired_name, datacenter)

        print("Site creation initiated! Moving to info gathering...")
    else:
        print("\n---[ SKIPPED PHASE 1: API Site Creation ]---")
        print("Moving directly to site discovery...")
        # If we didn't just create a site, we assume the site name likely matches the base_name
        desired_name = base_name

    # Always attempt to gather SFTP details
    print("\n---[ Fetching Site Details and SFTP Credentials ]---")
    site = get_site_with_ips(token, clean_hostname, desired_name)
    if not site:
        print(f"⚠️ Could not locate the site via API using domain '{clean_hostname}' or fallback name '{desired_name}'.")
        record["status"] = f"Failed: could not locate the site via API (domain '{clean_hostname}' / name '{desired_name}')"
    else:
        site_id = site["id"]
        staging_url = site.get("url") or ""

        # ---[ EXACT STAGING & IP OUTPUT ]---
        # Clean the protocol off the staging URL string returned by the API
        clean_staging_url = staging_url.replace("https://", "").replace("http://", "").split("/")[0]

        # The Pressable API response includes 'ipAddresses' (camelCase) for the allocated IPs
        ips = site.get("ipAddresses", [])

        # Last-resort fallback only — DNS on the staging hostname does not
        # reliably return the site's dedicated IPs, so this should rarely trigger
        # now that get_site_with_ips() waits for the API to report them.
        if not ips:
            print("  ⚠️ API never returned dedicated IPs after retrying — falling back to DNS (may be inaccurate).")
            try:
                _, _, ips = socket.gethostbyname_ex(clean_staging_url)
            except Exception as e:
                print(f"  ⚠️ DNS lookup for {clean_staging_url} failed too: {e}")

        ip_str = " ".join(ips)
        print(f"\n{clean_staging_url}\n{ip_str}\n")
        # ------------------------------------

        sftp_user, sftp_password = get_sftp_credentials(token, site_id)

        sftp_data = {
            "staging": staging_url,
            "sftp_user": sftp_user,
            "sftp_password": sftp_password
        }
        print(f"✅ Gathered credentials!")
        print(f"  Staging URL: {staging_url}")
        print(f"  SFTP Username: {sftp_user}")
        print(f"  SFTP Password: {sftp_password}")

        record.update(sftp_data)
        record["ips"] = ip_str
        record["status"] = "Site details gathered"

    # Site-creation-only mode (or both browser phases off) stops here:
    # no credential prompt, no reset, no login.
    if not browser_phases:
        print("\n---[ No browser phases enabled: skipping credential check, Phase 2 and Phase 3 ]---")
        return page

    # ---[ Credential Check: skip the whole reset flow if a working password is already known ]---
    print("\n---[ Credential Check ]---")
    existing_password = settings.global_wp_admin_password
    if existing_password:
        print(f"  ℹ️ Using the hardcoded GLOBAL_WP_ADMIN_PASSWORD for {origin_url} — skipping Phase 2 and the password reset.")
    elif settings.verbose_questions:
        existing_password = input(
            f" If you already have a working password for {target_email} on {origin_url}, "
            f"paste it now to skip straight to login (or press Enter to go through the password reset flow, "
            f"or type 'skip' to skip this site): "
        ).strip()

        if existing_password.lower() == "skip":
            print(f"⏭️  Skipping {origin_url} — no migration triggered for this site.")
            record["status"] = "Skipped at the credential check"
            return page
    else:
        print(f"  ℹ️ VERBOSE_QUESTIONS is off — going straight to the password reset flow for {origin_url}.")

    if existing_password:
        if not DO_PHASE_3_PROCESS_MIGRATION:
            print("\n---[ SKIPPED: Phase 3 disabled, existing password not used ]---")
            record["status"] = "Skipped: Phase 3 disabled"
            return page

        print("\n---[ Using existing password — skipping Phase 2 and the password reset step ]---")
        sftp_data = sftp_data or prompt_for_sftp_details()
        if not sftp_data:
            print(f"⏭️  Skipping {origin_url} — no SFTP details available.")
            record["status"] = "Skipped: no SFTP details available"
            return page

        await start_migration(page, existing_password, target_email, settings.migration_email,
                              sftp_data, origin_url, wp_login_url, enable_maintenance, record)
        return page

    # ---[ PHASE 2: Request Reset ]---
    phase2_status = None
    if DO_PHASE_2_REQUEST_RESET:
        print("\n---[ PHASE 2: Origin Site Automation (Request Reset) ]---")
        phase2_status, page = await request_password_reset(page, wp_login_url, target_email)
        if phase2_status == "no_user":
            print(f"⏭️  Skipping {origin_url} — no account exists for {target_email} on the origin site.")
            record["status"] = f"Skipped: no account for {target_email} on the origin site"
            return page
    else:
        print("\n---[ SKIPPED PHASE 2: Request Reset ]---")

    # ---[ PHASE 3: Process Reset & Migrate ]---
    if not DO_PHASE_3_PROCESS_MIGRATION:
        print("\n---[ SKIPPED PHASE 3: Process Reset & Migrate ]---")
        if phase2_status == "success":
            record["status"] = "Password reset requested (Phase 3 disabled)"
        return page

    print("\n---[ PHASE 3: Origin Site Automation (Migrate) ]---")

    # Safeguard: if API gathering failed, fall back to manual input
    sftp_data = sftp_data or prompt_for_sftp_details()
    if not sftp_data:
        print(f"⏭️  Skipping {origin_url} — no SFTP details available.")
        record["status"] = "Skipped: no SFTP details available"
        return page

    print("\n==================================================")
    if phase2_status == "failed":
        print(f" [ACTION REQUIRED] The automatic password reset request failed for {origin_url} after retrying.")
        print(f" Manually trigger a password reset email for {target_email} on that site, then paste the link below.")
    else:
        print(f" [ACTION REQUIRED] Check your email for the reset link for {origin_url}.")
    reset_link = input(" Paste the full reset link, or type 'skip' to skip this site, and press Enter: ").strip()
    print("==================================================\n")

    if not reset_link or reset_link.lower() == "skip":
        print(f"⏭️  Skipping {origin_url} — no migration triggered for this site.")
        record["status"] = "Skipped at the reset link prompt"
        return page

    # A new password for every site, so one leaked password only affects one site
    new_password = generate_password()
    try:
        await reset_password_on_origin(page, reset_link, new_password)
    except Exception as e:
        print(f"  ❌ Error during password reset: {str(e)[:100]}")
        # The form may have been submitted before the error, so keep the
        # password in the summary in case it did change
        print(f"  ℹ️ If the reset did go through, the new password is: {new_password}")
        record["wp_password"] = f"{new_password} (unconfirmed — reset step failed)"
        record["status"] = f"Failed during password reset: {str(e)[:100]}"
        return page

    record["wp_password"] = new_password
    await start_migration(page, new_password, target_email, settings.migration_email,
                          sftp_data, origin_url, wp_login_url, enable_maintenance, record)
    return page


async def process_all_sites(token, origin_lines, page, settings):
    """Run process_single_site() for every line in ORIGIN_URLS, carrying on to
    the next site if one fails. Returns (site_summary, page)."""
    site_summary = []
    for index, line in enumerate(origin_lines, start=1):
        origin_url, wp_login_url = parse_origin_line(line, settings.custom_login_url)

        # This site's entry in the end-of-run summary, filled in as it goes
        record = {"origin": origin_url or line, "status": "Not finished"}
        site_summary.append(record)

        print(f"\n==================== Site {index} of {len(origin_lines)}: {origin_url or line} ====================")
        if not origin_url:
            print(f"❌ Could not read a site URL from '{line}' — skipping.")
            record["status"] = "Skipped: could not read a URL from this line"
            continue

        try:
            page = await process_single_site(token, origin_url, wp_login_url, page, settings, record)
        except Exception as e:
            print(f"❌ Unhandled error processing {origin_url}: {str(e)[:200]}")
            print("   Moving on to the next site...")
            record["status"] = f"Failed: unhandled error: {str(e)[:150]}"

    return site_summary, page


# ---[ MAIN EXECUTION LOOP ]---

async def main():
    print("==================================================")
    print("    PRESSABLE BULK MIGRATION SCRIPT              ")
    print("==================================================\n")

    # 1. Load and check the hardcoded settings before anything is created
    client_id = CLIENT_ID.strip()
    client_secret = CLIENT_SECRET.strip()
    settings = Settings(
        # Blank means "ask for each site" (handled in process_single_site)
        datacenter=DATACENTER.strip().upper(),
        target_email=TARGET_EMAIL.strip(),
        migration_email=MIGRATION_EMAIL.strip(),
        custom_site_name=CUSTOM_SITE_NAME.strip(),
        custom_login_url=CUSTOM_LOGIN_URL.strip(),
        global_wp_admin_password=GLOBAL_WP_ADMIN_PASSWORD.strip(),
        verbose_questions=VERBOSE_QUESTIONS,
        enable_maintenance_mode=ENABLE_MAINTENANCE_MODE,
    )
    need_browser = DO_PHASE_2_REQUEST_RESET or DO_PHASE_3_PROCESS_MIGRATION

    if not client_id or not client_secret:
        print("❌ CLIENT_ID and CLIENT_SECRET must both be set. Exiting.")
        return

    origin_lines = [u.strip() for u in ORIGIN_URLS.strip().splitlines() if u.strip()]
    if not origin_lines:
        print("❌ ORIGIN_URLS is empty. Add at least one URL and re-run. Exiting.")
        return

    # Catch a mistyped DATACENTER now, before anything is created
    if settings.datacenter and settings.datacenter not in VALID_DATACENTERS:
        print(f"❌ DATACENTER '{settings.datacenter}' is not valid. Use one of: {', '.join(VALID_DATACENTERS)} "
              "(or leave it blank to be asked per site). Exiting.")
        return
    if not settings.datacenter and DO_PHASE_1_CREATE_SITE:
        print("ℹ️ DATACENTER is blank — you will be asked which datacenter to use for each site.")

    if need_browser and not settings.target_email:
        print("❌ TARGET_EMAIL must be set to log in to the origin sites. Exiting.")
        return
    if DO_PHASE_3_PROCESS_MIGRATION and not settings.migration_email:
        print("❌ MIGRATION_EMAIL must be set to start migrations. Exiting.")
        return

    if SITE_CREATION_ONLY:
        print("ℹ️ SITE_CREATION_ONLY is set — sites will be created and their details printed, "
              "but no password resets or logins will be attempted.")
        # Without Phase 1 this mode only looks up existing sites (and still resets
        # their SFTP passwords to display them), which is probably not intended.
        if not DO_PHASE_1_CREATE_SITE:
            print("⚠️ DO_PHASE_1_CREATE_SITE is False, so no sites will be created — this run will only "
                  "look up existing sites and RESET their SFTP passwords.")

    # ---[ API Authentication (shared across all sites) ]---
    print("\n---[ Authenticating with Pressable API ]---")
    token = get_access_token(client_id, client_secret)
    print("✅ Authentication OK.")

    if need_browser:
        user_data_dir = tempfile.mkdtemp()

        async with async_playwright() as p:

            # Configure the browser, with Basic Auth if provided
            context_args = {
                "headless": False, # Keep false so you can watch it happen
                "slow_mo": 1000,
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "args": ["--no-sandbox", "--disable-setuid-sandbox", "--no-first-run", "--disable-save-password-bubble"]
            }

            if BASIC_AUTH_USER and BASIC_AUTH_PASS:
                context_args["http_credentials"] = {
                    "username": BASIC_AUTH_USER.strip(),
                    "password": BASIC_AUTH_PASS.strip()
                }
                print(f"ℹ️ Basic HTTP Auth loaded for user: {BASIC_AUTH_USER}")

            context = await p.chromium.launch_persistent_context(user_data_dir, **context_args)

            stealth = Stealth()
            await stealth.apply_stealth_async(context)

            page = context.pages[0]

            # Print the BlogVault URL whenever the migration plugin hands over to it
            page.on("framenavigated", lambda frame: print(f"\n  🌐 Captured BlogVault Navigation: {frame.url}\n") if "migrate.blogvault.net" in frame.url else None)

            site_summary, page = await process_all_sites(token, origin_lines, page, settings)
            print_site_summary(site_summary)

            print("\n🎉 Bulk automation complete! The browser will remain open so you can verify.")
            print(f"👉 Current Browser URL: {page.url}")
            await asyncio.Event().wait()
    else:
        site_summary, _ = await process_all_sites(token, origin_lines, None, settings)
        print_site_summary(site_summary)
        print("\n🎉 Bulk automation complete!")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Closing...")
