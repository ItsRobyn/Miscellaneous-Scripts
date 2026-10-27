#!/usr/bin/env python3
"""
Pressable End-to-End Migration Automation (Bulk)

Same as all-in-one.py, but processes a list of origin sites one after
another instead of a single hardcoded URL.

What it does, per site in ORIGIN_URLS:
1. Authenticates with Pressable API (once, shared across all sites).
   Creates the site (if enabled).
2. Always fetches SFTP credentials for the target site (forcing a password reset).
3. Uses Playwright (with stealth) to request a password reset on the Origin site.
4. Pauses and waits for you to paste the password reset link from your email.
5. Automatically changes the Origin password, logs in, checks for Yoast,
   installs the Pressable migration plugin, and starts the migration.

Because step 4 needs a human to check email and paste a reset link, this is
still a sequential, hands-on process for each site — it just saves you from
re-running the script and re-entering the config for every URL.

Set SITE_CREATION_ONLY = True to stop after step 2: the sites are created and
their details/credentials printed (plus a summary at the end), but no browser is
opened and no password resets or logins are attempted on the origin sites.
"""

import time
import re
import random
import string
import asyncio
import tempfile
import socket
import secrets
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright
# --- FIXED IMPORT ---
from playwright_stealth import Stealth

try:
    import tldextract
    HAVE_TLDEXTRACT = True
except ImportError:
    HAVE_TLDEXTRACT = False

# ==========================================
# ---[ CONFIGURATION SETTINGS ]---
# Hardcode your variables here before running
# ==========================================
CLIENT_ID = ""
CLIENT_SECRET = ""

# NEW: List of origin sites to migrate, processed one at a time in order.
ORIGIN_URLS = [

]

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

# NEW: Set a custom name for the Pressable site.
# If left blank (""), it will automatically extract the name from each origin URL.
CUSTOM_SITE_NAME = ""

# NEW: Set a custom WP-Admin login URL if the site doesn't use the standard /wp-login.php
# Example: "https://example.com/my-custom-login/"
# Applies to every site in ORIGIN_URLS — leave blank unless all sites share this path.
CUSTOM_LOGIN_URL = ""

# NEW: Basic HTTP Auth (Used ONLY if the server prompts for it)
BASIC_AUTH_USER = ""
BASIC_AUTH_PASS = ""

TARGET_EMAIL = "migrations@pressable.com"

MIGRATION_EMAIL = ""

# NEW: If set, this single password is used to log in to WP Admin on EVERY site
# in ORIGIN_URLS. When set, the per-site "do you have a password?" prompt, Phase 2
# (request reset), and the password reset step are all skipped entirely — it goes
# straight to login using this password. Leave blank ("") to go through the normal
# per-site prompt/reset flow instead.
GLOBAL_WP_ADMIN_PASSWORD = ""

# NEW: If True, skips the per-site "do you already have a working password?" prompt
# and goes straight into the password reset flow (Phase 2) for every site — useful
# when you know none of the sites in ORIGIN_URLS have a usable password yet and every
# one of them will need a reset. Ignored if GLOBAL_WP_ADMIN_PASSWORD is set, since
# that already skips the prompt (and the reset) on its own.
ALWAYS_SKIP_PASSWORD_PROMPT = False

# ==========================================
# ---[ EXECUTION PHASES ]---
# Toggle these booleans to run specific parts
# ==========================================

DO_PHASE_1_CREATE_SITE = True         # Create site (False skips creation but STILL gathers info)
DO_PHASE_2_REQUEST_RESET = True       # Trigger the origin password reset link
DO_PHASE_3_PROCESS_MIGRATION = True   # Input reset link, login, and start migration

# NEW: If True, the script ONLY creates the sites and prints their details
# (staging URL, IPs, SFTP username/password), then prints a summary of every
# site at the end. Phases 2 and 3 are forced off regardless of the toggles
# above — no browser is opened, no password prompts are shown, and no password
# resets or logins are attempted on the origin sites.
SITE_CREATION_ONLY = False

# Enforce SITE_CREATION_ONLY by switching off the browser phases here, so the
# rest of the script doesn't need to check both settings everywhere.
if SITE_CREATION_ONLY:
    DO_PHASE_2_REQUEST_RESET = False
    DO_PHASE_3_PROCESS_MIGRATION = False


# ---[ PRESSABLE API HELPER FUNCTIONS ]---

API_BASE = "https://my.pressable.com"
V1 = f"{API_BASE}/v1"

def generate_password() -> str:
    """Generate a fresh random password. Called per-site so no two sites
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

            sftp_password = reset_resp.json().get("data")
            return sftp_user, str(sftp_password)

        # If the user doesn't exist yet, wait and try again
        print(f"  ⏳ SFTP user not provisioned yet. Waiting {delay} seconds... (Attempt {attempt + 1}/{max_retries})")
        time.sleep(delay)

    # If the loop exhausts all retries, then we throw the error
    raise RuntimeError(f"No SFTP users exist for site {site_id} after waiting {max_retries * delay} seconds.")


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

    # --- NEW: Handle iThemes / Solid Security Magic Link ---
    fallback_selector = "a.itsec-pwls-login-fallback__link"
    if await page.is_visible(fallback_selector):
        print("  ℹ️ Magic link fallback detected. Clicking to reveal standard form...")
        await page.click(fallback_selector)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)
    # -------------------------------------------------------

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


async def login_and_run_migration(page, password, target_email, migration_email, sftp_data, base_url, wp_login_url):
    # Log in to the site
    print(f"🚀 [Playwright] Logging into {wp_login_url}...")
    await goto_with_retries(page, wp_login_url, retries=2, wait_until="networkidle", timeout=15000)
    await asyncio.sleep(1.5)

    # --- NEW: Handle iThemes / Solid Security Magic Link ---
    fallback_selector = "a.itsec-pwls-login-fallback__link"
    if await page.is_visible(fallback_selector):
        print("  ℹ️ Magic link fallback detected. Clicking to reveal standard login...")
        await page.click(fallback_selector)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)
    # -------------------------------------------------------

    await page.wait_for_selector("input[name='log']", state="visible", timeout=10000)
    await page.fill("input[name='log']", target_email)
    await page.fill("input[name='pwd']", password)
    await asyncio.sleep(1)
    await page.click("#wp-submit")

    await page.wait_for_load_state("networkidle", timeout=60000)
    await asyncio.sleep(1.5)

    # --- NEW: Check for Yoast SEO ---
    page_content = await page.content()
    if "yoast" in page_content.lower():
        print("  ✅ Yoast SEO plugin detected on the dashboard.")
    else:
        print("  ℹ️ Yoast SEO plugin not found on the dashboard.")
    # --------------------------------

    # Install & Activate Plugin
    PLUGIN_SLUG = "pressable-automated-migration"
    target_plugin_url = f"{base_url}/wp-admin/plugin-install.php?s={PLUGIN_SLUG}&tab=search&type=term"
    print(f"🚀 [Playwright] Installing Migration Plugin...")
    await page.goto(target_plugin_url, wait_until="networkidle")
    await asyncio.sleep(1.5)

    plugin_card = f".plugin-card-{PLUGIN_SLUG}"
    await page.wait_for_selector(plugin_card, state="visible", timeout=15000)

    install_btn = f"{plugin_card} a.install-now"
    activate_btn = f"{plugin_card} a.activate-now"

    if await page.is_visible(activate_btn):
        print(f"  ℹ️ Plugin installed but inactive. Activating...")
        await page.click(activate_btn)
        await page.wait_for_load_state("networkidle")
    elif await page.is_visible(install_btn):
        print(f"  ℹ️ Plugin not installed. Installing...")
        await page.click(install_btn)
        await page.wait_for_selector(activate_btn, state="visible", timeout=45000)
        await asyncio.sleep(1) # Pause between install and activation
        print(f"  ℹ️ Plugin installed. Activating...")
        await page.click(activate_btn)
        await page.wait_for_load_state("networkidle")
    else:
        print(f"  ✅ Plugin is already installed and active.")

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


async def process_reset_and_migrate(page, reset_link, new_password, target_email, migration_email, sftp_data, base_url, wp_login_url):
    try:
        await reset_password_on_origin(page, reset_link, new_password)
        await login_and_run_migration(page, new_password, target_email, migration_email, sftp_data, base_url, wp_login_url)
    except Exception as e:
        print(f"  ❌ Error during reset and migration sequence: {str(e)[:100]}")


async def process_existing_password_migration(page, existing_password, target_email, migration_email, sftp_data, base_url, wp_login_url):
    print(f"\n🚀 [Playwright] Using existing password — skipping password reset...")
    try:
        await login_and_run_migration(page, existing_password, target_email, migration_email, sftp_data, base_url, wp_login_url)
    except Exception as e:
        print(f"  ❌ Error during login and migration sequence: {str(e)[:100]}")


# ---[ DATACENTER HELPERS ]---

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


# ---[ SUMMARY HELPERS ]---

def print_site_summary(site_summary: list):
    """Print every site's details together at the end of the run, so they
    don't have to be picked out of the per-site output.

    Each entry in site_summary is a dict with an "origin" key plus either the
    gathered details ("staging", "ips", "sftp_user", "sftp_password") or an
    "error" key describing why that site's details couldn't be gathered."""
    print("\n==================== SUMMARY ====================")
    if not site_summary:
        print("No sites were processed.")
        return

    for entry in site_summary:
        print(f"\nOrigin:        {entry['origin']}")
        if "error" in entry:
            print(f"  ❌ {entry['error']}")
            continue
        print(f"Staging URL:   {entry['staging']}")
        print(f"IPs:           {entry['ips'] or '(none found)'}")
        print(f"SFTP Username: {entry['sftp_user']}")
        print(f"SFTP Password: {entry['sftp_password']}")

    failed = sum(1 for entry in site_summary if "error" in entry)
    print(f"\n{len(site_summary) - failed} succeeded, {failed} failed.")
    print("=================================================")


# ---[ PER-SITE ORCHESTRATION ]---

async def process_single_site(token, origin_url, page, datacenter, target_email, migration_email,
                               custom_site_name, custom_login_url, global_wp_admin_password,
                               always_skip_password_prompt, site_summary):
    """Runs Phases 1-3 for a single origin site. `page` is the shared
    Playwright page reused across every site in the bulk run (None if no
    browser automation phases are enabled). The site's gathered details (or
    an error) are appended to `site_summary` for the end-of-run summary.
    Returns the page to use for the next site — normally the same one passed
    in, but a fresh page if the original had to be recreated after the
    browser crashed mid-run."""

    new_password = generate_password()

    parsed_url = urlparse(origin_url if "://" in origin_url else f"https://{origin_url}")
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    clean_hostname = extract_hostname(origin_url)

    wp_login_url = custom_login_url if custom_login_url else f"{base_url}/wp-login.php"
    base_name = custom_site_name if custom_site_name else base_label_from_domain(origin_url)

    sftp_data = None

    # Handle Creation vs. Skip Creation
    if DO_PHASE_1_CREATE_SITE:
        print("\n---[ PHASE 1: Pressable API Site Creation ]---")
        desired_name = next_available_name(token, base_name)
        # No datacenter configured, so ask which one to use for this site
        if not datacenter:
            datacenter = prompt_for_datacenter(origin_url)
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
        site_summary.append({
            "origin": origin_url,
            "error": f"Could not locate the site via API (domain '{clean_hostname}' / name '{desired_name}').",
        })
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
            except Exception:
                pass

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

        site_summary.append({
            "origin": origin_url,
            "staging": staging_url,
            "ips": ip_str,
            "sftp_user": sftp_user,
            "sftp_password": sftp_password,
        })

    # Site-creation-only mode stops here: no credential prompt, no reset, no login.
    if SITE_CREATION_ONLY:
        print("\n---[ SITE_CREATION_ONLY: skipping credential check, Phase 2 and Phase 3 ]---")
        return page

    # ---[ Credential Check: skip the whole reset flow if a working password is already known ]---
    existing_password = global_wp_admin_password
    if existing_password:
        print("\n---[ Credential Check ]---")
        print(f"  ℹ️ Using the hardcoded GLOBAL_WP_ADMIN_PASSWORD for {origin_url} — skipping prompt, Phase 2, and password reset.")
    elif always_skip_password_prompt:
        print("\n---[ Credential Check ]---")
        print(f"  ℹ️ ALWAYS_SKIP_PASSWORD_PROMPT is set — skipping the prompt and going straight to the password reset flow for {origin_url}.")
    elif DO_PHASE_2_REQUEST_RESET or DO_PHASE_3_PROCESS_MIGRATION:
        print("\n---[ Credential Check ]---")
        existing_password = input(
            f" If you already have a working password for {target_email} on {origin_url}, "
            f"paste it now to skip straight to login (or press Enter to go through the password reset flow, "
            f"or type 'skip' to skip this site): "
        ).strip()

        if existing_password.lower() == "skip":
            print(f"⏭️  Skipping {origin_url} — no migration triggered for this site.")
            return page

    if existing_password:
        if not DO_PHASE_3_PROCESS_MIGRATION:
            print("\n---[ SKIPPED: Phase 3 disabled, existing password not used ]---")
            return page

        print("\n---[ Using existing password — skipping Phase 2 and the password reset step ]---")

        # Safeguard: if API gathering failed, fall back to manual input
        if not sftp_data:
            print("⚠️ WARNING: API data gathering failed. No SFTP credentials available.")
            manual_staging = input(" Enter Target Staging URL: ").strip()
            manual_sftp_user = input(" Enter SFTP Username: ").strip()
            manual_sftp_password = input(" Enter SFTP Password: ").strip()

            sftp_data = {
                "staging": manual_staging,
                "sftp_user": manual_sftp_user,
                "sftp_password": manual_sftp_password
            }

        await process_existing_password_migration(
            page, existing_password, target_email, migration_email, sftp_data, base_url, wp_login_url
        )
        return page

    # ---[ PHASE 2: Request Reset ]---
    phase2_status = None
    if DO_PHASE_2_REQUEST_RESET:
        print("\n---[ PHASE 2: Origin Site Automation (Request Reset) ]---")
        phase2_status, page = await request_password_reset(page, wp_login_url, target_email)
        if phase2_status == "no_user":
            print(f"⏭️  Skipping {origin_url} — no account exists for {target_email} on the origin site.")
            return page
    else:
        print("\n---[ SKIPPED PHASE 2: Request Reset ]---")

    # ---[ PHASE 3: Process Reset & Migrate ]---
    if DO_PHASE_3_PROCESS_MIGRATION:
        print("\n---[ PHASE 3: Origin Site Automation (Migrate) ]---")

        # Safeguard: if API gathering failed, fall back to manual input
        if not sftp_data:
            print("⚠️ WARNING: API data gathering failed. No SFTP credentials available.")
            manual_staging = input(" Enter Target Staging URL: ").strip()
            manual_sftp_user = input(" Enter SFTP Username: ").strip()
            manual_sftp_password = input(" Enter SFTP Password: ").strip()

            sftp_data = {
                "staging": manual_staging,
                "sftp_user": manual_sftp_user,
                "sftp_password": manual_sftp_password
            }

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
            return page

        # Complete reset and initiate migration
        await process_reset_and_migrate(
            page,
            reset_link,
            new_password,
            target_email,
            migration_email,
            sftp_data,
            base_url,
            wp_login_url
        )
    else:
        print("\n---[ SKIPPED PHASE 3: Process Reset & Migrate ]---")

    return page


# ---[ MAIN EXECUTION LOOP ]---

async def main():
    print("==================================================")
    print("    PRESSABLE BULK MIGRATION SCRIPT              ")
    print("==================================================\n")

    # 1. Load Hardcoded Inputs
    client_id = CLIENT_ID.strip()
    client_secret = CLIENT_SECRET.strip()
    # Blank means "ask for each site" (handled in process_single_site)
    datacenter = DATACENTER.strip().upper()
    target_email = TARGET_EMAIL.strip()
    migration_email = MIGRATION_EMAIL.strip()

    custom_site_name = CUSTOM_SITE_NAME.strip()
    custom_login_url = CUSTOM_LOGIN_URL.strip()
    global_wp_admin_password = GLOBAL_WP_ADMIN_PASSWORD.strip()
    always_skip_password_prompt = ALWAYS_SKIP_PASSWORD_PROMPT

    origin_urls = [u.strip() for u in ORIGIN_URLS if u.strip()]
    if not origin_urls:
        print("❌ ORIGIN_URLS is empty. Add at least one URL and re-run. Exiting.")
        return

    # Catch a mistyped DATACENTER now, before anything is created
    if datacenter and datacenter not in VALID_DATACENTERS:
        print(f"❌ DATACENTER '{datacenter}' is not valid. Use one of: {', '.join(VALID_DATACENTERS)} "
              "(or leave it blank to be asked per site). Exiting.")
        return
    if not datacenter and DO_PHASE_1_CREATE_SITE:
        print("ℹ️ DATACENTER is blank — you will be asked which datacenter to use for each site.")

    if SITE_CREATION_ONLY:
        print("ℹ️ SITE_CREATION_ONLY is set — sites will be created and their details printed, "
              "but no password resets or logins will be attempted.")
        # Without Phase 1 this mode only looks up existing sites (and still resets
        # their SFTP passwords to display them), which is probably not intended.
        if not DO_PHASE_1_CREATE_SITE:
            print("⚠️ DO_PHASE_1_CREATE_SITE is False, so no sites will be created — this run will only "
                  "look up existing sites and RESET their SFTP passwords.")

    # Collects each site's details (or error) for the summary at the end of the run
    site_summary = []

    # ---[ API Authentication (shared across all sites) ]---
    print("\n---[ Authenticating with Pressable API ]---")
    token = get_access_token(client_id, client_secret)
    print("✅ Authentication OK.")

    need_browser = DO_PHASE_2_REQUEST_RESET or DO_PHASE_3_PROCESS_MIGRATION

    if need_browser:
        user_data_dir = tempfile.mkdtemp()

        async with async_playwright() as p:

            # --- NEW: Configure context with Basic Auth if provided ---
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

            # --- FIXED STEALTH APPLICATION ---
            stealth = Stealth()
            await stealth.apply_stealth_async(context)

            page = context.pages[0]

            for index, origin_url in enumerate(origin_urls, start=1):
                print(f"\n==================== Site {index} of {len(origin_urls)}: {origin_url} ====================")
                try:
                    page = await process_single_site(
                        token, origin_url, page, datacenter, target_email, migration_email,
                        custom_site_name, custom_login_url, global_wp_admin_password,
                        always_skip_password_prompt, site_summary
                    )
                except Exception as e:
                    print(f"❌ Unhandled error processing {origin_url}: {str(e)[:200]}")
                    print("   Moving on to the next site...")

            print("\n🎉 Bulk automation complete! The browser will remain open so you can verify.")
            print(f"👉 Current Browser URL: {page.url}")
            await asyncio.Event().wait()
    else:
        for index, origin_url in enumerate(origin_urls, start=1):
            print(f"\n==================== Site {index} of {len(origin_urls)}: {origin_url} ====================")
            try:
                await process_single_site(
                    token, origin_url, None, datacenter, target_email, migration_email,
                    custom_site_name, custom_login_url, global_wp_admin_password,
                    always_skip_password_prompt, site_summary
                )
            except Exception as e:
                print(f"❌ Unhandled error processing {origin_url}: {str(e)[:200]}")
                print("   Moving on to the next site...")
                site_summary.append({"origin": origin_url, "error": f"Unhandled error: {str(e)[:200]}"})

        # The summary is only shown in SITE_CREATION_ONLY mode, where the run is
        # all about collecting each new site's details in one place.
        if SITE_CREATION_ONLY:
            print_site_summary(site_summary)

        print("\n🎉 Bulk automation complete!")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Closing...")
