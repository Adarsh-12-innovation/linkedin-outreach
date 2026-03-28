"""
LinkedIn Saved Posts Outreach Agent
=====================================
Fetches your LinkedIn saved posts (via li_at cookie), uses Gemini to extract
emails & phone numbers, and auto-sends personalized outreach emails via Gmail.

Flow:
    You save posts on LinkedIn (phone/desktop) throughout the day
    → Run this script once daily (manually or via GitHub Actions)
    → Fetches your saved posts from the last 24 hours
    → Fetches full content of each post
    → Gemini extracts emails, phones, poster name, role details
    → Auto-sends email (with resume) to posts that have an email
    → Saves phone leads separately for manual follow-up
    → Tracks history to avoid re-contacting

Prerequisites:
    pip install requests beautifulsoup4 google-api-python-client google-auth-oauthlib

=== LOCAL SETUP ===

    1. LINKEDIN li_at COOKIE (one-time, lasts ~1 year):
       - Open LinkedIn in Chrome on your PC → Log in
       - Press F12 → Application tab → Cookies → linkedin.com
       - Find the cookie named "li_at" → Copy the value
       - Set as env var: export LINKEDIN_LI_AT="your_cookie_value"

    2. GEMINI API:
       - Get free key from https://aistudio.google.com/app/apikey
       - Set as env var: export GEMINI_API_KEY="your_key"

    3. GMAIL API (OAuth2):
       - Go to https://console.cloud.google.com → Create/select project
       - APIs & Services → Library → Search "Gmail API" → Enable
       - APIs & Services → Credentials → + CREATE CREDENTIALS → OAuth client ID
         → Application type: Desktop app → Create → Download JSON
       - Rename to credentials.json, place in this directory
       - Run the script once locally — it opens browser for Google consent
       - After consent, token.json is created and reused automatically

    4. RESUME:
       - Place resume.pdf in this directory (or use --resume flag)

=== GITHUB ACTIONS SETUP (automated daily runs) ===

    1. Create a PRIVATE GitHub repo and push this project to it

    2. Run the script ONCE locally first to generate token.json (Gmail OAuth)

    3. Go to your GitHub repo → Settings → Secrets and variables → Actions
       Add these repository secrets:
         LINKEDIN_LI_AT          → your li_at cookie value
         GEMINI_API_KEY          → your Gemini API key
         GMAIL_CREDENTIALS_JSON  → open credentials.json, copy-paste entire content
         GMAIL_TOKEN_JSON        → open token.json, copy-paste entire content

    4. Add resume.pdf directly to the repo (just commit it, no encoding needed)

    5. Push the .github/workflows/daily_outreach.yml file (included in this repo)

    6. The workflow runs daily at 9 PM UTC (adjust cron in the yml)
       - It also commits updated history/phone leads back to the repo
       - You can trigger it manually from Actions tab anytime

Usage:
    python saved_posts_outreach.py                # Full auto pipeline
    python saved_posts_outreach.py --dry-run      # Everything except sending emails
    python saved_posts_outreach.py --hours 48     # Look back 48 hours instead of 24
    python saved_posts_outreach.py --resume path/to/resume.pdf
"""

import os
import sys
import json
import base64
import re
import time
import argparse
import logging
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

import requests

from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

CONFIG = {
    # LinkedIn
    "LINKEDIN_LI_AT": os.getenv("LINKEDIN_LI_AT", "YOUR_LI_AT_COOKIE"),
    "LINKEDIN_JSESSIONID": os.getenv("LINKEDIN_JSESSIONID", "YOUR_JSESSIONID_COOKIE"),


    # Gemini
    "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY"),
    "ALTERNATIVE_GEMINI_API_KEY": os.getenv("ALTERNATIVE_GEMINI_API_KEY", ""),
    "GEMINI_MODEL": "gemini-2.5-flash",

    # "GEMINI_MODEL": "gemini-2.0-flash-lite",

    # Gmail OAuth2
    "GMAIL_CREDENTIALS_FILE": "credentials.json",
    "GMAIL_TOKEN_FILE": "token.json",
    "GMAIL_SCOPES": ["https://www.googleapis.com/auth/gmail.send"],

    # Your details
    "SENDER_NAME": "Adarsh Bansal",
    "SENDER_EMAIL": "adarshbansal1995@gmail.com",  # Auto-detected from Gmail auth

    # Resume
    "RESUME_PATH": "Adarsh Bansal_CV_2026.pdf",

    # Time window
    "LOOKBACK_HOURS": 48,

    # Tracking
    "HISTORY_FILE": "outreach_history.json",
    "PHONE_LEADS_FILE": "phone_leads.json",
    "RESULTS_DIR": "results",
}

# ─────────────────────────────────────────────
# EMAIL TEMPLATE
# ─────────────────────────────────────────────

EMAIL_TEMPLATE = {
    "subject": "Application AI/ML Engineer — Available for Contract roles",
    "body": """\
Hello,

I came across your recent post on LinkedIn about the {role_title} role and wanted to reach out.

Hope you are doing well! Wished to know for suitable contract roles in Data Science and AI/ML or Analytics.

Linkedin profile- https://www.linkedin.com/in/adarsh-bansal-31490a124/

Skills:
Programming: Python (scikit-learn, Pandas, NumPy), SQL, TensorFlow
GenAl Tools & Frameworks: Langchain, Langgraph Studio, Llamaindex, CrewAl, n8n, Streamlit, HuggingFace, Ollama
Data Engineering/Analytics: MLFlow, AWS (S3, EC2, Lambda, ECS, ECR, Cloudformation, Sagemaker etc.), Azure, Alteryx, Tableau, PowerBI, SQL, Dockers


It would be great if you can share any suitable fit. Kindly find my resume attached below.

Available to join immediately.

Looking forward to hearing from you. Please feel free to reach out to me on +91-8077593119.

Best regards,
{sender_name}
""",
}

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("saved-posts-agent")


# ═══════════════════════════════════════════════
# STEP 1: FETCH LINKEDIN SAVED POSTS
# ═══════════════════════════════════════════════

def create_linkedin_session() -> requests.Session:
    """Create an authenticated LinkedIn session using li_at + JSESSIONID cookies."""
    session = requests.Session()

    li_at = CONFIG["LINKEDIN_LI_AT"]
    jsessionid = CONFIG["LINKEDIN_JSESSIONID"].strip('"')  # Remove quotes if present

    session.cookies.set("li_at", li_at, domain=".linkedin.com")
    session.cookies.set("JSESSIONID", f'"{jsessionid}"', domain=".linkedin.com")

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/vnd.linkedin.normalized+json+2.1",
        "x-li-lang": "en_US",
        "x-restli-protocol-version": "2.0.0",
        "csrf-token": jsessionid,
    })

    log.info("LinkedIn session created with li_at + JSESSIONID")
    return session


def fetch_saved_posts(session: requests.Session, history: dict = None) -> list[dict]:
    """
    Fetch saved items from LinkedIn.
    Optimization: Stops immediately when it hits a post URN already in outreach_history.json.
    """
    log.info("Fetching saved posts list (IDs only)...")
    
    seen_urns = set(history.get("contacted_urns", [])) if history else set()
    saved_items = []
    
    # Try multiple known endpoint patterns
    endpoints = [
        "https://www.linkedin.com/voyager/api/graphql?queryId=voyagerSaveDashSaves.f465d4848c0ef24e03c2ddd1fbe1e8f6&variables=(count:{count},start:{start})",
        "https://www.linkedin.com/voyager/api/voyagerContentDashSaves?count={count}&start={start}&q=savedByMe",
        "https://www.linkedin.com/voyager/api/saveDashSaves?count={count}&start={start}",
    ]

    for endpoint_template in endpoints:
        log.info(f"  Checking endpoint: {endpoint_template[:60]}...")
        start = 0
        count = 20
        endpoint_results = []
        already_seen_trigger = False

        while True:
            url = endpoint_template.format(count=count, start=start)
            try:
                resp = session.get(url, timeout=15)
                if resp.status_code != 200: break
                data = resp.json()
            except: break

            elements = (
                data.get("elements", [])
                or data.get("data", {}).get("saveDashSavesByAll", {}).get("elements", [])
                or data.get("included", [])
            )
            if not elements: break

            for item in elements:
                urn = (
                    item.get("entityUrn", "")
                    or item.get("savedEntity", {}).get("entityUrn", "")
                    or item.get("*savedEntity", "")
                )
                if not urn: continue

                # HIGH-PERFORMANCE OPTIMIZATION: Stop if we've reached a post from a previous run
                if urn in seen_urns:
                    log.info(f"  Reached known post {urn[:30]}... stopping fetch.")
                    already_seen_trigger = True
                    break

                endpoint_results.append({
                    "entity_urn": urn,
                    "post_urn": urn,
                })

            if already_seen_trigger or len(elements) < count: break
            start += count
            time.sleep(0.5)
        
        if endpoint_results or already_seen_trigger:
            log.info(f"  Found {len(endpoint_results)} new posts to process.")
            return endpoint_results

    # Fallback to HTML scrape if API fails
    log.info("  API endpoints yielded no new items. Trying HTML fallback...")
    return _try_fetch_saved_from_html(session, 0, 0, history)


def _try_fetch_saved(
    session: requests.Session,
    endpoint_template: str,
    cutoff_ms: int,
    lookback_hours: int,
) -> list[dict] | None:
    """Try fetching saved posts from a single endpoint. Returns None if endpoint fails."""
    saved_items = []
    start = 0
    count = 20

    while True:
        url = endpoint_template.format(count=count, start=start)

        try:
            resp = session.get(url, timeout=15)

            if resp.status_code in (401, 403):
                log.error(f"  Auth error ({resp.status_code}) — check li_at and JSESSIONID cookies")
                return None
            if resp.status_code == 404:
                log.debug(f"  404 — endpoint not found")
                return None
            if resp.status_code != 200:
                log.debug(f"  HTTP {resp.status_code}")
                return None

            data = resp.json()
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            log.debug(f"  Request failed: {e}")
            return None

        # Handle different response structures
        elements = (
            data.get("elements", [])
            or data.get("data", {}).get("saveDashSavesByAll", {}).get("elements", [])
            or data.get("data", {}).get("*elements", [])
            or data.get("included", [])
        )

        if not elements and start == 0:
            # First page empty — might be wrong endpoint or genuinely no saves
            if "elements" in str(data.keys()) or "data" in str(data.keys()):
                log.info(f"  Endpoint works but returned 0 saved items")
                return []
            return None

        if not elements:
            break

        for item in elements:
            # Prioritize created timestamp over saved timestamp
            created_at = (
                item.get("createdAt", 0)
                or item.get("savedEntity", {}).get("createdAt", 0)
                or item.get("lastModifiedAt", 0)
                or item.get("savedAt", 0)
            )

            if created_at and created_at < cutoff_ms:
                # Still continue if savedAt is newer, as we want to check all elements
                # But only log and stop if we are sure we've passed the creation cutoff
                # LinkedIn API usually returns items sorted by savedAt, not createdAt.
                # To be safe, we'll process all items in the first few pages.
                pass 

            # Extract whatever identifying info is available
            entity_urn = (
                item.get("entityUrn", "")
                or item.get("savedEntity", {}).get("entityUrn", "")
                or item.get("*savedEntity", "")
                or item.get("dashEntityUrn", "")
            )

            # Try to get post text directly from included data
            text = item.get("commentary", {}).get("text", "") if isinstance(item.get("commentary"), dict) else ""

            # Check if this post was created within our lookback window
            if created_at and created_at >= cutoff_ms:
                saved_item = {
                    "created_at": created_at,
                    "created_at_iso": (
                        datetime.fromtimestamp(created_at / 1000, tz=timezone.utc).isoformat()
                        if created_at else None
                    ),
                    "entity_urn": entity_urn,
                    "post_urn": entity_urn,
                    "text_preview": text[:200] if text else "",
                    "raw_data": item,
                }
                saved_items.append(saved_item)

        log.info(f"  Page {start // count + 1}: {len(elements)} elements | {len(saved_items)} match creation cutoff")
        start += count
        time.sleep(1)

    log.info(f"\nTotal saved posts in last {lookback_hours}h: {len(saved_items)}")
    return saved_items


def _try_fetch_saved_from_html(
    session: requests.Session,
    cutoff_ms: int,
    lookback_hours: int,
    history: dict = None
) -> list[dict]:
    """Fallback: try loading the saved posts HTML page and extracting URNs."""
    from bs4 import BeautifulSoup

    url = "https://www.linkedin.com/my-items/saved-posts/"
    try:
        resp = session.get(url, timeout=15, headers={"Accept": "text/html"})
        if resp.status_code != 200:
            return []

        # Look for activity URNs in the page source
        urns = re.findall(r"urn:li:activity:\d+", resp.text)
        urns = list(set(urns))  # Dedupe list of found URNs

        if not urns:
            return []

        seen_urns = set(history.get("contacted_urns", [])) if history else set()
        
        saved_items = []
        for urn in urns:
            # OPTIMIZATION: Skip if already contacted
            if urn in seen_urns:
                continue
                
            saved_items.append({
                "saved_at": 0,
                "saved_at_iso": None,
                "entity_urn": urn,
                "post_urn": urn,
                "text_preview": "",
                "raw_data": {},
            })

        log.info(f"  Found {len(urns)} post URNs on page, {len(saved_items)} are new.")
        return saved_items

    except Exception as e:
        log.debug(f"  HTML fallback failed: {e}")
        return []


def fetch_post_content(session: requests.Session, post_urn: str) -> tuple[str, int]:
    """
    Fetch the full text content and creation timestamp of a LinkedIn post.
    Returns (content_string, created_at_ms).
    """
    activity_id = None
    match = re.search(r"urn:li:activity:(\d+)", post_urn)
    if match: activity_id = match.group(1)
    if not activity_id:
        match = re.search(r"urn:li:share:(\d+)", post_urn)
        if match: activity_id = match.group(1)
    if not activity_id:
        match = re.search(r"urn:li:ugcPost:(\d+)", post_urn)
        if match: activity_id = match.group(1)

    if not activity_id:
        return "", 0

    url = (
        f"https://www.linkedin.com/voyager/api/feed/updates"
        f"?decorationId=com.linkedin.voyager.deco.feed.FeedUpdate-4"
        f"&q=activityByUrn"
        f"&activityUrn=urn%3Ali%3Aactivity%3A{activity_id}"
    )

    created_at = 0
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            url2 = f"https://www.linkedin.com/voyager/api/feed/updates/urn:li:activity:{activity_id}"
            resp = session.get(url2, timeout=15)
            if resp.status_code != 200:
                return "", 0

        data = resp.json()
        
        # Extract created_at from various possible fields in the detail response
        if "createdAt" in str(data):
            # Deep search for createdAt
            def find_created_at(obj):
                if isinstance(obj, dict):
                    if "createdAt" in obj: return obj["createdAt"]
                    for v in obj.values():
                        res = find_created_at(v)
                        if res: return res
                elif isinstance(obj, list):
                    for item in obj:
                        res = find_created_at(item)
                        if res: return res
                return None
            created_at = find_created_at(data) or 0

    except Exception as e:
        log.debug(f"  Error fetching post {activity_id}: {e}")
        return "", 0

    text_parts = []
    raw = json.dumps(data)

    def extract_texts(obj, depth=0):
        if depth > 10: return
        if isinstance(obj, dict):
            for key in ("text", "commentary", "translationText"):
                if key in obj and isinstance(obj[key], str) and len(obj[key]) > 20:
                    text_parts.append(obj[key])
            if "attributes" not in obj:
                for v in obj.values(): extract_texts(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj: extract_texts(item, depth + 1)

    extract_texts(data)

    emails = set(re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", raw))
    emails -= {"example@email.com", "noreply@linkedin.com", "user@example.com"}
    phones = set(re.findall(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", raw))
    intl_phones = set(re.findall(r"\+\d{1,3}[-.\s]?\d{4,5}[-.\s]?\d{4,6}", raw))
    all_phones = {p.strip() for p in (phones | intl_phones) if len(re.sub(r"\D", "", p)) >= 10}

    content = "\n\n".join(text_parts)
    if emails: content += f"\n\n[EMAILS FOUND: {', '.join(emails)}]"
    if all_phones: content += f"\n\n[PHONE NUMBERS FOUND: {', '.join(all_phones)}]"

    return content, created_at


def fetch_all_post_contents(session: requests.Session, saved_items: list[dict]) -> list[dict]:
    """Fetch full content for all provided items."""
    log.info(f"Fetching full content for {len(saved_items)} items...")

    filtered_items = []
    for i, item in enumerate(saved_items):
        post_urn = item.get("post_urn", "") or item.get("entity_urn", "")
        if post_urn:
            content, created_at = fetch_post_content(session, post_urn)
            if not content:
                log.warning(f"  [{i+1}/{len(saved_items)}] FAILED to fetch content for {post_urn[:50]}")
                continue

            item["full_content"] = content
            item["created_at"] = created_at
            item["created_at_iso"] = datetime.fromtimestamp(created_at/1000, tz=timezone.utc).isoformat() if created_at else None
            
            log.info(f"  [{i+1}/{len(saved_items)}] {len(content):>5d} chars | {post_urn[:50]}")
            filtered_items.append(item)
        else:
            log.info(f"  [{i+1}/{len(saved_items)}] No URN available")
        time.sleep(0.5)

    return filtered_items


# ═══════════════════════════════════════════════
# STEP 2: GEMINI EXTRACTION
# ═══════════════════════════════════════════════

def call_gemini(prompt: str, max_retries: int = 5) -> str:
    """Call Gemini API with retry on rate limit and automatic key rotation."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{CONFIG['GEMINI_MODEL']}:generateContent"
    )
    
    current_key = CONFIG["GEMINI_API_KEY"]
    alt_key = CONFIG["ALTERNATIVE_GEMINI_API_KEY"]
    using_alt = False

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                params={"key": current_key},
                json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192}},
                timeout=90,
            )

            if resp.status_code == 429:
                # If we have an alternative key and haven't used it yet, switch immediately
                if alt_key and not using_alt:
                    log.warning(f"  Quota reached for primary key. Switching to ALTERNATIVE_GEMINI_API_KEY...")
                    current_key = alt_key
                    using_alt = True
                    continue
                
                # Otherwise, wait and retry
                wait = 15 * (2 ** (attempt - 1))
                log.warning(f"  Rate limited (429). Retrying in {wait}s... ({attempt}/{max_retries})")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
            
        except Exception as e:
            if attempt == max_retries:
                log.error(f"  Gemini failed after {max_retries} attempts: {e}")
                return ""
            time.sleep(2)

    return ""


def extract_contacts_with_gemini(saved_items: list[dict]) -> list[dict]:
    """
    Use Gemini to extract emails, phone numbers, and role details
    from saved LinkedIn posts.
    """
    items_with_content = [s for s in saved_items if s.get("full_content")]
    if not items_with_content:
        log.info("No saved posts with content to analyze.")
        return []

    batch_size = 5
    all_extracted = []

    for batch_start in range(0, len(items_with_content), batch_size):
        batch = items_with_content[batch_start: batch_start + batch_size]

        posts_block = ""
        for idx, item in enumerate(batch):
            posts_block += f"""
===== Post {idx + 1} =====
Saved at: {item.get('saved_at_iso', 'unknown')}
URN: {item.get('post_urn', 'unknown')[:80]}

--- Content ---
{item.get('full_content', '')[:2500]}
"""

        prompt = f"""You are analyzing LinkedIn posts that a user has saved. These are posts the user found interesting — likely contract/freelance job opportunities in AI/ML.

For each post, extract ALL contact information and job details. Look very carefully for:
- Email addresses (sometimes obfuscated like "name [at] company [dot] com")
- Phone numbers (any format — US, international, with/without country code)
- The poster's full name
- Company hiring
- Role details

{posts_block}

Respond with ONLY a JSON array (no markdown, no commentary). Per post:
{{
    "index": <1-based>,
    "poster_name": "<full name or null>",
    "poster_email": "<email address or null>",
    "poster_phone": "<phone number or null>",
    "company": "<hiring company or null>",
    "role_title": "<role title or null>",
    "role_summary": "<1-line summary of what the post is about>",
    "rate_or_compensation": "<pay info or null>",
    "contact_method": "<how to apply: email / DM / link / phone / null>",
    "has_contact_info": true/false
}}"""

        batch_num = batch_start // batch_size + 1
        total_batches = (len(items_with_content) + batch_size - 1) // batch_size
        log.info(f"[Gemini {batch_num}/{total_batches}] Analyzing {len(batch)} posts...")

        raw = call_gemini(prompt)

        try:
            cleaned = re.sub(r"^```json\s*", "", raw.strip())
            cleaned = re.sub(r"\s*```$", "", cleaned)
            evaluations = json.loads(cleaned)

            for ev in evaluations:
                idx = ev.get("index", 0) - 1
                if 0 <= idx < len(batch):
                    enriched = {**batch[idx], **ev}
                    all_extracted.append(enriched)
                    email = ev.get("poster_email") or "no email"
                    phone = ev.get("poster_phone") or "no phone"
                    log.info(
                        f"  {(ev.get('poster_name') or '?')[:25]:25s} | "
                        f"email: {email:30s} | phone: {phone}"
                    )
        except json.JSONDecodeError as e:
            log.warning(f"  JSON parse error: {e}")
            log.debug(f"  Raw: {raw[:500]}")

        time.sleep(5)  # Free tier rate limit

    log.info(f"\nExtracted info from {len(all_extracted)} posts")
    return all_extracted


# ═══════════════════════════════════════════════
# STEP 3: HISTORY TRACKING
# ═══════════════════════════════════════════════

def load_history() -> dict:
    path = Path(CONFIG["HISTORY_FILE"])
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"contacted_urns": [], "contacted_emails": [], "contacted_details": []}


def save_history(history: dict):
    with open(CONFIG["HISTORY_FILE"], "w") as f:
        json.dump(history, f, indent=2, default=str)


def dedupe_against_history(results: list[dict], history: dict) -> list[dict]:
    seen_urns = set(history.get("contacted_urns", []))
    seen_emails = set(history.get("contacted_emails", []))

    fresh, skipped = [], 0
    for r in results:
        urn = r.get("post_urn", "") or r.get("entity_urn", "")
        email = r.get("poster_email", "")
        if urn in seen_urns or (email and email in seen_emails):
            skipped += 1
        else:
            fresh.append(r)

    if skipped:
        log.info(f"Skipped {skipped} already-contacted posts")
    return fresh


def record_contact(history: dict, urn: str, email: str = None, url: str = None):
    if urn and urn not in history["contacted_urns"]:
        history["contacted_urns"].append(urn)
        
        # Add structured detail with URL
        if "contacted_details" not in history:
            history["contacted_details"] = []
        
        history["contacted_details"].append({
            "urn": urn,
            "url": url,
            "email": email,
            "timestamp": datetime.now().isoformat()
        })

    if email and email not in history["contacted_emails"]:
        history["contacted_emails"].append(email)


# ═══════════════════════════════════════════════
# STEP 4: GMAIL
# ═══════════════════════════════════════════════

def get_gmail_service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    token_path = Path(CONFIG["GMAIL_TOKEN_FILE"])
    creds_path = Path(CONFIG["GMAIL_CREDENTIALS_FILE"])

    # CI/CD: load token from env var (raw JSON string)
    token_env = os.getenv("GMAIL_TOKEN_JSON")
    if token_env:
        token_path.write_text(token_env)
        log.info("Gmail token loaded from GMAIL_TOKEN_JSON env var")

    # CI/CD: load credentials.json from env var (raw JSON string)
    creds_env = os.getenv("GMAIL_CREDENTIALS_JSON")
    if creds_env and not creds_path.exists():
        creds_path.write_text(creds_env)
        log.info("Gmail credentials loaded from GMAIL_CREDENTIALS_JSON env var")

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), CONFIG["GMAIL_SCOPES"])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        else:
            if not creds_path.exists():
                log.error(f"Missing {creds_path}. Download from GCP Console -> OAuth 2.0 Client.")
                log.error("For GitHub Actions, set GMAIL_CREDENTIALS_JSON and GMAIL_TOKEN_JSON secrets.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), CONFIG["GMAIL_SCOPES"])
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    service = build("gmail", "v1", credentials=creds)

    if not CONFIG["SENDER_EMAIL"]:
        profile = service.users().getProfile(userId="me").execute()
        CONFIG["SENDER_EMAIL"] = profile["emailAddress"]
        log.info(f"Sender: {CONFIG['SENDER_EMAIL']}")

    return service


def compose_email(to_email: str, recipient_name: str, role_title: str) -> str:
    msg = MIMEMultipart()
    msg["From"] = f"{CONFIG['SENDER_NAME']} <{CONFIG['SENDER_EMAIL']}>"
    msg["To"] = to_email
    msg["Subject"] = EMAIL_TEMPLATE["subject"]

    body = EMAIL_TEMPLATE["body"].format(
        # recipient_name=recipient_name or "there",
        role_title=role_title or "AI/ML Engineer",
        sender_name=CONFIG["SENDER_NAME"],
    )
    msg.attach(MIMEText(body, "plain"))

    resume = Path(CONFIG["RESUME_PATH"])
    if resume.exists():
        with open(resume, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={resume.name}")
            msg.attach(part)
    else:
        log.warning(f"Resume not found at {resume} — sending without attachment.")

    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def send_one_email(service, to_email: str, name: str, role_title: str) -> dict:
    raw = compose_email(to_email, name, role_title)
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_leads_summary_email(service, phone_leads: list[dict]):
    """Send a summary of all phone leads found in this run to the user's email."""
    if not phone_leads:
        return

    log.info(f"Sending summary of {len(phone_leads)} phone leads to {CONFIG['SENDER_EMAIL']}...")

    msg = MIMEMultipart()
    msg["From"] = f"{CONFIG['SENDER_NAME']} <{CONFIG['SENDER_EMAIL']}>"
    msg["To"] = CONFIG["SENDER_EMAIL"]
    msg["Subject"] = f"LinkedIn Phone Leads Summary - {datetime.now().strftime('%Y-%m-%d')}"

    body_lines = [
        f"Hi {CONFIG['SENDER_NAME']},\n",
        f"Identified {len(phone_leads)} phone leads in the latest LinkedIn saved posts run:\n",
        "-" * 60
    ]

    for i, lead in enumerate(phone_leads, 1):
        name = lead.get("poster_name") or "Unknown"
        phone = lead.get("poster_phone") or "No Phone"
        email = lead.get("poster_email") or "No Email"
        role = lead.get("role_title") or lead.get("role_summary", "")[:100]
        company = lead.get("company") or "Unknown Company"
        urn = lead.get("post_urn") or lead.get("entity_urn", "")
        url = f"https://www.linkedin.com/feed/update/{urn}" if urn else "No URL"

        body_lines.append(f"{i}. {name} ({company})")
        body_lines.append(f"   Phone: {phone}")
        body_lines.append(f"   Email: {email}")
        body_lines.append(f"   Role:  {role}")
        body_lines.append(f"   Link:  {url}")
        body_lines.append("-" * 60)

    body_lines.append("\nBest regards,\nYour Outreach Agent")
    body = "\n".join(body_lines)
    msg.attach(MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info("Summary email sent successfully.")


# ═══════════════════════════════════════════════
# STEP 5: AUTO-SEND & PHONE LEADS
# ═══════════════════════════════════════════════

def save_phone_leads(results: list[dict]):
    phone_leads = [r for r in results if r.get("poster_phone")]
    if not phone_leads:
        return

    path = Path(CONFIG["PHONE_LEADS_FILE"])
    existing = []
    if path.exists():
        with open(path) as f:
            existing = json.load(f)

    # Load history for final deduplication guard
    history = load_history()
    seen_urns = set(history.get("contacted_urns", []))
    existing_urns = {e.get("post_urn") or e.get("entity_urn") for e in existing} | seen_urns
    
    new_leads = []
    for r in phone_leads:
        urn = r.get("post_urn") or r.get("entity_urn")
        if urn not in existing_urns:
            r["saved_to_leads_at"] = datetime.now().isoformat()
            new_leads.append(r)

    if new_leads:
        existing.extend(new_leads)
        with open(path, "w") as f:
            json.dump(existing, f, indent=2, default=str)
        log.info(f"\n  Saved {len(new_leads)} new phone leads to {path}")


def auto_send(results: list[dict], dry_run: bool = False) -> list[dict]:
    """Auto-send emails to posts with emails. Save phone leads separately."""
    # Final guard: Only process items not in history
    history = load_history()
    results = dedupe_against_history(results, history)
    
    if not results:
        log.info("  No new leads to contact (all deduped against history).")
        return []

    save_phone_leads(results)

    phone_leads = [r for r in results if r.get("poster_phone")]
    with_email = [r for r in results if r.get("poster_email")]
    without_email = [r for r in results if not r.get("poster_email")]

    log.info(f"\n  {len(with_email)} with email (will send)")
    log.info(f"  {len(without_email)} without email (skipped)")
    log.info(f"  {len(phone_leads)} with phone (summary will be sent to self)")

    if dry_run:
        log.info("\n  [DRY RUN] Would send emails to:")
        for r in with_email:
            log.info(f"    {r.get('poster_name','?')} <{r['poster_email']}> — {r.get('role_title','')[:50]}")
        if phone_leads:
            log.info(f"  [DRY RUN] Would send phone leads summary to {CONFIG['SENDER_EMAIL']}")
        return []

    gmail = get_gmail_service()

    # 1. Send phone leads summary to self
    if phone_leads:
        try:
            send_leads_summary_email(gmail, phone_leads)
        except Exception as e:
            log.error(f"  Failed to send phone leads summary to self: {e}")

    # 2. Send outreach emails to candidates
    if not with_email:
        return []

    emailed = []
    for r in with_email:
        try:
            send_one_email(
                gmail, r["poster_email"],
                r.get("poster_name", ""), r.get("role_title", ""),
            )
            log.info(f"  Sent -> {r['poster_email']} ({r.get('poster_name', '?')})")
            urn = r.get("post_urn") or r.get("entity_urn", "")
            url = f"https://www.linkedin.com/feed/update/{urn}" if urn else ""
            record_contact(history, urn, r["poster_email"], url)
            emailed.append(r)
            time.sleep(1)
        except Exception as e:
            log.error(f"  FAILED {r.get('poster_email', '?')}: {e}")

    save_history(history)
    log.info(f"\n  Emails sent: {len(emailed)}/{len(with_email)}")
    return emailed


# ═══════════════════════════════════════════════
# STEP 6: SAVE RESULTS
# ═══════════════════════════════════════════════

def save_run(saved: list, extracted: list, emailed: list):
    out_dir = Path(CONFIG["RESULTS_DIR"])
    out_dir.mkdir(exist_ok=True)

    phone_count = len([r for r in extracted if r.get("poster_phone")])
    email_count = len([r for r in extracted if r.get("poster_email")])

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    data = {
        "timestamp": datetime.now().isoformat(),
        "lookback_hours": CONFIG["LOOKBACK_HOURS"],
        "stats": {
            "saved_posts_fetched": len(saved),
            "contacts_extracted": len(extracted),
            "with_email": email_count,
            "with_phone": phone_count,
            "emailed": len(emailed),
        },
        "extracted": extracted,
    }

    # Remove raw_data to keep file size reasonable
    for item in data["extracted"]:
        item.pop("raw_data", None)

    path = out_dir / f"saved_run_{ts}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    log.info(f"Results saved: {path}")


# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="LinkedIn Saved Posts Outreach Agent")
    parser.add_argument("--dry-run", action="store_true", help="Skip sending emails")
    parser.add_argument("--hours", type=int, default=48, help="Lookback hours (default: 48)")
    parser.add_argument("--resume", type=str, help="Path to resume PDF")
    args = parser.parse_args()

    if args.resume:
        CONFIG["RESUME_PATH"] = args.resume
    CONFIG["LOOKBACK_HOURS"] = args.hours

    # Validate
    missing = [k for k, v in CONFIG.items() if isinstance(v, str) and v.startswith("YOUR_")]
    if missing:
        log.error(f"Set these in CONFIG or as env vars: {', '.join(missing)}")
        log.error("  LINKEDIN_LI_AT      — from browser cookies (F12 → Application → Cookies)")
        log.error("  LINKEDIN_JSESSIONID — from browser cookies (same place, copy JSESSIONID)")
        log.error("  GEMINI_API_KEY      — from aistudio.google.com")
        sys.exit(1)

    mode = "dry-run" if args.dry_run else "full"
    print(f"\n{'='*70}")
    print(f"  LinkedIn Saved Posts Outreach Agent")
    print(f"  Lookback: {CONFIG['LOOKBACK_HOURS']}h  |  Mode: {mode}")
    print(f"{'='*70}")

    # ── 1. Fetch saved posts (Optimization: stops when it hits history) ──
    log.info("\n[STEP 1] Fetching saved LinkedIn posts...")
    session = create_linkedin_session()
    history = load_history()
    saved = fetch_saved_posts(session, history)

    # Final check before heavy content fetching
    saved = dedupe_against_history(saved, history)

    if not saved:
        log.info("No new saved posts found (all recent posts already in history). Optimization complete.")
        return

    # ── 2. Fetch full content ──
    log.info("\n[STEP 2] Fetching full post content for new leads...")
    saved = fetch_all_post_contents(session, saved)

    # ── 3. Gemini extraction ──
    log.info("\n[STEP 3] Extracting contacts with Gemini...")
    extracted = extract_contacts_with_gemini(saved)

    if not extracted:
        log.info("No contacts extracted from saved posts.")
        save_run(saved, [], [])
        return

    # ── 5. Auto-send ──
    log.info("\n[STEP 4] Auto-send emails & save phone leads...")
    emailed = auto_send(extracted, dry_run=args.dry_run)

    # ── 6. Save ──
    save_run(saved, extracted, emailed)

    phone_count = len([r for r in extracted if r.get("poster_phone")])
    email_count = len([r for r in extracted if r.get("poster_email")])
    print(f"\n{'='*70}")
    print(f"  RUN COMPLETE")
    print(f"  Saved posts:  {len(saved)}")
    print(f"  With email:   {email_count} (sent: {len(emailed)})")
    print(f"  With phone:   {phone_count} (saved to {CONFIG['PHONE_LEADS_FILE']})")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()

