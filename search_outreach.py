"""
LinkedIn Keyword Search Outreach Agent
=======================================
Searches LinkedIn for AI/ML contract hiring posts, filters them through
a two-stage pipeline (keyword → LLM), extracts contacts, and feeds them
into the shared outreach pipeline.

DESIGNED TO RUN LOCALLY (PC / Termux) — not on GitHub Actions.
Uses residential IP, curl_cffi for TLS fingerprinting, and human-like
delays to minimize LinkedIn flagging risk.

Shares outreach_history.json with saved_posts_outreach.py via git.

Flow:
    git pull (auto-sync history)
    → Search LinkedIn for keyword phrases (sorted by latest)
    → Fetch full content of each result
    → Stage I:  Keyword filter (no LLM) — must-have / must-not-have rules
    → Stage II: LLM filter (gemini-2.5-flash-lite) — deeper relevancy check
    → Extract contacts with Gemini (reuses existing extraction logic)
    → Auto-send emails / save phone leads
    → git push (auto-sync history back)

Prerequisites:
    pip install curl_cffi requests beautifulsoup4 google-api-python-client \\
                google-auth-oauthlib python-dotenv phonenumbers

Usage:
    python search_outreach.py                 # Full pipeline
    python search_outreach.py --dry-run       # Everything except sending emails
    python search_outreach.py --no-git-sync   # Skip auto git pull/push
"""

import os
import sys
import json
import base64
import re
import time
import random
import argparse
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from urllib.parse import quote
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

    # Gemini — Contact extraction keys (reuses same keys as saved_posts_outreach.py)
    "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY"),
    "ALTERNATIVE_GEMINI_API_KEY": os.getenv("ALTERNATIVE_GEMINI_API_KEY", ""),
    "SECOND_ALTERNATIVE_GEMINI_API_KEY": os.getenv("SECOND_ALTERNATIVE_GEMINI_API_KEY", ""),
    "GEMINI_MODEL": "gemini-2.5-flash",

    # Gemini — LLM Filtering keys (SEPARATE keys to avoid quota contention)
    "FILTER_GEMINI_API_KEY": os.getenv("FILTER_GEMINI_API_KEY", ""),
    "FILTER_ALT_GEMINI_API_KEY": os.getenv("FILTER_ALT_GEMINI_API_KEY", ""),
    "FILTER_GEMINI_MODEL": "gemini-2.5-flash-lite",

    # Gmail OAuth2
    "GMAIL_CREDENTIALS_FILE": "credentials.json",
    "GMAIL_TOKEN_FILE": "token.json",
    "GMAIL_SCOPES": [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly"
    ],

    # Your details
    "SENDER_NAME": "Adarsh Bansal",
    "SENDER_EMAIL": "adarshbansal1995@gmail.com",

    # Resume
    "RESUME_PATH": "Adarsh Bansal_CV_2026.pdf",
    "RESUME_URL": "https://github.com/Adarsh-12-innovation/linkedin-outreach/raw/main/Adarsh%20Bansal_CV_2026.pdf",

    # GitHub (for auto git sync)
    "GITHUB_REPO": "Adarsh-12-innovation/linkedin-outreach",

    # Tracking — SHARED with saved_posts_outreach.py
    "HISTORY_FILE": "outreach_history.json",
    "PHONE_LEADS_FILE": "phone_leads.json",
    "RESULTS_DIR": "results",

    # Search config
    "SEARCH_PHRASES": [
        "ai contract hiring",
        # "machine learning contract hiring",
    ],
    "SEARCH_QUERYID": "voyagerSearchDashClusters.05111e1b90ee7fea15bebe9f9410ced9",
    "SEARCH_MAX_PAGES_PER_PHRASE": 1,  # 10 results per page × 5 = 50 per phrase
}

# ─────────────────────────────────────────────
# EMAIL TEMPLATE (same as saved_posts_outreach.py)
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
# KEYWORD FILTERING RULES (Stage I)
# ─────────────────────────────────────────────

# All checked case-insensitively against post content
MUST_HAVE_KEYWORDS = {
    "employment_type": [
        "contract", "freelancer", "freelancing", "contractual", "contractor"
    ],
    "location_type": [
        "remote", "anywhere", "pan india",
    ],
    "domain": [
        "ai", "gen ai", "genai", "generative ai", "machine learning", "ml",
        "aiml", "ai ml", "data science", "ml engineer",
        "machine learning engineer", "data scientist", "llm", "nlp",
        "python", "agentic", "engineer", "developer", "software",
        "agentic ai", "ai/ml", "artificial intelligence",
        "ai engineer", "ai developer"
    ],
}

MUST_NOT_HAVE_KEYWORDS = [
    "onsite", "hybrid", "wfo", "in-office", "in office", "office", "intern", "internship", "apprentice", "apprenticeship", "headquarters", "full-time","full time"
]

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("search-outreach")


# ═══════════════════════════════════════════════
# ANTI-DETECTION: SESSION & HUMAN SIMULATION
# ═══════════════════════════════════════════════

def _human_delay(min_s: float = 2.0, max_s: float = 6.0):
    """Sleep a random human-like duration with gaussian jitter."""
    base = random.uniform(min_s, max_s)
    jitter = random.gauss(0, 0.3)
    time.sleep(max(0.5, base + jitter))


def create_linkedin_session(use_cffi: bool = False):
    """
    Create an authenticated LinkedIn session.

    Default: plain requests (proven to work with saved_posts_outreach.py).
    Optional: curl_cffi with --use-cffi flag for Chrome TLS fingerprinting.
    """
    li_at = CONFIG["LINKEDIN_LI_AT"]
    jsessionid = CONFIG["LINKEDIN_JSESSIONID"].strip('"')

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/vnd.linkedin.normalized+json+2.1",
        "x-li-lang": "en_US",
        "x-restli-protocol-version": "2.0.0",
        "csrf-token": jsessionid,
    }

    if use_cffi:
        try:
            from curl_cffi.requests import Session as CffiSession
            session = CffiSession(impersonate="chrome")
            session.headers.update(headers)
            session.headers["Cookie"] = f"li_at={li_at}; JSESSIONID=\"{jsessionid}\""
            session._is_cffi = True
            log.info("LinkedIn session created with curl_cffi (Chrome TLS fingerprint)")
            return session
        except ImportError:
            log.warning("curl_cffi not installed. Falling back to requests.")

    # Default: plain requests (same as saved_posts_outreach.py — proven to work)
    import requests
    session = requests.Session()
    session.cookies.set("li_at", li_at, domain=".linkedin.com")
    session.cookies.set("JSESSIONID", f'"{jsessionid}"', domain=".linkedin.com")
    session.headers.update(headers)
    session._is_cffi = False
    log.info("LinkedIn session created with requests")
    return session


def decoy_request(session):
    """
    Make a random 'noise' request to look like normal browsing.
    LinkedIn expects ancillary traffic alongside API calls.
    """
    decoys = [
        "https://www.linkedin.com/voyager/api/me",
        "https://www.linkedin.com/voyager/api/feed/notifications?count=3",
        "https://www.linkedin.com/voyager/api/messaging/conversations?count=1",
    ]
    url = random.choice(decoys)
    try:
        session.get(url, timeout=10)
        log.debug(f"  Decoy request: {url.split('/')[-1]}")
    except Exception:
        pass
    _human_delay(1.0, 2.5)


# ═══════════════════════════════════════════════
# STEP 1: LINKEDIN KEYWORD SEARCH
# ═══════════════════════════════════════════════

def _normalize_urns_to_activity(raw_urns: set) -> set:
    """Collapse activity/ugcPost/share variants to single activity URN per numeric ID."""
    id_to_urn = {}
    for urn in raw_urns:
        m = re.search(r"(\d{10,})", urn)
        if not m:
            continue
        numeric_id = m.group(1)
        existing = id_to_urn.get(numeric_id)
        if existing and existing.startswith("urn:li:activity:"):
            continue
        id_to_urn[numeric_id] = f"urn:li:activity:{numeric_id}"
    return set(id_to_urn.values())


def _normalize_seen_urns(seen_urns: set) -> set:
    """Extract bare numeric IDs from history URNs for cross-format dedup."""
    ids = set()
    for urn in seen_urns:
        m = re.search(r"(\d{10,})", urn)
        if m:
            ids.add(m.group(1))
    return ids


def search_linkedin_posts(session, phrase: str, seen_ids: set) -> list[dict]:
    """
    Search LinkedIn content posts by keyword phrase, sorted by 'latest'.
    Directly extracts post content from GraphQL to minimize API calls.
    Implements: Max 100 posts, Pagination Gaps (8-15s), and Request Jitter.
    """
    graphql_base = "https://www.linkedin.com/voyager/api/graphql"
    query_id = CONFIG["SEARCH_QUERYID"]
    
    # HARD STOP at 100 posts (10 calls max)
    max_calls = 1
    results = []
    pagination_token = None
    total_fetched = 0
    start_offset = 0

    for call_num in range(max_calls):
        # REQUEST JITTER: Randomize count between 8 and 12
        current_batch_size = random.randint(8, 12)
        
        query_part = (
            f"keywords:{phrase},"
            f"flagshipSearchIntent:SEARCH_SRP,"
            f"queryParameters:List("
            f"(key:resultType,value:List(CONTENT)),"
            f"(key:sortBy,value:List(date_posted)),"
            f"(key:datePosted,value:List(past-24h))"
            f")"
        )

        if pagination_token:
            encoded_token = pagination_token.replace("=", "%3D")
            variables = f"(start:{start_offset},paginationToken:{encoded_token},query:({query_part}))"
        else:
            variables = f"(start:{start_offset},query:({query_part}))"

        variables_encoded = variables.replace(" ", "%20")
        url = f"{graphql_base}?variables={variables_encoded}&queryId={query_id}"

        try:
            resp = session.get(url, timeout=20)
            status = resp.status_code
            if status in (401, 403):
                log.error(f"  Auth error ({status}) — li_at + JSESSIONID likely expired.")
                send_linkedin_auth_error_notification(status)
                return results
            if status != 200: break
            data = resp.json()
        except: break

        # ── EXTRACT DATA DIRECTLY ──
        # Map URNs to their text content within the same response
        urn_to_content = {}
        
        # 1. Search for all 'Update' objects in 'included' which contain the text
        included = data.get("included", [])
        for item in included:
            if item.get("$type") == "com.linkedin.voyager.dash.feed.Update":
                # Find the activity URN
                urn = item.get("entityUrn", "")
                m = re.search(r"urn:li:activity:(\d+)", urn)
                if not m: continue
                act_id = m.group(1)
                
                # Extract text from commentary
                commentary = item.get("commentary", {})
                text_obj = commentary.get("text", {})
                if isinstance(text_obj, dict) and "text" in text_obj:
                    urn_to_content[act_id] = text_obj["text"]

        # 2. Identify the actual search results (to avoid noise/ads)
        page_results = []
        def find_results(obj):
            if isinstance(obj, dict):
                update_val = obj.get("*update") or obj.get("update")
                if isinstance(update_val, str) and "urn:li:activity:" in update_val:
                    m = re.search(r"urn:li:activity:(\d+)", update_val)
                    if m:
                        act_id = m.group(1)
                        full_urn = f"urn:li:activity:{act_id}"
                        if act_id not in [r["id"] for r in page_results]:
                            page_results.append({"id": act_id, "urn": full_urn})
                for v in obj.values(): find_results(v)
            elif isinstance(obj, list):
                for i in obj: find_results(i)

        find_results(data)

        if not page_results: break

        new_on_page = 0
        for res in page_results:
            act_id = res["id"]
            if act_id not in seen_ids:
                content = urn_to_content.get(act_id, "")
                post_url = f"https://www.linkedin.com/feed/update/{res['urn']}"
                
                # TERMINAL LOGGING: Show snippet of what was found
                preview = (content[:100].replace('\n', ' ') + "...") if content else "[No Text]"
                log.info(f"    - Extracted: {preview}")
                
                results.append({
                    "entity_urn": res["urn"],
                    "post_urn": res["urn"],
                    "post_url": post_url,
                    "full_content": content, 
                    "created_at": 0 
                })
                seen_ids.add(act_id)
                new_on_page += 1

        log.info(f"  Search '{phrase}' call {call_num + 1}: {len(page_results)} items ({new_on_page} new)")

        # ── FIND PAGINATION TOKEN ──
        next_token = None
        def find_token(obj):
            nonlocal next_token
            if next_token: return
            if isinstance(obj, dict):
                t = obj.get("paginationToken")
                if isinstance(t, str): next_token = t; return
                for v in obj.values(): find_token(v)
            elif isinstance(obj, list):
                for i in obj: find_token(i)
        find_token(data)

        if not next_token and len(page_results) < 5: break
        
        # ── PREPARE NEXT CALL ──
        pagination_token = next_token
        start_offset += len(page_results)
        total_fetched += len(page_results)
        
        if total_fetched >= 100: break

        # THE PAGINATION GAP: Simulate reading time (8-15s)
        gap = random.uniform(8.0, 15.0)
        log.info(f"    Simulating reading time... ({gap:.1f}s delay)")
        time.sleep(gap)

    return results


def fetch_all_search_results(session, history: dict) -> list[dict]:
    """
    Run all configured search phrases and collect unique new post URNs.
    """
    seen_urns = set(history.get("contacted_urns", []))
    seen_ids = _normalize_seen_urns(seen_urns)
    all_results = []

    for i, phrase in enumerate(CONFIG["SEARCH_PHRASES"]):
        log.info(f"\n  Searching: \"{phrase}\" (sorted by latest)...")
        phrase_results = search_linkedin_posts(session, phrase, seen_ids)
        all_results.extend(phrase_results)
        log.info(f"  \"{phrase}\": {len(phrase_results)} new posts")
        if i < len(CONFIG["SEARCH_PHRASES"]) - 1:
            _human_delay(5.0, 10.0)

    unique = {}
    for r in all_results:
        m = re.search(r"(\d{10,})", r["post_urn"])
        if m: unique[m.group(1)] = r

    log.info(f"\n  Total unique new posts across all phrases: {len(unique)}")
    return list(unique.values())


# ═══════════════════════════════════════════════
# STEP 2: FETCH POST CONTENT (Verification only)
# ═══════════════════════════════════════════════

def fetch_post_content(session, post_urn: str) -> tuple[str, int]:
    """[LEGACY] No longer used by primary flow."""
    return "", 0


def fetch_all_post_contents(session, items: list[dict]) -> list[dict]:
    """
    In the GraphQL-direct version, content is already present.
    This function now just verifies and filters valid items.
    """
    log.info(f"Verifying content for {len(items)} posts...")
    valid = []
    for item in items:
        content = item.get("full_content", "")
        # Only accept posts that have readable text
        if content and len(content) > 20:
            valid.append(item)
    return valid


# ═══════════════════════════════════════════════
# STEP 3: STAGE I — KEYWORD FILTERING (No LLM)
# ═══════════════════════════════════════════════

def _has_contact_info(content: str) -> bool:
    """
    Carefully detect emails and phone numbers using a robust regex suite.
    Handles standard and obfuscated formats (e.g., name [at] domain.com).
    """
    if not content or len(content) < 50:
        return False
        
    # 1. EMAILS (Standard + Obfuscated)
    email_patterns = [
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        r"[a-zA-Z0-9._%+\-]+\s*[\[\(\{\s]*at[\]\)\}\s]*\s*[a-zA-Z0-9.\-]+\s*[\[\(\{\s]*dot[\]\)\}\s]*\s*[a-zA-Z]{2,}",
    ]
    
    for pattern in email_patterns:
        if re.search(pattern, content, re.IGNORECASE):
            return True

    # 2. PHONE NUMBERS (India, US, International)
    phone_patterns = [
        r"(?:\+91|91)?[-\s]?[6-9]\d{4}[-\s]?\d{5}", 
        r"(?:\+?1[-\s]?)?\(?\d{3}\)?[-\s]?\d{3}[-\s]?\d{4}",
        r"\+\d{1,3}[-\s]?\d{2,5}[-\s]?\d{4,10}",
    ]
    
    for pattern in phone_patterns:
        matches = re.finditer(pattern, content)
        for m in matches:
            digits = re.sub(r"\D", "", m.group())
            if len(digits) >= 10:
                return True

    return False


def stage_i_filter(items: list[dict]) -> list[dict]:
    """
    Stage I: 
    1. Precision keyword check with word boundaries.
    2. LLM check (flash-lite) for contact info.
    """
    if not items: return []
    
    # 1. Precision Keyword Check
    kw_passed = []
    for item in items:
        content = (item.get("full_content") or "").lower()
        
        # Must NOT have (using word boundaries \b)
        blocked = False
        for kw in MUST_NOT_HAVE_KEYWORDS:
            if re.search(rf"\b{re.escape(kw.lower())}\b", content):
                blocked = True; break
        if blocked: continue
            
        # Must have (all categories, using word boundaries)
        all_groups = True
        for group, kws in MUST_HAVE_KEYWORDS.items():
            if not any(re.search(rf"\b{re.escape(kw.lower())}\b", content) for kw in kws):
                all_groups = False; break
        if all_groups:
            kw_passed.append(item)
            
    if not kw_passed:
        log.info("  Stage I: 0 posts passed keyword check.")
        return []

    # 2. LLM Contact Detection (flash-lite)
    batch_size = 10
    passed = []
    log.info(f"  [Stage I LLM] Detecting contact info in {len(kw_passed)} posts...")

    for batch_start in range(0, len(kw_passed), batch_size):
        batch = kw_passed[batch_start:batch_start + batch_size]
        posts_block = ""
        for idx, item in enumerate(batch):
            posts_block += f"\nPost {idx + 1}:\n{item['full_content'][:2000]}\n"

        prompt = f"""Analyze these posts for any contact info (Email or Phone). 
Include obfuscated ones like "user [at] company dot com".

Respond with ONLY a JSON array: [ {{"index": 1, "has_contact": true/false}} ]
{posts_block}"""

        raw = call_filter_gemini(prompt) # 1 retry per key
        if not raw:
            passed.extend(batch); continue

        try:
            cleaned = re.sub(r"^.*?\[", "[", raw.strip(), flags=re.DOTALL)
            cleaned = re.sub(r"\].*?$", "]", cleaned, flags=re.DOTALL)
            evals = json.loads(cleaned)
            for ev in evals:
                idx = ev.get("index", 0) - 1
                if 0 <= idx < len(batch) and ev.get("has_contact"):
                    passed.append(batch[idx])
        except: passed.extend(batch)
        time.sleep(2)

    log.info(f"  Stage I Results: {len(passed)} passed.")
    return passed


# ═══════════════════════════════════════════════
# STEP 4: STAGE II — DEEPER RELEVANCY (gemini-2.5-flash)
# ═══════════════════════════════════════════════

def call_filter_gemini(prompt: str) -> str:
    """
    Call Gemini with FILTER-specific API keys (flash-lite).
    - Key 1: 1 retry
    - Key 2: 1 retry
    """
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{CONFIG['FILTER_GEMINI_MODEL']}:generateContent"
    )

    keys = [
        {"key": CONFIG["FILTER_GEMINI_API_KEY"], "retries": 1, "name": "Filter Primary"},
        {"key": CONFIG["FILTER_ALT_GEMINI_API_KEY"], "retries": 1, "name": "Filter Alt"},
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
                            "maxOutputTokens": 16000,
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
                        log.warning(f"  {k_info['name']} Key: Exhausted retries. Rotating...")
                        break

                resp.raise_for_status()
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]

            except Exception as e:
                log.error(f"  {k_info['name']} Error: {e}")
                if attempt == max_attempts:
                    break
                time.sleep(2)

    log.critical("All filter Gemini keys failed.")
    try: send_rate_limit_notification()
    except: pass
    return ""


def stage_ii_llm_filter(items: list[dict]) -> list[dict]:
    """
    Stage II: Deep relevancy check using gemini-2.5-flash.
    Uses 3 extraction keys with 1 retry each.
    """
    if not items: return []

    batch_size = 10
    passed = []

    for batch_start in range(0, len(items), batch_size):
        batch = items[batch_start:batch_start + batch_size]
        posts_block = ""
        for idx, item in enumerate(batch):
            content = (item.get("full_content") or "")[:2500]
            posts_block += f"\n===== Post {idx + 1} =====\n{content}\n"

        prompt = f"""Analyze if these are GENUINE AI/ML contract jobs suitable for a candidate in India.
Reject US-only, training, or onsite roles.

{posts_block}

Respond with ONLY a JSON array:
[ {{"index": 1, "relevant": true/false, "reason": "..."}} ]"""

        log.info(f"  [Stage II LLM] Relevancy check for {len(batch)} posts...")
        raw = call_gemini(prompt) 
        if not raw:
            passed.extend(batch); continue

        try:
            cleaned = re.sub(r"^.*?\[", "[", raw.strip(), flags=re.DOTALL)
            cleaned = re.sub(r"\].*?$", "]", cleaned, flags=re.DOTALL)
            evals = json.loads(cleaned)
            for ev in evals:
                idx = ev.get("index", 0) - 1
                if 0 <= idx < len(batch):
                    if ev.get("relevant"):
                        passed.append(batch[idx])
                        log.info(f"    Post {idx+1}: ✅")
                    else:
                        log.info(f"    Post {idx+1}: ❌ {ev.get('reason')}")
        except: passed.extend(batch)
        time.sleep(3)

    log.info(f"  Stage II results: {len(passed)} passed.")
    return passed


# ═══════════════════════════════════════════════
# STEP 5: CONTACT EXTRACTION (gemini-2.5-flash)
# ═══════════════════════════════════════════════

def call_gemini(prompt: str) -> str:
    """Call Gemini with extraction API keys (same retry logic as saved_posts_outreach.py)."""
    import requests

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{CONFIG['GEMINI_MODEL']}:generateContent"
    )

    keys = [
        {"key": CONFIG["GEMINI_API_KEY"], "retries": 1, "name": "Primary"},
        {"key": CONFIG["ALTERNATIVE_GEMINI_API_KEY"], "retries": 1, "name": "Alt 1"},
        {"key": CONFIG["SECOND_ALTERNATIVE_GEMINI_API_KEY"], "retries": 1, "name": "Alt 2"},
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
                        log.warning(f"  {k_info['name']}: Rate limited. Retry in {wait}s...")
                        time.sleep(wait)
                        continue
                    else:
                        break

                resp.raise_for_status()
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]

            except Exception as e:
                log.error(f"  {k_info['name']} error: {e}")
                if attempt == max_attempts:
                    break
                time.sleep(2)

    log.critical("All extraction Gemini keys failed.")
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
        msg["Subject"] = "⚠️ Gemini API Rate Limit Alert (Search Outreach)"
        body = "All your Gemini API keys have hit their rate limits or failed during the keyword search run."
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
Your LinkedIn Search Outreach Agent encountered an authentication error ({status_code}). 
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


def extract_contacts_with_gemini(items: list[dict]) -> list[dict]:
    """Extract emails, phones, names, roles using Gemini (same logic as saved_posts_outreach.py)."""
    items_with_content = [s for s in items if s.get("full_content")]
    if not items_with_content:
        return []

    batch_size = 5
    all_extracted = []

    for batch_start in range(0, len(items_with_content), batch_size):
        batch = items_with_content[batch_start:batch_start + batch_size]

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
- Email addresses: Extract the primary contact email. Look for obfuscated formats.
- Phone numbers: Extract the primary phone number (India +91 or others).
- Poster Name: Identify the person who shared the post or the contact person.
- Role & Company: Identify the job title and the hiring company.

{posts_block}

Respond with ONLY a JSON array of objects (one per post). If info is missing, use null.
{{
    "index": <1-based index>,
    "poster_name": "<full name>",
    "poster_email": "<email>",
    "poster_phone": "<phone like +918077593119>",
    "company": "<company name>",
    "role_title": "<job title>",
    "role_summary": "<1-sentence summary>",
    "has_contact_info": <true/false>
}}"""

        batch_num = batch_start // batch_size + 1
        total_batches = (len(items_with_content) + batch_size - 1) // batch_size
        log.info(f"  [Gemini {batch_num}/{total_batches}] Extracting from {len(batch)} posts...")

        raw = call_gemini(prompt)
        try:
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
                        f"    {(ev.get('poster_name') or '?')[:25]:25s} | "
                        f"email: {email:30s} | phone: {phone}"
                    )
        except json.JSONDecodeError as e:
            log.warning(f"  JSON parse error: {e}")

        time.sleep(2)

    log.info(f"\nExtracted info from {len(all_extracted)} posts")
    return all_extracted


# ═══════════════════════════════════════════════
# HISTORY, GMAIL, AND OUTREACH (shared with saved_posts_outreach.py)
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
    seen_ids = _normalize_seen_urns(seen_urns)
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
        log.info(f"  Dedup: skipped {skipped} already-contacted posts")
    return fresh


def record_contact(history, urn, email=None, url=None, thread_id=None, message_id=None):
    if urn and urn not in history["contacted_urns"]:
        history["contacted_urns"].append(urn)
        if "contacted_details" not in history:
            history["contacted_details"] = []
        history["contacted_details"].append({
            "urn": urn, "url": url, "email": email,
            "thread_id": thread_id, "message_id": message_id,
            "followed_up": False, "source": "search",
            "timestamp": datetime.now().isoformat()
        })
    if email and email not in history["contacted_emails"]:
        history["contacted_emails"].append(email)


def get_gmail_service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    token_path = Path(CONFIG["GMAIL_TOKEN_FILE"])
    creds_path = Path(CONFIG["GMAIL_CREDENTIALS_FILE"])

    token_env = os.getenv("GMAIL_TOKEN_JSON")
    if token_env:
        token_path.write_text(token_env)

    creds_env = os.getenv("GMAIL_CREDENTIALS_JSON")
    if creds_env and not creds_path.exists():
        creds_path.write_text(creds_env)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), CONFIG["GMAIL_SCOPES"])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        else:
            if not creds_path.exists():
                log.error(f"Missing {creds_path}")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), CONFIG["GMAIL_SCOPES"])
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def extract_first_name(full_name: str) -> str:
    if not full_name or full_name == "?":
        return None
    clean_name = re.sub(r"^(Mr\.|Ms\.|Mrs\.|Dr\.)\s+", "", full_name, flags=re.I)
    parts = clean_name.split()
    if not parts:
        return None
    first_name = parts[0].strip()
    if len(first_name) <= 1 or not first_name.isalpha():
        return None
    return first_name.capitalize()


def send_one_email(service, to_email, name, role_title, post_url=""):
    msg = MIMEMultipart()
    msg["From"] = f"{CONFIG['SENDER_NAME']} <{CONFIG['SENDER_EMAIL']}>"
    msg["To"] = to_email
    msg["Subject"] = EMAIL_TEMPLATE["subject"]

    first_name = extract_first_name(name)
    body = EMAIL_TEMPLATE["body"].format(
        recipient_name=first_name or "Team",
        role_title=role_title or "AI/ML Engineer",
        sender_name=CONFIG["SENDER_NAME"],
        post_url=post_url or "N/A",
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

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def auto_send(results: list[dict], dry_run: bool = False) -> list[dict]:
    """Send outreach emails. Reuses shared history."""
    history = load_history()
    results = dedupe_against_history(results, history)
    if not results:
        log.info("  No new leads to contact.")
        return []

    with_email = [r for r in results if r.get("poster_email")]
    phone_leads = [r for r in results if r.get("poster_phone")]

    log.info(f"\n  {len(with_email)} with email, {len(phone_leads)} with phone")

    if dry_run:
        for r in with_email:
            log.info(f"    [DRY RUN] Would send to: {r.get('poster_name','?')} <{r['poster_email']}>")
        return []

    gmail = get_gmail_service()
    emailed = []

    for r in with_email:
        try:
            urn = r.get("post_urn") or r.get("entity_urn", "")
            url = f"https://www.linkedin.com/feed/update/{urn}" if urn else ""
            resp = send_one_email(gmail, r["poster_email"], r.get("poster_name", ""), r.get("role_title", ""), url)
            thread_id = resp.get("threadId")
            message_id = resp.get("id")
            log.info(f"  Sent -> {r['poster_email']} ({r.get('poster_name', '?')})")
            record_contact(history, urn, r["poster_email"], url, thread_id, message_id)
            emailed.append(r)
            time.sleep(1)
        except Exception as e:
            log.error(f"  FAILED {r.get('poster_email', '?')}: {e}")

    save_history(history)
    log.info(f"\n  Summary: {len(emailed)} new emails sent.")
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
            ["git", "commit", "-m", f"search run {ts}"],
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
# SAVE RESULTS
# ═══════════════════════════════════════════════

def save_run(search_results, stage1_passed, stage2_passed, extracted, emailed):
    out_dir = Path(CONFIG["RESULTS_DIR"])
    out_dir.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    data = {
        "timestamp": datetime.now().isoformat(),
        "mode": "search",
        "search_phrases": CONFIG["SEARCH_PHRASES"],
        "stats": {
            "raw_search_results": len(search_results),
            "stage1_passed": len(stage1_passed),
            "stage2_passed": len(stage2_passed),
            "contacts_extracted": len(extracted),
            "emailed": len(emailed),
        },
        "all_scraped_posts": [
            {
                "urn": item.get("post_urn"),
                "url": item.get("post_url"),
                "content": item.get("full_content")
            } for item in search_results
        ],
        "extracted": [{k: v for k, v in item.items() if k != "raw_data"} for item in extracted],
    }

    path = out_dir / f"search_run_{ts}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    log.info(f"Results saved: {path}")


# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="LinkedIn Keyword Search Outreach Agent")
    parser.add_argument("--dry-run", action="store_true", help="Skip sending emails")
    parser.add_argument("--no-git-sync", action="store_true", help="Skip auto git pull/push")
    parser.add_argument("--test", action="store_true", help="Test mode: skip emails AND git sync")
    parser.add_argument("--use-cffi", action="store_true", help="Use curl_cffi for Chrome TLS fingerprint")
    args = parser.parse_args()

    # --test is shorthand for --dry-run + --no-git-sync
    if args.test:
        args.dry_run = True
        args.no_git_sync = True

    # Validate required env vars
    missing = [k for k, v in CONFIG.items()
               if isinstance(v, str) and v.startswith("YOUR_") and k in ("LINKEDIN_LI_AT", "LINKEDIN_JSESSIONID")]
    if missing:
        log.error(f"Set these env vars: {', '.join(missing)}")
        sys.exit(1)

    mode = "test" if args.test else ("dry-run" if args.dry_run else "full")
    print(f"\n{'='*70}")
    print(f"  LinkedIn Keyword Search Outreach Agent")
    print(f"  Phrases: {CONFIG['SEARCH_PHRASES']}")
    print(f"  Mode: {mode}")
    print(f"{'='*70}")

    # ── 0. Git sync (pull latest history) ──
    if not args.no_git_sync:
        log.info("\n[STEP 0] Syncing history from GitHub...")
        git_sync_pull()

    # ── 1. Search LinkedIn ──
    log.info("\n[STEP 1] Searching LinkedIn for keyword posts...")
    session = create_linkedin_session(use_cffi=args.use_cffi)
    history = load_history()

    # Decoy disabled — extra API calls trigger session invalidation
    # decoy_request(session)

    search_results = fetch_all_search_results(session, history)

    if not search_results:
        log.info("No new search results found.")
        if not args.no_git_sync:
            git_sync_push()
        return

    # ── 2. Fetch full content ──
    log.info(f"\n[STEP 2] Fetching full content for {len(search_results)} posts...")
    with_content = fetch_all_post_contents(session, search_results)

    if not with_content:
        log.info("Failed to fetch content for any posts.")
        if not args.no_git_sync:
            git_sync_push()
        return

    # ── 3. Stage I: Keyword & Contact filter ──
    log.info(f"\n[STEP 3] Stage I — Filtering ({len(with_content)} posts)...")
    stage1_passed = stage_i_filter(with_content)

    if not stage1_passed:
        log.info("No posts passed Stage I filter.")
        save_run(search_results, [], [], [], [])
        if not args.no_git_sync:
            git_sync_push()
        return

    # ── 4. Stage II: LLM filter ──
    log.info(f"\n[STEP 4] Stage II — LLM relevancy filter ({len(stage1_passed)} posts)...")
    stage2_passed = stage_ii_llm_filter(stage1_passed)

    if not stage2_passed:
        log.info("No posts passed Stage II LLM filter.")
        save_run(search_results, stage1_passed, [], [], [])
        if not args.no_git_sync:
            git_sync_push()
        return

    # ── 5. Extract contacts ──
    log.info(f"\n[STEP 5] Extracting contacts from {len(stage2_passed)} posts...")
    extracted = extract_contacts_with_gemini(stage2_passed)

    # Filter out posts without any contact info
    extracted = [e for e in extracted if e.get("poster_email") or e.get("poster_phone")]

    if not extracted:
        log.info("No contacts extracted.")
        save_run(search_results, stage1_passed, stage2_passed, [], [])
        if not args.no_git_sync:
            git_sync_push()
        return

    # ── 6. Auto-send ──
    log.info(f"\n[STEP 6] Sending outreach emails...")
    emailed = auto_send(extracted, dry_run=args.dry_run)

    # ── 7. Save results ──
    save_run(search_results, stage1_passed, stage2_passed, extracted, emailed)

    # ── 8. Git sync (push updated history) ──
    if not args.no_git_sync:
        log.info("\n[STEP 8] Pushing updated history to GitHub...")
        git_sync_push()

    email_count = len([r for r in extracted if r.get("poster_email")])
    phone_count = len([r for r in extracted if r.get("poster_phone")])
    print(f"\n{'='*70}")
    print(f"  SEARCH RUN COMPLETE")
    print(f"  Raw results:    {len(search_results)}")
    print(f"  Stage I pass:   {len(stage1_passed)}")
    print(f"  Stage II pass:  {len(stage2_passed)}")
    print(f"  With email:     {email_count} (sent: {len(emailed)})")
    print(f"  With phone:     {phone_count}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
