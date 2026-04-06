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
    "GEMINI_MODEL": "gemini-2.5-pro", 
    "RESUME_CONFIG": "resume_config.json",
    "RESUME_TEMPLATE": "resume_template.html",
    "OUTPUT_PDF": "Tailored_Resume_Adarsh_Bansal.pdf",
    
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

def fetch_jd_from_linkedin(urn: str) -> str:
    """Fetch job description from LinkedIn using Voyager API."""
    log.info(f"Fetching JD for URN: {urn}...")
    
    # If URN is just numeric, prefix it
    if urn.isdigit():
        urn = f"urn:li:activity:{urn}"
        
    session = requests.Session()
    session.cookies.set("li_at", CONFIG["LINKEDIN_LI_AT"], domain=".linkedin.com")
    session.cookies.set("JSESSIONID", f'"{CONFIG["LINKEDIN_JSESSIONID"].strip(chr(34))}"', domain=".linkedin.com")
    
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "csrf-token": CONFIG["LINKEDIN_JSESSIONID"].strip(chr(34)),
        "Accept": "application/vnd.linkedin.normalized+json+2.1",
    })
    
    # Try fetching as activity
    activity_id = re.search(r"(\d{10,})", urn).group(1)
    url = f"https://www.linkedin.com/voyager/api/feed/updates?q=activityByUrn&activityUrn=urn%3Ali%3Aactivity%3A{activity_id}"
    
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            log.error(f"Failed to fetch activity {activity_id}: {resp.status_code}")
            return ""
        
        data = resp.json()
        # Find commentary text
        def find_text(obj):
            if isinstance(obj, dict):
                if "text" in obj and isinstance(obj["text"], str) and len(obj["text"]) > 50:
                    return obj["text"]
                for v in obj.values():
                    res = find_text(v)
                    if res: return res
            elif isinstance(obj, list):
                for i in obj:
                    res = find_text(i)
                    if res: return res
            return None
        
        return find_text(data) or ""
    except Exception as e:
        log.error(f"Error fetching JD: {e}")
        return ""

# ═══════════════════════════════════════════════
# STEP 2: TAILOR WITH GEMINI
# ═══════════════════════════════════════════════

def call_gemini(prompt: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{CONFIG['GEMINI_MODEL']}:generateContent?key={CONFIG['GEMINI_API_KEY']}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"}
    }
    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        log.error(f"Gemini error: {e}")
        return ""

def tailor_resume(base_resume: dict, jd_text: str) -> dict:
    """Use Gemini to tailor bullets and calculate scores."""
    log.info("Tailoring resume with Gemini...")
    
    prompt = f"""
You are an expert Resume Strategist and ATS Optimizer. Your goal is to tailor the provided resume JSON to match the Job Description (JD).

### GUIDELINES:
1.  **Preserve Integrity:** DO NOT invent experiences or change company names/dates.
2.  **Highlight Relevancy:** Rephrase bullet points to emphasize skills, tools, and outcomes mentioned in the JD. Use JD keywords naturally.
3.  **Optimize Skills:** Update the "Skills" section categories or tool order to prioritize what the JD asks for.
4.  **Semantic Scoring:** Provide a semantic match score (0-100) and identify "Keyword Gaps" (essential JD skills missing from the resume).
5.  **Output Format:** Return a JSON object with two top-level keys:
    - "tailored_resume": The updated resume JSON matching the original structure.
    - "analysis": {{ "match_score": 85, "keyword_gaps": ["...", "..."], "tailoring_notes": "..." }}

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
    
    # 4. Generate PDF
    generate_pdf(tailored_data, CONFIG["RESUME_TEMPLATE"], CONFIG["OUTPUT_PDF"])
    
    # 5. Email
    send_resume_email(CONFIG["OUTPUT_PDF"], analysis)

if __name__ == "__main__":
    main()
