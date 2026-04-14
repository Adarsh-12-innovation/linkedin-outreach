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
import subprocess
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

import requests
import phonenumbers
from urllib.parse import quote

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
    "GEMINI_API_KEY_1": os.getenv("GEMINI_API_KEY_1", os.getenv("GEMINI_API_KEY", "")),
    "GEMINI_API_KEY_2": os.getenv("GEMINI_API_KEY_2", os.getenv("ALTERNATIVE_GEMINI_API_KEY", "")),
    "GEMINI_API_KEY_3": os.getenv("GEMINI_API_KEY_3", os.getenv("SECOND_ALTERNATIVE_GEMINI_API_KEY", "")),
    "GEMINI_API_KEY_4": os.getenv("GEMINI_API_KEY_4", ""),
    "GEMINI_API_KEY_5": os.getenv("GEMINI_API_KEY_5", ""),
    "GEMINI_API_KEY_6": os.getenv("GEMINI_API_KEY_6", ""),
    "GEMINI_MODEL": "gemini-2.5-flash-lite",

    # Gmail OAuth2
    "GMAIL_CREDENTIALS_FILE": "credentials.json",
    "GMAIL_TOKEN_FILE": "token.json",
    "GMAIL_SCOPES": [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly"
    ],

    # Your details
    "SENDER_NAME": "Adarsh Bansal",
    "SENDER_EMAIL": "adarshbansal1995@gmail.com",  # Auto-detected from Gmail auth

    # Resume
    "RESUME_PATH": os.getenv("RESUME_PATH", "Adarsh Bansal_CV_2026.pdf"),
    "RESUME_URL_PREFIX": "https://github.com/Adarsh-12-innovation/linkedin-outreach/raw/main/",

    # Keyword-based Resume Mapping
    # Format in .env: RESUME_MAPPING='{"python, ai, ml": "resume_ai.pdf", "data, analytics": "resume_data.pdf"}'
    "RESUME_MAPPING": json.loads(os.getenv("RESUME_MAPPING", "{}")),

    # Time window
    "LOOKBACK_HOURS": 48,

    # Tracking
    "HISTORY_FILE": "outreach_history.json",
    "PHONE_LEADS_FILE": "phone_leads.json",
    "RESULTS_DIR": "results",
    "STALE_QUERYID_NOTIFIED_FILE": ".queryid_stale_notified",
    "EXCLUDED_DOMAINS": [d.strip().lower() for d in os.getenv("EXCLUDED_DOMAINS", "").split(",") if d.strip()]
}

# ─────────────────────────────────────────────
# EMAIL TEMPLATE
# ─────────────────────────────────────────────

EMAIL_TEMPLATE = {
    "subject": "Application AI/ML Engineer — Available for Contract roles",
    "body": """\
Hello {recipient_name},

I came across your recent post on LinkedIn about the {role_title} role and wanted to reach out.

Post link: {post_url}

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

# Module-level flag — set by _try_fetch_saved_via_graphql when queryId returns 400/404
_GRAPHQL_QUERYID_STALE = False


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


def _normalize_urns_to_activity(raw_urns: set) -> set:
    """
    Normalize LinkedIn URNs so that the same post isn't counted twice.

    LinkedIn represents a single post under multiple URN types that share
    the same numeric ID:
        urn:li:activity:7445700967880122369
        urn:li:ugcPost:7445700967880122369
        urn:li:share:7445700967880122369
        urn:li:fs_updateV2:(urn:li:activity:7445700967880122369,...)

    We canonicalize everything to ``urn:li:activity:<id>`` because that's
    what ``fetch_post_content`` expects downstream.  Any URN whose numeric
    ID already appears under an ``activity`` prefix is dropped.
    """
    id_to_urn = {}
    for urn in raw_urns:
        # Extract the bare numeric ID regardless of prefix
        m = re.search(r"(\d{10,})", urn)
        if not m:
            continue
        numeric_id = m.group(1)

        existing = id_to_urn.get(numeric_id)
        # Prefer activity URN; if we already have one, skip duplicates
        if existing and existing.startswith("urn:li:activity:"):
            continue
        # Store the canonical activity form
        id_to_urn[numeric_id] = f"urn:li:activity:{numeric_id}"

    return set(id_to_urn.values())


def _normalize_seen_urns(seen_urns: set) -> set:
    """
    Expand history URNs so that *any* URN variant of an already-contacted
    post is recognized as seen.  Returns a set of bare numeric IDs.
    """
    ids = set()
    for urn in seen_urns:
        m = re.search(r"(\d{10,})", urn)
        if m:
            ids.add(m.group(1))
    return ids


def fetch_saved_posts(session: requests.Session, history: dict = None) -> list[dict]:
    """
    Fetch saved items from LinkedIn.
    Uses aggressive multi-format URN extraction and Deep JSON Inspection.
    """
    log.info("Fetching saved posts list (IDs only)...")
    
    seen_urns = set(history.get("contacted_urns", [])) if history else set()
    seen_ids = _normalize_seen_urns(seen_urns)   # bare numeric IDs for cross-format dedup
    all_new_results = {}
    
    # Broad set of endpoints including modern Dash and Identity endpoints
    endpoints = [
        "https://www.linkedin.com/voyager/api/myItems/savedPosts?count={count}&start={start}",
        "https://www.linkedin.com/voyager/api/identity/dash/savedItems?count={count}&q=savedByMe&start={start}",
        "https://www.linkedin.com/voyager/api/voyagerContentDashSaves?count={count}&start={start}&q=savedByMe",
        "https://www.linkedin.com/voyager/api/saveDashSaves?count={count}&start={start}",
    ]

    for endpoint_template in endpoints:
        log.info(f"  Checking endpoint: {endpoint_template[:60]}...")
        start = 0
        count = 50
        endpoint_new_count = 0
        endpoint_total_found = 0

        while True:
            url = endpoint_template.format(count=count, start=start)
            try:
                resp = session.get(url, timeout=15)
                if resp.status_code in (401, 403):
                    log.error(f"  Auth error ({resp.status_code}) — li_at/JSESSIONID likely expired.")
                    send_linkedin_auth_error_notification(resp.status_code)
                    return [] # Stop this run
                if resp.status_code != 200:
                    break
                data = resp.json()
            except:
                break

            # AGGRESSIVE URN EXTRACTION
            found_urns_in_page = []
            
            def find_urns(obj):
                if isinstance(obj, str):
                    # Match common LinkedIn URN patterns
                    # activity, share, ugcPost, fs_updateV2
                    matches = re.findall(r"urn:li:(?:activity|share|ugcPost|fs_updateV2):[\d\(\)a-zA-Z0-9_-]+", obj)
                    if matches: found_urns_in_page.extend(matches)
                elif isinstance(obj, dict):
                    for v in obj.values(): find_urns(v)
                elif isinstance(obj, list):
                    for i in obj: find_urns(i)

            find_urns(data)
            # Normalize to canonical activity URNs (dedup across urn types)
            page_urns = list(_normalize_urns_to_activity(set(found_urns_in_page)))
            
            if not page_urns:
                break

            endpoint_total_found += len(page_urns)
            for urn in page_urns:
                numeric_id = re.search(r"(\d{10,})", urn).group(1)
                if numeric_id not in seen_ids and urn not in all_new_results:
                    all_new_results[urn] = {
                        "entity_urn": urn,
                        "post_urn": urn,
                    }
                    endpoint_new_count += 1

            if len(page_urns) < 3 or start > 200:
                break
            
            start += count
            time.sleep(0.5)
        
        log.info(f"  Endpoint summary: {endpoint_total_found} total posts found, {endpoint_new_count} were new.")

    final_results = list(all_new_results.values())
    log.info(f"Total unique new posts discovered: {len(final_results)}")

    # ── Try GraphQL endpoint (modern LinkedIn frontend uses this) ──
    if not final_results:
        log.info("  REST APIs yielded nothing. Trying GraphQL saved-items endpoints...")
        graphql_results = _try_fetch_saved_via_graphql(session, seen_ids)
        if graphql_results:
            for urn, item in graphql_results.items():
                if urn not in all_new_results:
                    all_new_results[urn] = item
            final_results = list(all_new_results.values())
            log.info(f"  GraphQL yielded {len(graphql_results)} new posts.")

    if not final_results:
        log.info("  All API endpoints yielded nothing. Trying HTML fallback with pagination...")
        return _try_fetch_saved_from_html(session, 0, 0, history)

    return final_results


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


def _try_fetch_saved_via_graphql(
    session: requests.Session,
    seen_ids: set,
) -> dict:
    """
    Fetch saved posts via LinkedIn's GraphQL search-clusters endpoint.
    
    LinkedIn treats "My Saved Posts" as a search query internally.
    The endpoint uses cursor-based pagination: each response includes a
    ``paginationToken`` that must be passed to the next request.
    
    Page 1:  variables=(start:0,query:(flagshipSearchIntent:SEARCH_MY_ITEMS_SAVED_POSTS))
    Page 2+: variables=(start:N,paginationToken:<base64>,query:(...))
    
    The queryId rotates every 4-8 weeks on LinkedIn redeploys.
    When it breaks (400/404), grab the new one from DevTools:
        /my-items/saved-posts/ → Network → filter "graphql" → scroll down
    
    Args:
        seen_ids: Set of bare numeric IDs already contacted (for cross-format dedup).
    
    Returns dict of {urn: item_dict} for new (unseen) posts.
    """
    graphql_base = "https://www.linkedin.com/voyager/api/graphql"
    
    # Actual queryId from LinkedIn's saved-posts page (as of Apr 2026).
    # When this stops working (400/404), update via DevTools (see docstring).
    known_query_ids = [
        "voyagerSearchDashClusters.05111e1b90ee7fea15bebe9f9410ced9",
    ]
    
    search_intent = "SEARCH_MY_ITEMS_SAVED_POSTS"
    page_size = 10     # LinkedIn uses 10 per page for saved posts
    max_pages = 3     # Safety cap: 200 items max
    
    all_new = {}

    for query_id in known_query_ids:
        start = 0
        pagination_token = None
        endpoint_found_any = False
        
        for page_num in range(max_pages):
            # Build variables — first page has no paginationToken
            if pagination_token:
                # Only encode '=' in the base64 token (as LinkedIn expects %3D)
                encoded_token = pagination_token.replace("=", "%3D")
                variables = (
                    f"(start:{start},"
                    f"paginationToken:{encoded_token},"
                    f"query:(flagshipSearchIntent:{search_intent}))"
                )
            else:
                variables = (
                    f"(start:{start},"
                    f"query:(flagshipSearchIntent:{search_intent}))"
                )
            
            url = f"{graphql_base}?variables={variables}&queryId={query_id}"
            
            try:
                resp = session.get(url, timeout=15)
                if resp.status_code in (400, 404):
                    if not endpoint_found_any:
                        global _GRAPHQL_QUERYID_STALE
                        _GRAPHQL_QUERYID_STALE = True
                        log.warning(f"  GraphQL queryId stale ({resp.status_code}). "
                                    f"Update from DevTools: /my-items/saved-posts/ → Network → scroll")
                    break
                if resp.status_code in (401, 403):
                    log.debug(f"  GraphQL auth error ({resp.status_code})")
                    break
                if resp.status_code != 200:
                    break

                data = resp.json()
            except Exception as e:
                log.debug(f"  GraphQL request failed: {e}")
                break

            # ── Extract URNs from this page (TARGETED, not full sweep) ──
            # LinkedIn's search-clusters response contains the actual saved
            # posts in trackingUrn / navigationUrl fields.  The rest of the
            # JSON (especially `included`) has reshares, originals, profiles
            # etc. that inflate the count if we regex-sweep everything.
            targeted_urns = set()
            
            def extract_saved_post_urns(obj):
                """Walk the JSON and pull URNs only from fields that identify
                the actual saved post, not related/embedded content."""
                if isinstance(obj, dict):
                    # trackingUrn — primary: identifies the tracked search result
                    tracking = obj.get("trackingUrn", "")
                    if isinstance(tracking, str) and "activity" in tracking:
                        m = re.search(r"urn:li:activity:\d+", tracking)
                        if m:
                            targeted_urns.add(m.group(0))
                    
                    # navigationUrl — secondary: /feed/update/urn:li:activity:123
                    nav_url = obj.get("navigationUrl", "")
                    if isinstance(nav_url, str) and "/feed/update/" in nav_url:
                        m = re.search(r"urn:li:activity:\d+", nav_url)
                        if m:
                            targeted_urns.add(m.group(0))

                    for v in obj.values():
                        extract_saved_post_urns(v)
                elif isinstance(obj, list):
                    for item in obj:
                        extract_saved_post_urns(item)

            extract_saved_post_urns(data)
            page_urns = _normalize_urns_to_activity(targeted_urns)

            # Fallback: if targeted extraction found nothing but the response
            # has data, try a full sweep (handles unexpected response shapes)
            if not page_urns:
                raw_text = json.dumps(data)
                raw_urns = set(re.findall(
                    r"urn:li:(?:activity|share|ugcPost|fs_updateV2):[\d\(\)a-zA-Z0-9_-]+",
                    raw_text
                ))
                page_urns = _normalize_urns_to_activity(raw_urns)
                if page_urns:
                    log.debug(f"  Targeted extraction found 0, full sweep found {len(page_urns)}")

            if not page_urns:
                if not endpoint_found_any:
                    break  # Wrong queryId — no results at all
                log.info(f"  GraphQL pagination exhausted at page {page_num + 1}.")
                break  # Pagination done

            endpoint_found_any = True
            new_on_page = 0
            for urn in page_urns:
                numeric_id = re.search(r"(\d{10,})", urn).group(1)
                if numeric_id not in seen_ids and urn not in all_new:
                    all_new[urn] = {
                        "entity_urn": urn,
                        "post_urn": urn,
                    }
                    new_on_page += 1

            log.info(f"  GraphQL page {page_num + 1}: "
                     f"{len(page_urns)} posts, {new_on_page} new")

            # ── Extract paginationToken for next page ──
            # LinkedIn embeds it in the response metadata. We search recursively
            # because the nesting depth varies across LinkedIn deploys.
            next_token = None
            def find_pagination_token(obj):
                nonlocal next_token
                if next_token:
                    return
                if isinstance(obj, dict):
                    if "paginationToken" in obj and isinstance(obj["paginationToken"], str):
                        next_token = obj["paginationToken"]
                        return
                    for v in obj.values():
                        find_pagination_token(v)
                elif isinstance(obj, list):
                    for item in obj:
                        find_pagination_token(item)
            
            find_pagination_token(data)
            
            if not next_token:
                log.info(f"  No paginationToken in response — last page reached.")
                break
            
            pagination_token = next_token
            start += page_size
            time.sleep(0.7)

        if endpoint_found_any:
            log.info(f"  GraphQL fetched {len(all_new)} total new posts.")
            # queryId is still valid — clear any prior stale notification flag
            stale_path = Path(CONFIG["STALE_QUERYID_NOTIFIED_FILE"])
            if stale_path.exists():
                stale_path.unlink()
            break  # Found a working queryId

    return all_new


def _try_fetch_saved_from_html(
    session: requests.Session,
    cutoff_ms: int,
    lookback_hours: int,
    history: dict = None
) -> list[dict]:
    """
    Paginated HTML fallback for fetching saved posts.
    
    Strategy (3 phases):
      Phase 1 — Load the saved-posts HTML page; extract URNs from both the
                visible DOM *and* LinkedIn's embedded <code> data tags which
                often contain the full first-page API payload.
      Phase 2 — Make paginated Voyager REST / GraphQL API calls to fetch
                subsequent pages of saved items (the same calls LinkedIn's
                frontend makes on scroll).
      Phase 3 — Deduplicate against history and return results.
    
    All URNs are normalized to ``urn:li:activity:<id>`` so that the same
    post appearing as activity/ugcPost/share is only counted once.
    """
    from bs4 import BeautifulSoup

    seen_urns = set(history.get("contacted_urns", [])) if history else set()
    seen_ids = _normalize_seen_urns(seen_urns)
    raw_found_urns = set()  # Collects all URN variants before normalization

    # ── Phase 1: Load initial HTML page ──────────────────────────────────
    url = "https://www.linkedin.com/my-items/saved-posts/"
    urn_pattern = r"urn:li:(?:activity|ugcPost|share|fs_updateV2):\d+"
    try:
        resp = session.get(url, timeout=20, headers={"Accept": "text/html"})
        if resp.status_code != 200:
            log.debug(f"  HTML page returned {resp.status_code}")
            return []

        html_text = resp.text

        # 1a. Regex sweep across raw HTML for all URN types
        raw_found_urns.update(re.findall(urn_pattern, html_text))

        # 1b. Parse embedded <code> data tags (LinkedIn hides API payloads here)
        #     These tags have ids like "bpr-guid-*" and contain JSON blobs with
        #     the actual Voyager response data, often including URNs from the
        #     full initial fetch (which may exceed visible DOM items).
        soup = BeautifulSoup(html_text, "html.parser")
        for code_tag in soup.find_all("code", id=True):
            try:
                code_text = code_tag.get_text()
                if not code_text or len(code_text) < 20:
                    continue
                raw_found_urns.update(re.findall(urn_pattern, code_text))

                # Try to parse JSON and extract URNs from nested structures
                try:
                    embedded_data = json.loads(code_text)
                    included = embedded_data.get("included", [])
                    if isinstance(included, list):
                        for item in included:
                            if isinstance(item, dict):
                                for val in item.values():
                                    if isinstance(val, str):
                                        raw_found_urns.update(re.findall(urn_pattern, val))
                except (json.JSONDecodeError, AttributeError):
                    pass
            except Exception:
                continue

        # Normalize: collapse activity/ugcPost/share variants → single activity URN per post
        all_found_urns = _normalize_urns_to_activity(raw_found_urns)

        # Count how many are genuinely unseen
        found_ids = {re.search(r"(\d{10,})", u).group(1) for u in all_found_urns
                     if re.search(r"(\d{10,})", u)}
        new_count = len(found_ids - seen_ids)
        log.info(f"  Found {len(all_found_urns)} unique posts on page (HTML + embedded data),"
                 f" {new_count} are new.")

    except Exception as e:
        log.debug(f"  HTML page load failed: {e}")
        return []

    # ── Phase 2: Paginate via API calls ──────────────────────────────────
    #    LinkedIn's saved-posts page loads ~10 items initially and fetches
    #    more via Voyager API on scroll. We replicate those calls.
    page_size = 20
    max_pages = 10  # Safety cap: up to ~200 additional items
    initial_count = len(all_found_urns)

    # Endpoints to try for pagination (REST + GraphQL variants)
    pagination_endpoints = [
        # REST endpoints with savedByMe qualifier
        "https://www.linkedin.com/voyager/api/saveDashSaves?q=savedByMe&count={count}&start={start}",
        "https://www.linkedin.com/voyager/api/voyagerContentDashSaves?q=savedByMe&count={count}&start={start}",
        "https://www.linkedin.com/voyager/api/identity/dash/savedItems?q=savedByMe&count={count}&start={start}",
        # Original endpoints (may work with different start offsets)
        "https://www.linkedin.com/voyager/api/myItems/savedPosts?count={count}&start={start}",
    ]

    for endpoint_template in pagination_endpoints:
        start = initial_count  # Begin after what HTML gave us
        page_yielded_new = False

        for page_num in range(max_pages):
            api_url = endpoint_template.format(count=page_size, start=start)
            try:
                api_resp = session.get(api_url, timeout=15)
                if api_resp.status_code != 200:
                    break
                data = api_resp.json()
            except Exception:
                break

            # Extract + normalize URNs from JSON response
            raw_text = json.dumps(data)
            page_raw_urns = set(re.findall(urn_pattern, raw_text))
            page_urns = _normalize_urns_to_activity(page_raw_urns)
            new_urns = page_urns - all_found_urns

            if not page_urns:
                break  # Empty page = endpoint exhausted or wrong

            if new_urns:
                all_found_urns.update(new_urns)
                page_yielded_new = True
                log.info(f"  API pagination page {page_num + 1}: "
                         f"+{len(new_urns)} new posts (total: {len(all_found_urns)})")

            # Check if we've exhausted all pages
            paging = data.get("paging", {})
            total = paging.get("total", 0)
            elements = data.get("elements", [])

            if total and start + page_size >= total:
                break
            if len(elements) < page_size and len(page_urns) < 3:
                break

            start += page_size
            time.sleep(0.5)

        if page_yielded_new:
            log.info(f"  Pagination via {endpoint_template[:60]}... yielded results.")
            break  # Found a working endpoint, stop trying others

    new_total = len(all_found_urns) - initial_count
    if new_total > 0:
        log.info(f"  Pagination added {new_total} posts beyond initial HTML page.")

    # ── Phase 3: Deduplicate against history and build result list ────────
    saved_items = []
    for urn in all_found_urns:
        numeric_id = re.search(r"(\d{10,})", urn)
        if numeric_id and numeric_id.group(1) in seen_ids:
            continue
        saved_items.append({
            "saved_at": 0,
            "saved_at_iso": None,
            "entity_urn": urn,
            "post_urn": urn,
            "text_preview": "",
            "raw_data": {},
        })

    log.info(f"  Total: {len(all_found_urns)} URNs found, {len(saved_items)} are new (unseen).")
    return saved_items


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
        if resp.status_code in (401, 403):
            log.error(f"  Auth error ({resp.status_code}) fetching post content — session likely expired.")
            send_linkedin_auth_error_notification(resp.status_code)
            return "", 0
        if resp.status_code != 200:
            url2 = f"https://www.linkedin.com/voyager/api/feed/updates/urn:li:activity:{activity_id}"
            resp = session.get(url2, timeout=15)
            if resp.status_code in (401, 403):
                send_linkedin_auth_error_notification(resp.status_code)
                return "", 0
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
    
    # FORBIDDEN keys that usually contain comments, social details, or metadata
    FORBIDDEN_KEYS = {
        "socialDetail", "socialContent", "comments", "actions", 
        "updateAction", "socialDetailEntity", "attributes", "reactions",
        "followingInfo", "tracking", "footer", "feedbackDetail", "header"
    }

    def extract_texts(obj, depth=0):
        if depth > 15: return
        if isinstance(obj, dict):
            # Check for main text fields
            for key in ("text", "commentary", "translationText"):
                val = obj.get(key)
                if isinstance(val, str) and len(val) > 10:
                    text_parts.append(val)
                elif isinstance(val, dict) and "text" in val: # Handle commentary: {text: "..."}
                    if isinstance(val["text"], str) and len(val["text"]) > 10:
                        text_parts.append(val["text"])
            
            # Recurse, but skip forbidden keys
            for k, v in obj.items():
                if k not in FORBIDDEN_KEYS:
                    extract_texts(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                extract_texts(item, depth + 1)

    extract_texts(data)

    content = "\n\n".join(text_parts)

    # NOW run regex on the assembled content (main post ONLY), NOT raw JSON
    emails = set(re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", content))
    emails -= {"example@email.com", "noreply@linkedin.com", "user@example.com"}
    phones = set(re.findall(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", content))
    intl_phones = set(re.findall(r"\+\d{1,3}[-.\s]?\d{4,5}[-.\s]?\d{4,6}", content))
    all_phones = {p.strip() for p in (phones | intl_phones) if len(re.sub(r"\D", "", p)) >= 10}

    if emails: content += f"\n\n[EMAILS FOUND IN POST: {', '.join(emails)}]"
    if all_phones: content += f"\n\n[PHONE NUMBERS FOUND IN POST: {', '.join(all_phones)}]"

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

def call_gemini(prompt: str) -> str:
    """
    Call Gemini API with specialized retry/rotation logic:
    - 6 keys with 1 retry each (2 attempts per key).
    If all fail with 429, send an email notification.
    """
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{CONFIG['GEMINI_MODEL']}:generateContent"
    )
    
    keys = [
        {"key": CONFIG["GEMINI_API_KEY_1"], "retries": 1, "name": "Key 1"},
        {"key": CONFIG["GEMINI_API_KEY_2"], "retries": 1, "name": "Key 2"},
        {"key": CONFIG["GEMINI_API_KEY_3"], "retries": 1, "name": "Key 3"},
        {"key": CONFIG["GEMINI_API_KEY_4"], "retries": 1, "name": "Key 4"},
        {"key": CONFIG["GEMINI_API_KEY_5"], "retries": 1, "name": "Key 5"},
        {"key": CONFIG["GEMINI_API_KEY_6"], "retries": 1, "name": "Key 6"},
    ]

    for k_info in keys:
        current_key = k_info["key"]
        if not current_key or current_key.startswith("YOUR_"):
            continue

        max_attempts = k_info["retries"] + 1
        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    params={"key": current_key},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.1,
                            "maxOutputTokens": 64000,
                            "response_mime_type": "application/json"
                        }
                    },
                    timeout=90,
                )

                if resp.status_code == 429:
                    if attempt < max_attempts:
                        wait = 10 * attempt
                        log.warning(f"  {k_info['name']} Key: Rate limited (429). Retry {attempt}/{k_info['retries']} in {wait}s...")
                        time.sleep(wait)
                        continue
                    else:
                        log.warning(f"  {k_info['name']} Key: Exhausted all retries.")
                        break # Switch to next key

                resp.raise_for_status()
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
                
            except Exception as e:
                log.error(f"  {k_info['name']} Key Error: {e}")
                if attempt == max_attempts:
                    break
                time.sleep(2)

    # If we reach here, all keys failed
    log.critical("ALL Gemini API keys failed or were rate limited.")
    try:
        send_rate_limit_notification()
    except:
        pass
    return ""


def send_rate_limit_notification():
    """Send an email alert when all Gemini keys are rate limited."""
    try:
        gmail = get_gmail_service()
        msg = MIMEMultipart()
        msg["From"] = f"{CONFIG['SENDER_NAME']} <{CONFIG['SENDER_EMAIL']}>"
        msg["To"] = CONFIG["SENDER_EMAIL"]
        msg["Subject"] = "⚠️ Gemini API Rate Limit Alert"
        body = "All your Gemini API keys have hit their rate limits or failed. The outreach run has been paused or completed with partial results."
        msg.attach(MIMEText(body, "plain"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        log.info("Rate limit notification sent to email.")
    except Exception as e:
        log.error(f"Failed to send rate limit email: {e}")


def send_linkedin_auth_error_notification(status_code: int):
    """Send an email alert when LinkedIn session cookies expire."""
    try:
        gmail = get_gmail_service()
        msg = MIMEMultipart()
        msg["From"] = f"{CONFIG['SENDER_NAME']} <{CONFIG['SENDER_EMAIL']}>"
        msg["To"] = CONFIG["SENDER_EMAIL"]
        msg["Subject"] = f"⚠️ LinkedIn Auth Error ({status_code}) — Session Expired"
        
        body = f"""\
Your LinkedIn Outreach Agent encountered an authentication error ({status_code}). 
This likely means your 'li_at' or 'JSESSIONID' cookies have expired.

Please:
1. Log in to LinkedIn in your browser.
2. Extract the fresh 'li_at' and 'JSESSIONID' values from DevTools.
3. Update your environment variables or CONFIG.
"""
        msg.attach(MIMEText(body, "plain"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        log.info("LinkedIn auth error notification sent to email.")
    except Exception as e:
        log.error(f"Failed to send auth error email: {e}")


def extract_contacts_with_gemini(saved_items: list[dict]) -> list[dict]:
    """
    Use Gemini to extract emails, phone numbers, and role details
    from saved LinkedIn posts.
    """
    items_with_content = [s for s in saved_items if s.get("full_content")]
    if not items_with_content:
        log.info("No saved posts with content to analyze.")
        return []

    # Reduced batch size for higher quality and reliability
    batch_size = 3
    all_extracted = []

    for batch_start in range(0, len(items_with_content), batch_size):
        batch = items_with_content[batch_start: batch_start + batch_size]

        posts_block = ""
        for idx, item in enumerate(batch):
            posts_block += f"""
===== Post {idx + 1} =====
URN: {item.get('post_urn', 'unknown')[:80]}

--- Content ---
{item.get('full_content', '')[:3500]}
"""

        prompt = f"""You are analyzing LinkedIn posts to extract contact information for jobs in AI/ML, Data Science, and Engineering.

For each post, scan the content VERY carefully for:
- Email addresses: Extract the primary contact email. Look for obfuscated formats like "name[at]company.com", "name at company dot com", or emails hidden in signatures.
- Phone numbers: Extract the primary phone number (India +91 or US +1 or others).
- Poster Name: Identify the person who shared the post or the contact person mentioned.
- Role & Company: Identify the job title and the hiring company.

{posts_block}

Respond with ONLY a JSON array of objects (one per post). If info is missing, use null.
{{
    "index": <1-based index from the post list above>,
    "poster_name": "<full name>",
    "poster_email": "<clean email address like user@example.com>",
    "poster_phone": "<clean digits like +918077593119>",
    "company": "<company name>",
    "role_title": "<job title>",
    "role_summary": "<very brief 1-sentence role summary>",
    "has_contact_info": <true/false if EITHER email or phone was found>
}}"""

        batch_num = batch_start // batch_size + 1
        total_batches = (len(items_with_content) + batch_size - 1) // batch_size
        log.info(f"[Gemini {batch_num}/{total_batches}] Analyzing {len(batch)} posts...")

        raw = call_gemini(prompt)

        try:
            # More robust JSON cleaning
            cleaned = re.sub(r"^.*?\[", "[", raw.strip(), flags=re.DOTALL)
            cleaned = re.sub(r"\].*?$", "]", cleaned, flags=re.DOTALL)
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

        time.sleep(2)  # Short pause

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
    seen_ids = _normalize_seen_urns(seen_urns)  # bare numeric IDs for cross-format dedup

    fresh, skipped = [], 0
    for r in results:
        urn = r.get("post_urn", "") or r.get("entity_urn", "")
        m = re.search(r"(\d{10,})", urn)
        numeric_id = m.group(1) if m else ""
        if urn in seen_urns or numeric_id in seen_ids:
            skipped += 1
        else:
            fresh.append(r)

    if skipped:
        log.info(f"Skipped {skipped} already-contacted posts (by LinkedIn ID)")
    return fresh


def record_contact(history: dict, urn: str, email: str = None, url: str = None, thread_id: str = None, message_id: str = None):
    if urn and urn not in history["contacted_urns"]:
        history["contacted_urns"].append(urn)
        
        # Add structured detail with URL
        if "contacted_details" not in history:
            history["contacted_details"] = []
        
        history["contacted_details"].append({
            "urn": urn,
            "url": url,
            "email": email,
            "thread_id": thread_id,
            "message_id": message_id,
            "followed_up": False,
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


def extract_first_name(full_name: str) -> str:
    """Extract first name from full name, defaulting to None if unclear."""
    if not full_name or full_name == "?":
        return None
    
    # Remove common prefixes/suffixes
    clean_name = re.sub(r"^(Mr\.|Ms\.|Mrs\.|Dr\.)\s+", "", full_name, flags=re.I)
    
    # Take the first word
    parts = clean_name.split()
    if not parts:
        return None
        
    first_name = parts[0].strip()
    
    # If first name is just a single character or symbol, it's not reliable
    if len(first_name) <= 1 or not first_name.isalpha():
        return None
        
    return first_name.capitalize()


def select_resume(content: str) -> str:
    """
    Select the most relevant resume based on post content and RESUME_MAPPING.
    Returns the file path of the selected resume.
    """
    mapping = CONFIG.get("RESUME_MAPPING", {})
    if not mapping:
        return CONFIG["RESUME_PATH"]

    content_lower = content.lower()
    
    # Clean up content: replace non-alphanumeric with spaces for easier matching
    # We keep numbers for things like 'M365' or 'S/4HANA'
    clean_content = re.sub(r'[^a-z0-9\s]', ' ', content_lower)
    
    for keywords_str, resume_file in mapping.items():
        keywords = [k.strip().lower() for k in keywords_str.split(",")]
        for kw in keywords:
            # STRICT word boundary check with optional plural 's'
            # This matches 'sap' but NOT 'whatsapp'
            # This matches 'copilot' and 'copilots'
            pattern = rf'\b{re.escape(kw)}s?\b'
            if re.search(pattern, clean_content):
                # CRITICAL: Log if we found a match but the file is missing
                if Path(resume_file).exists():
                    log.info(f"  ✅ Match Found: keyword '{kw}' -> Selected: {resume_file}")
                    return resume_file
                else:
                    log.warning(f"  ⚠️ Match Found ('{kw}'), but FILE MISSING: {resume_file}")

    log.info(f"  ℹ️ No keyword matches found in post. Using default resume.")
    return CONFIG["RESUME_PATH"]


def compose_email(to_email: str, recipient_name: str, role_title: str, post_url: str, resume_path: str = None) -> str:
    msg = MIMEMultipart()
    msg["From"] = f"{CONFIG['SENDER_NAME']} <{CONFIG['SENDER_EMAIL']}>"
    msg["To"] = to_email
    msg["Subject"] = EMAIL_TEMPLATE["subject"]

    # Address by first name or "there"
    first_name = extract_first_name(recipient_name)
    body = EMAIL_TEMPLATE["body"].format(
        recipient_name=first_name or "Team",
        role_title=role_title or "AI/ML Engineer",
        post_url=post_url or "LinkedIn post",
        sender_name=CONFIG["SENDER_NAME"],
    )

    msg.attach(MIMEText(body, "plain"))

    # Use selected resume or default
    final_resume_path = Path(resume_path or CONFIG["RESUME_PATH"])
    if final_resume_path.exists():
        with open(final_resume_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={final_resume_path.name}")
            msg.attach(part)
    else:
        log.warning(f"Resume not found at {final_resume_path} — sending without attachment.")

    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def send_one_email(service, to_email: str, name: str, role_title: str, post_url: str, resume_path: str = None) -> dict:
    raw = compose_email(to_email, name, role_title, post_url, resume_path=resume_path)
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def get_resume_url(resume_path: str) -> str:
    """Construct the GitHub URL for a given resume filename."""
    base_prefix = CONFIG["RESUME_URL_PREFIX"]
    filename = Path(resume_path).name if resume_path else Path(CONFIG["RESUME_PATH"]).name
    return f"{base_prefix}{quote(filename)}"


def format_whatsapp_link(phone_str: str, recipient_name: str, role_title: str, post_url: str, resume_url: str = None) -> str:
    """Clean phone number and generate a pre-filled WhatsApp wa.me link."""
    try:
        # Extract ONLY digits
        digits_only = re.sub(r"\D", "", phone_str)
        
        # WhatsApp requirement: Country code + Number, NO '+' or other chars.
        # If user provided 10 digits, assume India (91)
        if len(digits_only) == 10:
            final_number = "91" + digits_only
        elif len(digits_only) > 10:
            # Already has a country code (like 91987...)
            final_number = digits_only
        else:
            return "" # Invalid length for WhatsApp
        
        # Build message
        first_name = extract_first_name(recipient_name)
        body = EMAIL_TEMPLATE["body"].format(
            recipient_name=first_name or "Team",
            role_title=role_title or "AI/ML Engineer",
            post_url=post_url or "LinkedIn post",
            sender_name=CONFIG["SENDER_NAME"],
        )
        
        # Explicitly call out the dynamic resume in the WhatsApp message
        final_resume_url = resume_url or get_resume_url(CONFIG["RESUME_PATH"])
        body = body.replace("Kindly find my resume attached below.", f"📄 View my Resume: {final_resume_url}\n\n")
        
        encoded_msg = quote(body)
        return f"https://wa.me/{final_number}?text={encoded_msg}"
    except Exception as e:
        log.debug(f"Failed to format WhatsApp link for {phone_str}: {e}")
        return ""


def send_followup_email(service, to_email: str, thread_id: str, last_message_id: str) -> dict:
    """Send a follow-up email in the same thread."""
    # Get thread to find subject
    thread = service.users().threads().get(userId="me", id=thread_id).execute()
    messages = thread.get("messages", [])
    
    # Find subject from first message
    subject = "Follow up: Application"
    for part in messages[0].get("payload", {}).get("headers", []):
        if part.get("name") == "Subject":
            subject = part.get("value")
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            break

    msg = MIMEMultipart()
    msg["From"] = f"{CONFIG['SENDER_NAME']} <{CONFIG['SENDER_EMAIL']}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["In-Reply-To"] = last_message_id
    msg["References"] = last_message_id

    body = """Hello Team,

Hope you are doing well! It would be great if you can share for any update on my application.

Thanks & Regards,
Adarsh
Contact: +91-8077593119"""

    msg.attach(MIMEText(body, "plain"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    
    return service.users().messages().send(userId="me", body={
        "raw": raw,
        "threadId": thread_id
    }).execute()


def process_followups(service, history: dict) -> list[dict]:
    """Check history for emails sent > 24h ago with no reply, and send followup."""
    log.info("\n[STEP 5] Checking for follow-ups...")
    
    contacted_details = history.get("contacted_details", [])
    if not contacted_details:
        log.info("  No outreach history to check for follow-ups.")
        return []

    now = datetime.now(timezone.utc)
    followups_sent = []

    for entry in contacted_details:
        # 1. Basic checks (time, already followed up, has thread)
        ts_str = entry.get("timestamp")
        if not ts_str: continue
        
        sent_at = datetime.fromisoformat(ts_str)
        # Ensure sent_at is timezone-aware
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
            
        hours_passed = (now - sent_at).total_seconds() / 3600
        
        if hours_passed < 24: continue
        if entry.get("followed_up"): continue
        
        thread_id = entry.get("thread_id")
        email = entry.get("email")
        if not thread_id or not email: continue

        log.info(f"  Checking thread {thread_id} ({email})...")

        try:
            # 2. Check for replies in thread
            thread = service.users().threads().get(userId="me", id=thread_id).execute()
            messages = thread.get("messages", [])
            
            # If more than 1 message, check if any message is NOT from the sender
            has_reply = False
            for msg in messages:
                headers = msg.get("payload", {}).get("headers", [])
                from_email = ""
                for h in headers:
                    if h.get("name") == "From":
                        from_email = h.get("value")
                        break
                
                # If message is NOT from our sender email, it's a reply
                if CONFIG["SENDER_EMAIL"].lower() not in from_email.lower():
                    has_reply = True
                    break
            
            if has_reply:
                log.info(f"    Recruiter replied! Marking as followed_up to stop monitoring.")
                entry["followed_up"] = True
                continue

            # 3. Send Follow-up
            log.info(f"    No reply after {hours_passed:.1f}h. Sending follow-up...")
            last_msg_id = entry.get("message_id")
            send_followup_email(service, email, thread_id, last_msg_id)
            
            entry["followed_up"] = True
            entry["followup_timestamp"] = datetime.now().isoformat()
            followups_sent.append(entry)
            time.sleep(1)

        except Exception as e:
            log.error(f"    Error processing follow-up for {email}: {e}")

    if followups_sent:
        log.info(f"  Sent {len(followups_sent)} follow-up emails.")
    else:
        log.info("  No follow-ups needed at this time.")
        
    return followups_sent


def send_run_summary_email(service, phone_leads: list[dict], emailed_leads: list[dict], followed_up: list[dict] = None):
    """Send a combined summary of phone leads, outreach emails, and follow-ups."""
    if not phone_leads and not emailed_leads and not followed_up:
        log.info("No new activity to summarize. Skipping notification email.")
        return

    log.info(f"Sending run summary to {CONFIG['SENDER_EMAIL']}...")

    msg = MIMEMultipart()
    msg["From"] = f"{CONFIG['SENDER_NAME']} <{CONFIG['SENDER_EMAIL']}>"
    msg["To"] = CONFIG["SENDER_EMAIL"]
    msg["Subject"] = f"LinkedIn Outreach Summary - {datetime.now().strftime('%Y-%m-%d')}"

    body_lines = [
        f"Hi {CONFIG['SENDER_NAME']},\n",
        f"Here is the summary from the latest LinkedIn outreach run.\n",
    ]

    # --- SECTION 1: EMAILED LEADS ---
    if emailed_leads:
        body_lines.append("✅ NEW OUTREACH EMAILS SENT")
        body_lines.append("-" * 30)
        for i, lead in enumerate(emailed_leads, 1):
            name = lead.get("poster_name") or "Unknown"
            email = lead.get("poster_email") or "No Email"
            role = lead.get("role_title") or lead.get("role_summary", "")[:100]
            company = lead.get("company") or "Unknown Company"
            urn = lead.get("post_urn") or lead.get("entity_urn", "")
            url = f"https://www.linkedin.com/feed/update/{urn}" if urn else "No URL"
            
            body_lines.append(f"{i}. {name} ({company})")
            body_lines.append(f"   Email:    {email}")
            body_lines.append(f"   Role:     {role}")
            body_lines.append(f"   LinkedIn: {url}")
            
            # GitHub Action link for tailoring
            resume_link = f"https://github.com/Adarsh-12-innovation/linkedin-outreach/actions/workflows/custom_resume.yml"
            short_urn = urn.split(":")[-1] if urn else ""
            if short_urn:
                body_lines.append(f"   📄 Custom Resume: {resume_link} (Paste URN: {short_urn})")
            body_lines.append("")
        body_lines.append("")

    # --- SECTION 2: FOLLOW-UPS ---
    if followed_up:
        body_lines.append("🔄 FOLLOW-UP EMAILS SENT (No reply after 24h)")
        body_lines.append("-" * 30)
        for i, entry in enumerate(followed_up, 1):
            email = entry.get("email")
            url = entry.get("url") or "No URL"
            urn = entry.get("urn", "")
            short_urn = urn.split(":")[-1] if urn else ""
            resume_link = f"https://github.com/Adarsh-12-innovation/linkedin-outreach/actions/workflows/custom_resume.yml"

            body_lines.append(f"{i}. Followed up with: {email}")
            body_lines.append(f"   LinkedIn: {url}")
            if short_urn:
                body_lines.append(f"   📄 Custom Resume: {resume_link} (Paste URN: {short_urn})")
            body_lines.append("")
        body_lines.append("")

    # --- SECTION 3: PHONE LEADS ---
    if phone_leads:
        body_lines.append("📞 PHONE LEADS (Manual Follow-up)")
        body_lines.append("-" * 30)
        body_lines.append("Click the [WhatsApp] links to message recruiters instantly.\n")
        for i, lead in enumerate(phone_leads, 1):
            name = lead.get("poster_name") or "Unknown"
            phone = lead.get("poster_phone") or "No Phone"
            email = lead.get("poster_email") or "No Email"
            role = lead.get("role_title") or lead.get("role_summary", "")[:100]
            company = lead.get("company") or "Unknown Company"
            urn = lead.get("post_urn") or lead.get("entity_urn", "")
            url = f"https://www.linkedin.com/feed/update/{urn}" if urn else "No URL"
            
            # Select resume for WhatsApp link consistency
            selected_resume = select_resume(lead.get("full_content", ""))
            resume_url = get_resume_url(selected_resume)
            wa_link = format_whatsapp_link(phone, name, role, url, resume_url=resume_url) if phone != "No Phone" else ""
            
            # GitHub Action link for tailoring
            resume_link = f"https://github.com/Adarsh-12-innovation/linkedin-outreach/actions/workflows/custom_resume.yml"
            short_urn = urn.split(":")[-1] if urn else ""

            body_lines.append(f"{i}. {name} ({company})")
            body_lines.append(f"   Phone:    {phone}")
            if wa_link:
                body_lines.append(f"   WhatsApp: {wa_link}")
            body_lines.append(f"   Email:    {email}")
            body_lines.append(f"   Role:     {role}")
            body_lines.append(f"   LinkedIn: {url}")
            if short_urn:
                body_lines.append(f"   📄 Custom Resume: {resume_link} (Paste URN: {short_urn})")
            body_lines.append("")

    body_lines.append("\nBest regards,\nYour Outreach Agent")
    body = "\n".join(body_lines)
    msg.attach(MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info("Combined summary email sent successfully.")


def send_queryid_stale_notification():
    """
    Send a one-time email alert when the GraphQL queryId has 
    .
    
    Uses a marker file to avoid re-sending on every run.  The marker is
    automatically deleted when a working queryId is detected again (see
    _try_fetch_saved_via_graphql).
    """
    marker = Path(CONFIG["STALE_QUERYID_NOTIFIED_FILE"])

    # Already notified for this stale period — don't spam
    if marker.exists():
        log.info("  QueryId stale notification already sent (skipping).")
        return

    try:
        gmail = get_gmail_service()

        msg = MIMEMultipart()
        msg["From"] = f"{CONFIG['SENDER_NAME']} <{CONFIG['SENDER_EMAIL']}>"
        msg["To"] = CONFIG["SENDER_EMAIL"]
        msg["Subject"] = "⚠️ LinkedIn Outreach Agent — GraphQL QueryId Expired"

        body = """\
Hi Adarsh,

Your LinkedIn Saved Posts Outreach Agent detected that the GraphQL queryId has expired (returned 400/404).

The script fell back to the HTML scraper which is limited to ~10 posts per run. To restore full pagination:

1. Open Chrome → linkedin.com/my-items/saved-posts/
2. F12 → Network tab → filter by "graphql"
3. Clear the log, then scroll down until new posts load
4. Copy the queryId from the new request URL
   (looks like: voyagerSearchDashClusters.xxxxx)
5. Update the known_query_ids list in saved_posts_outreach.py
6. Commit & push (if using GitHub Actions)

Current stale queryId:
  voyagerSearchDashClusters.05111e1b90ee7fea15bebe9f9410ced9

This alert is sent once per stale period. You won't get another until you
update the queryId and it expires again.

— Your Outreach Agent
"""
        msg.attach(MIMEText(body, "plain"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        gmail.users().messages().send(userId="me", body={"raw": raw}).execute()

        # Write marker so we don't re-send next run
        marker.write_text(datetime.now().isoformat())
        log.info("  Sent queryId-stale notification email to self.")

    except Exception as e:
        log.error(f"  Failed to send queryId-stale notification: {e}")


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
    seen_ids = _normalize_seen_urns(seen_urns)
    
    # Collect numeric IDs from existing phone leads + history
    existing_ids = set()
    for e in existing:
        urn = e.get("post_urn") or e.get("entity_urn") or ""
        m = re.search(r"(\d{10,})", urn)
        if m:
            existing_ids.add(m.group(1))
    existing_ids |= seen_ids
    
    new_leads = []
    for r in phone_leads:
        urn = r.get("post_urn") or r.get("entity_urn") or ""
        m = re.search(r"(\d{10,})", urn)
        numeric_id = m.group(1) if m else ""
        if numeric_id not in existing_ids:
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

    # Filter excluded domains
    excluded_domains = CONFIG["EXCLUDED_DOMAINS"]
    if excluded_domains:
        before_count = len(with_email)
        with_email = [
            r for r in with_email 
            if r["poster_email"].split('@')[-1].lower() not in excluded_domains
        ]
        if len(with_email) < before_count:
            log.info(f"  Filtered out {before_count - len(with_email)} leads (excluded domains)")

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
    emailed = []

    # 1. Send outreach emails to new candidates
    for r in with_email:
        try:
            urn = r.get("post_urn") or r.get("entity_urn", "")
            url = f"https://www.linkedin.com/feed/update/{urn}" if urn else ""
            
            # SELECT CUSTOM RESUME
            selected_resume = select_resume(r.get("full_content", ""))
            
            resp = send_one_email(
                gmail, r["poster_email"],
                r.get("poster_name", ""), r.get("role_title", ""),
                url,
                resume_path=selected_resume
            )
            thread_id = resp.get("threadId")
            message_id = resp.get("id")
            
            log.info(f"  Sent -> {r['poster_email']} ({r.get('poster_name', '?')}) | Thread: {thread_id} | Resume: {selected_resume}")
            record_contact(history, urn, r["poster_email"], url, thread_id, message_id)
            emailed.append(r)
            time.sleep(1)
        except Exception as e:
            log.error(f"  FAILED {r.get('poster_email', '?')}: {e}")

    # 2. Process follow-ups for old unanswered emails
    followed_up = []
    try:
        followed_up = process_followups(gmail, history)
    except Exception as e:
        log.error(f"  Failed to process follow-ups: {e}")

    # 3. Send combined summary email to self (if not dry run)
    try:
        send_run_summary_email(gmail, phone_leads, emailed, followed_up)
    except Exception as e:
        log.error(f"  Failed to send run summary email to self: {e}")

    save_history(history)
    log.info(f"\n  Summary: {len(emailed)} new sent, {len(followed_up)} follow-ups sent.")
    return emailed


# ═══════════════════════════════════════════════
# GIT AUTO-SYNC
# ═══════════════════════════════════════════════

def git_sync_pull():
    """Auto pull latest history from GitHub before running."""
    try:
        # Stash any local changes first (e.g. uncommitted results)
        subprocess.run(
            ["git", "stash", "--include-untracked"],
            capture_output=True, text=True, timeout=15
        )

        result = subprocess.run(
            ["git", "pull", "origin", "main", "--rebase"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log.info(f"  Git pull: {result.stdout.strip()}")
        else:
            log.warning(f"  Git pull failed: {result.stderr.strip()}")

        # Pop stashed changes back
        pop_result = subprocess.run(
            ["git", "stash", "pop"],
            capture_output=True, text=True, timeout=15
        )
        if pop_result.returncode != 0 and "No stash" not in pop_result.stderr:
            log.debug(f"  Git stash pop: {pop_result.stderr.strip()}")

    except FileNotFoundError:
        log.warning("  git not found. Skipping auto-sync.")
    except Exception as e:
        log.warning(f"  Git pull error: {e}")


def git_sync_push():
    """Auto commit and push updated history to GitHub after running."""
    try:
        files_to_commit = [
            CONFIG["HISTORY_FILE"],
            CONFIG["PHONE_LEADS_FILE"],
            CONFIG["RESULTS_DIR"],
        ]

        # Stage only files that exist
        for f in files_to_commit:
            if Path(f).exists():
                subprocess.run(["git", "add", f], capture_output=True, timeout=10)

        ts = datetime.now().strftime("%Y%m%d_%H%M")
        result = subprocess.run(
            ["git", "commit", "-m", f"saved run {ts}"],
            capture_output=True, text=True, timeout=15
        )
        if "nothing to commit" in result.stdout:
            log.info("  Git push: nothing new to commit.")
            return

        result = subprocess.run(
            ["git", "push", "origin", "main"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log.info(f"  Git push: {result.stdout.strip()}")
        else:
            log.warning(f"  Git push failed: {result.stderr.strip()}")
    except FileNotFoundError:
        log.warning("  git not found. Skipping auto-sync.")
    except Exception as e:
        log.warning(f"  Git push error: {e}")


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
    parser.add_argument("--no-git-sync", action="store_true", help="Skip auto git pull/push")
    args = parser.parse_args()

    if args.resume:
        CONFIG["RESUME_PATH"] = args.resume
    CONFIG["LOOKBACK_HOURS"] = args.hours

    # Detect if running in GitHub Actions
    is_github_actions = os.getenv("GITHUB_ACTIONS") == "true"

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

    # ── 0. Git sync (pull latest history) ──
    if not args.no_git_sync and not is_github_actions:
        log.info("\n[STEP 0] Syncing history from GitHub...")
        git_sync_pull()

    # ── 1. Fetch saved posts (Optimization: stops when it hits history) ──
    log.info("\n[STEP 1] Fetching saved LinkedIn posts...")
    session = create_linkedin_session()
    history = load_history()
    saved = fetch_saved_posts(session, history)

    # ── Notify if GraphQL queryId has expired ──
    if _GRAPHQL_QUERYID_STALE:
        log.warning("\n[ALERT] GraphQL queryId expired — pagination limited to ~10 posts.")
        log.warning("  Run is continuing with HTML fallback, but update the queryId soon.")
        try:
            send_queryid_stale_notification()
        except Exception as e:
            log.error(f"  Could not send stale-queryId notification: {e}")

    # Final check before heavy content fetching
    saved = dedupe_against_history(saved, history)

    if not saved:
        log.info("No new saved posts found (all recent posts already in history). Optimization complete.")
        # ── Git sync (push updated history even if no new posts, to keep results dir clean if needed) ──
        if not args.no_git_sync and not is_github_actions:
            git_sync_push()
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
        if not args.no_git_sync and not is_github_actions:
            git_sync_push()
        return

    # ── 5. Auto-send ──
    log.info("\n[STEP 4] Auto-send emails & save phone leads...")
    emailed = auto_send(extracted, dry_run=args.dry_run)

    # ── 6. Save ──
    save_run(saved, extracted, emailed)

    # ── 8. Git sync (push updated history) ──
    if not args.no_git_sync and not is_github_actions:
        log.info("\n[STEP 8] Pushing updated history to GitHub...")
        git_sync_push()

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