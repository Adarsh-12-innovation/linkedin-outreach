"""
Resume Tailoring Agent
======================
Tailors a base resume (JSON) to match a specific Job Description (JD).
Uses Gemini 2.5 Pro for intelligent rephrasing and skill alignment.
Generates a polished PDF and emails it to the user.
"""

import os
import json
import argparse
import logging
import requests
import base64
import time
import re
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

import jinja2
from weasyprint import HTML
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

CONFIG = {
    "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY"),
    "ALTERNATIVE_GEMINI_API_KEY": os.getenv("ALTERNATIVE_GEMINI_API_KEY"),
    "SECOND_ALTERNATIVE_GEMINI_API_KEY": os.getenv("SECOND_ALTERNATIVE_GEMINI_API_KEY"),
    # "GEMINI_MODEL": "gemini-2.5-flash", 
    "GEMINI_MODEL": "gemma-4-31b-it", 
    "RESUME_CONFIG": "resume_config.json",
    "RESUME_TEMPLATE": "resume_template.html",
    "OUTPUT_PDF": "Adarsh_Bansal_CV_2026_CTM.pdf",
    
    # LinkedIn (needed for fetching JD if URN is provided)
    "LINKEDIN_LI_AT": os.getenv("LINKEDIN_LI_AT"),
    "LINKEDIN_JSESSIONID": os.getenv("LINKEDIN_JSESSIONID"),
    
    # Gmail
    "SENDER_EMAIL": "adarshbansal1995@gmail.com",
    "GMAIL_TOKEN_FILE": "token.json",
    "GMAIL_CREDENTIALS_FILE": "credentials.json",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tailor-resume")

# ═══════════════════════════════════════════════
# STEP 1: FETCH JD
# ═══════════════════════════════════════════════

def create_linkedin_session() -> requests.Session:
    """Create an authenticated LinkedIn session using li_at + JSESSIONID cookies."""
    session = requests.Session()

    # Strip any potential whitespace or newlines from GitHub secrets
    li_at = (CONFIG["LINKEDIN_LI_AT"] or "").strip()
    jsessionid = (CONFIG["LINKEDIN_JSESSIONID"] or "").strip().strip('"')

    if not li_at or not jsessionid:
        log.error("LinkedIn credentials (LI_AT or JSESSIONID) are missing!")
        return session

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

def fetch_jd_from_linkedin(urn: str) -> str:
    """
    Fetch the full text content of a LinkedIn post.
    Matches the exact logic from saved_posts_outreach.py.
    """
    log.info(f"Fetching JD for URN: {urn}...")
    
    activity_id = None
    # Extract numeric ID from various URN formats or bare numbers
    match = re.search(r"(\d{10,})", urn)
    if match: activity_id = match.group(1)
    
    if not activity_id:
        log.error(f"Could not extract numeric activity ID from URN: {urn}")
        return ""

    session = create_linkedin_session()
    
    # Primary Voyager REST endpoint with decoration
    url = (
        f"https://www.linkedin.com/voyager/api/feed/updates"
        f"?decorationId=com.linkedin.voyager.deco.feed.FeedUpdate-4"
        f"&q=activityByUrn"
        f"&activityUrn=urn%3Ali%3Aactivity%3A{activity_id}"
    )

    try:
        # Disable redirects to avoid 30-redirect loop and see actual status
        resp = session.get(url, timeout=15, allow_redirects=False)
        
        if resp.status_code in (301, 302, 303, 307, 308):
            log.error(f"LinkedIn redirected to: {resp.headers.get('Location')}")
            log.error("This usually means your cookies are invalid or expired.")
            return ""

        if resp.status_code != 200:
            # Fallback endpoint
            url2 = f"https://www.linkedin.com/voyager/api/feed/updates/urn:li:activity:{activity_id}"
            resp = session.get(url2, timeout=15, allow_redirects=False)
            if resp.status_code != 200:
                log.error(f"Failed to fetch activity {activity_id}. Status: {resp.status_code}")
                return ""

        data = resp.json()
    except Exception as e:
        log.error(f"Error during network request: {e}")
        return ""

    text_parts = []
    
    # Forbidden keys filter to remove social noise
    FORBIDDEN_KEYS = {
        "socialDetail", "socialContent", "comments", "actions", 
        "updateAction", "socialDetailEntity", "attributes", "reactions",
        "followingInfo", "tracking", "footer", "feedbackDetail", "header"
    }

    def extract_texts(obj, depth=0):
        if depth > 15: return
        if isinstance(obj, dict):
            for key in ("text", "commentary", "translationText"):
                val = obj.get(key)
                if isinstance(val, str) and len(val) > 10:
                    text_parts.append(val)
                elif isinstance(val, dict) and "text" in val:
                    if isinstance(val["text"], str) and len(val["text"]) > 10:
                        text_parts.append(val["text"])
            
            for k, v in obj.items():
                if k not in FORBIDDEN_KEYS:
                    extract_texts(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                extract_texts(item, depth + 1)

    extract_texts(data)
    content = "\n\n".join(text_parts)
    
    if not content:
        log.warning("Post data was returned, but no text content could be extracted.")
        
    return content

# ═══════════════════════════════════════════════
# STEP 2: TAILOR WITH GEMINI
# ═══════════════════════════════════════════════

def call_gemini(prompt: str) -> str:
    """
    Call Gemini API with specialized retry/rotation logic.
    """
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{CONFIG['GEMINI_MODEL']}:generateContent"
    )
    
    keys = [
        {"key": CONFIG["GEMINI_API_KEY"], "retries": 2, "name": "Primary"},
        {"key": CONFIG["ALTERNATIVE_GEMINI_API_KEY"], "retries": 2, "name": "Alternative 1"},
        {"key": CONFIG["SECOND_ALTERNATIVE_GEMINI_API_KEY"], "retries": 2, "name": "Alternative 2"},
    ]

    for k_info in keys:
        current_key = k_info["key"]
        if not current_key or current_key.startswith("YOUR_") or len(current_key) < 10:
            log.debug(f"Skipping {k_info['name']} (missing or invalid).")
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
                            "temperature": 0.2,
                            "response_mime_type": "application/json"
                        }
                    },
                    timeout=120,
                )

                if resp.status_code == 429:
                    wait = 30 * attempt # Increased wait for Pro model (2 RPM limit)
                    if attempt < max_attempts:
                        log.warning(f"  {k_info['name']} Key: Rate limited (429). Waiting {wait}s before retry {attempt}/{k_info['retries']}...")
                        time.sleep(wait)
                        continue
                    else:
                        log.warning(f"  {k_info['name']} Key: Exhausted retries. Rotating to next key...")
                        break 

                resp.raise_for_status()
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
                
            except Exception as e:
                log.error(f"  {k_info['name']} Key Error: {e}")
                if attempt == max_attempts:
                    break
                time.sleep(5)

    return ""

def tailor_resume(base_resume: dict, jd_text: str) -> dict:
    """Use Gemini to tailor bullets and calculate scores."""
    log.info("Tailoring resume with Gemini...")
    
    prompt = f"""
You are an expert Resume Strategist and ATS Optimizer specializing in Stealth Keyword Integration and Space Optimization. 

### GOAL:
Tailor the provided resume JSON to match the Job Description (JD) so perfectly that it achieves a near-100% ATS score without looking forced or "synthetic."

### CORE STRATEGIES:
1.  **Stealth Keyword Insertion:** 
    - Identify every critical technology, tech stack, and skill in the JD missing from the resume.
    - **Intentionally insert** these missing keywords into the "Professional Experience" or "Academic Projects" sections.
    - **Replace** generic or less relevant keywords in the original resume with these specific JD keywords if they serve a similar technical purpose (e.g., if JD asks for 'FastAPI' and resume has 'Flask', update the project to use 'FastAPI' if plausible, or mention both).
    - Ensure the integration "gels" naturally with the surrounding context of the project. It must look like you actually used those tools to solve the described problem.

2.  **Strict 1-Page Constraint (Line-Level Control):**
    - The output MUST fit on a single A4 page.
    - **Consolidate:** If a role has too many bullets, merge 2-3 shorter ones into a single, high-impact technical sentence.
    - **Prioritize:** Keep 2-3 strong bullets for recent/relevant roles; limit older or less relevant roles to 1 impactful bullet.
    - **Prune:** Sacrifice low-value metrics or "fluff" phrases to make room for the mandatory JD keywords.
    - **Skills Section:** Group the "Tools & Technologies" aggressively to save vertical space.

3.  **Impact & Bolding:**
    - Use HTML <b> tags to bold the newly inserted JD keywords and key metrics.
    - Ensure the tone remains professional, senior-level, and outcome-oriented.

### GUIDELINES:
- **Integrity:** Maintain company names and dates exactly as they are.
- **Output Format:** Return a JSON object with:
    - "tailored_resume": The updated JSON.
    - "analysis": {{ "match_score": 0-100, "keyword_gaps_filled": ["list of JD keywords you inserted"], "optimization_notes": "how you saved space" }}

### BASE RESUME JSON:
{json.dumps(base_resume, indent=2)}

### JOB DESCRIPTION:
{jd_text}

### OUTPUT:
Respond ONLY with the JSON object.
"""
    
    raw_response = call_gemini(prompt)
    if not raw_response:
        return {"tailored_resume": base_resume, "analysis": {"match_score": 0, "keyword_gaps": [], "tailoring_notes": "API Error"}}
    
    try:
        # Clean up possible markdown wrapper
        cleaned = re.sub(r"^.*?\{", "{", raw_response.strip(), flags=re.DOTALL)
        cleaned = re.sub(r"\}\s*$", "}", cleaned, flags=re.DOTALL)
        return json.loads(cleaned)
    except Exception as e:
        log.error(f"JSON Parse Error: {e}")
        return {"tailored_resume": base_resume, "analysis": {"match_score": 0, "keyword_gaps": [], "tailoring_notes": "Parse Error"}}

# ═══════════════════════════════════════════════
# STEP 3: GENERATE PDF
# ═══════════════════════════════════════════════

def generate_pdf(data: dict, template_path: str, output_path: str):
    log.info(f"Generating PDF: {output_path}...")
    template_loader = jinja2.FileSystemLoader(searchpath="./")
    template_env = jinja2.Environment(loader=template_loader)
    template = template_env.get_template(template_path)
    
    html_out = template.render(data)
    
    # Save temporary HTML for debugging (optional)
    # with open("debug.html", "w", encoding="utf-8") as f:
    #     f.write(html_out)
        
    HTML(string=html_out).write_pdf(output_path)
    log.info("PDF generated successfully.")

# ═══════════════════════════════════════════════
# STEP 4: EMAIL
# ═══════════════════════════════════════════════

def get_gmail_service():
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    
    # Try to load from env vars first (for CI)
    token_json = os.getenv("GMAIL_TOKEN_JSON")
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
    else:
        creds = Credentials.from_authorized_user_file(CONFIG["GMAIL_TOKEN_FILE"])
        
    return build("gmail", "v1", credentials=creds)

def send_resume_email(pdf_path: str, analysis: dict):
    log.info(f"Emailing tailored resume to {CONFIG['SENDER_EMAIL']}...")
    service = get_gmail_service()
    
    msg = MIMEMultipart()
    msg["From"] = f"Resume Tailor <{CONFIG['SENDER_EMAIL']}>"
    msg["To"] = CONFIG["SENDER_EMAIL"]
    msg["Subject"] = f"Tailored Resume: Match Score {analysis.get('match_score', '?')}%"
    
    body = f"""
Hi Adarsh,

Your tailored resume is ready!

### Analysis:
- **Match Score:** {analysis.get('match_score', '?')}%
- **Keyword Gaps:** {', '.join(analysis.get('keyword_gaps', []))}
- **Notes:** {analysis.get('tailoring_notes', 'N/A')}

The PDF is attached.

Best regards,
Resume Tailoring Agent
"""
    msg.attach(MIMEText(body, "plain"))
    
    with open(pdf_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={Path(pdf_path).name}")
        msg.attach(part)
        
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info("Email sent.")

# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Tailor Resume to JD")
    parser.add_argument("--urn", type=str, help="LinkedIn Post URN")
    parser.add_argument("--jd", type=str, help="Full text of Job Description")
    args = parser.parse_args()
    
    if not args.urn and not args.jd:
        log.error("Either --urn or --jd must be provided.")
        return

    # 1. Load base resume
    with open(CONFIG["RESUME_CONFIG"]) as f:
        base_resume = json.load(f)
        
    # 2. Get JD
    jd_text = args.jd
    if args.urn:
        linkedin_jd = fetch_jd_from_linkedin(args.urn)
        if linkedin_jd:
            jd_text = linkedin_jd
        elif not jd_text:
            log.error("Could not fetch JD from LinkedIn and no fallback text provided.")
            return

    # 3. Tailor
    result = tailor_resume(base_resume, jd_text)
    tailored_data = result.get("tailored_resume")
    analysis = result.get("analysis", {})
    
    # SAFETY HALT: If Gemini failed, do not generate/send a broken resume
    if analysis.get("match_score") == 0 or analysis.get("tailoring_notes") == "API Error":
        log.critical("Resume tailoring failed (API Error or 0% match). Check Gemini quota and logs.")
        log.error("Aborting PDF generation and email to prevent sending un-tailored resume.")
        sys.exit(1)
    
    # 4. Generate PDF
    generate_pdf(tailored_data, CONFIG["RESUME_TEMPLATE"], CONFIG["OUTPUT_PDF"])
    
    # 5. Email
    send_resume_email(CONFIG["OUTPUT_PDF"], analysis)

if __name__ == "__main__":
    main()
