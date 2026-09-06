#!/usr/bin/env python3
"""
Global Independent Club Crawler & AI Parser
Discovers career pages across 28,000+ domains, detects hash changes, and uses AI to extract jobs.
"""

import csv
import sqlite3
import urllib.request
import urllib.parse
import urllib.error
import re
import hashlib
import os
import json
import time
import concurrent.futures
from datetime import datetime, timezone
from xml.sax.saxutils import escape

# --- CONFIGURATION ---
CSV_FILE = "Data Golf Clubs September 2020.xlsx - Sheet1.csv"
DB_FILE = "clubs.db"
OUTPUT_XML = "public/independent_clubs.xml"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
MAX_WORKERS = 2  # Reduced to 2 to respect Gemini's 15 RPM free tier limit
TIMEOUT = 10

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS clubs
                 (url TEXT PRIMARY KEY, name TEXT, country TEXT, career_url TEXT, last_hash TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS jobs
                 (job_reference TEXT PRIMARY KEY, title TEXT, company TEXT, apply_url TEXT, 
                  description TEXT, location TEXT, category TEXT, pubDate TEXT)''')
    conn.commit()
    return conn

def load_csv_to_db(conn):
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM clubs")
    if c.fetchone()[0] > 0:
        return # DB already populated

    print("Importing 28,000+ global clubs into local SQLite database...")
    with open(CSV_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = row.get('Website', '').strip()
            if url and url.startswith('http'):
                c.execute("INSERT OR IGNORE INTO clubs (url, name, country, career_url, last_hash) VALUES (?, ?, ?, ?, ?)",
                          (url, row.get('Club Name', 'Unknown Club'), row.get('Country', ''), "", ""))
    conn.commit()

# --- PHASE 1: DISCOVERY ---
def discover_career_page(club_url):
    try:
        req = urllib.request.Request(club_url, headers={'User-Agent': USER_AGENT})
        html = urllib.request.urlopen(req, timeout=TIMEOUT).read().decode('utf-8', errors='ignore')
        
        # Look for standard career links in the HTML
        links = re.finditer(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
        keywords = ['career', 'job', 'vacanc', 'employment', 'work-with']
        
        for m in links:
            link = m.group(1)
            if any(k in link.lower() for k in keywords):
                return urllib.parse.urljoin(club_url, link)
                
        # CMS Probing: If no link is found, blindly test standard paths
        probe_url = urllib.parse.urljoin(club_url, "/vacancies")
        req = urllib.request.Request(probe_url, headers={'User-Agent': USER_AGENT})
        if urllib.request.urlopen(req, timeout=TIMEOUT).getcode() == 200:
            return probe_url
            
    except Exception:
        pass
    return None

# --- PHASE 2: HASHING & EXTRACTION ---
def fetch_and_hash(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        html = urllib.request.urlopen(req, timeout=TIMEOUT).read().decode('utf-8', errors='ignore')
        
        # Strip HTML tags to get raw text for hashing and AI parsing
        raw_text = re.sub(r'<[^>]+>', ' ', html)
        raw_text = re.sub(r'\s+', ' ', raw_text).strip()
        
        content_hash = hashlib.sha256(raw_text.encode('utf-8')).hexdigest()
        return raw_text, content_hash
    except Exception:
        return None, None

# --- PHASE 3: AI PARSING ---
def ai_extract_jobs(raw_text, company_name, apply_url):
    if not GEMINI_API_KEY:
        print("No Gemini API key found. Skipping AI extraction.")
        return []
        
    time.sleep(4) # Force a 4-second delay to stay under the 15 RPM limit
    
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""
    You are a data extraction bot. Read the following text from the career page of {company_name}.
    If there are actual job vacancies listed, extract them and return them as a strictly formatted JSON array of objects. 
    If there are no jobs, or if it is just a general 'we are always looking for talent' message, return an empty array [].
    
    JSON Object format:
    {{
      "title": "Job Title",
      "category": "Department (e.g., Agronomy, F&B, Golf Operations)",
      "description": "A short 2-3 sentence summary of the role."
    }}
    
    Text to analyze:
    {raw_text[:8000]} 
    """
    
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    req = urllib.request.Request(api_url, data=json.dumps(payload).encode('utf-8'), method="POST")
    req.add_header("Content-Type", "application/json")
    
    try:
        response = urllib.request.urlopen(req, timeout=20)
        data = json.loads(response.read().decode('utf-8'))
        text_resp = data['candidates'][0]['content']['parts'][0]['text']
        
        # Clean markdown code blocks from AI response
        text_resp = text_resp.replace('```json', '').replace('```', '').strip()
        jobs_data = json.loads(text_resp)
        return jobs_data
    except Exception as e:
        print(f"AI Parse Error for {company_name}: {e}")
        return []

# --- PHASE 4: THE PRODUCTION PIPELINE ---
def process_club(club_data):
    base_url, name, country, stored_career_url, last_hash = club_data
    
    # 1. Discover
    career_url = stored_career_url
    if not career_url:
        career_url = discover_career_page(base_url)
        if not career_url: return None, base_url, "", "" # No page found
        
    # 2. Hash
    raw_text, current_hash = fetch_and_hash(career_url)
    if not raw_text or current_hash == last_hash:
        return None, base_url, career_url, current_hash # Unchanged or failed
        
    # 3. AI Parse
    jobs = ai_extract_jobs(raw_text, name, career_url)
    
    extracted_jobs = []
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    for j in jobs:
        ref = hashlib.md5(f"{base_url}-{j.get('title')}".encode('utf-8')).hexdigest()
        extracted_jobs.append({
            "job_reference": ref,
            "title": j.get("title", ""),
            "company": name,
            "apply_url": career_url,
            "description": f"{j.get('description', '')} <p>Apply directly via the {name} careers page.</p>",
            "location": country,
            "category": j.get("category", ""),
            "pubDate": now
        })
        
    return extracted_jobs, base_url, career_url, current_hash

def build_rss(conn):
    c = conn.cursor()
    c.execute("SELECT title, company, location, category, description, apply_url, job_reference, pubDate FROM jobs")
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0">', '  <channel>',
           '    <title>Global Independent Golf Clubs Feed</title>', '    <link>https://www.golf-jobs.com</link>',
           '    <description>Jobs extracted via AI from 28,000+ independent club websites</description>', f'    <lastBuildDate>{now}</lastBuildDate>']
           
    for row in c.fetchall():
        out.extend([
            "    <item>", f"      <title>{escape(row[0])}</title>", f"      <company>{escape(row[1])}</company>",
            f"      <location>{escape(row[2])}</location>", f"      <category>{escape(row[3])}</category>",
            f"      <description><![CDATA[{row[4]}]]></description>", f"      <link>{escape(row[5])}</link>",
            f"      <apply_url>{escape(row[5])}</apply_url>", f"      <job_reference>{escape(row[6])}</job_reference>",
            f"      <guid isPermaLink=\"false\">{escape(row[6])}</guid>", f"      <pubDate>{row[7]}</pubDate>", "    </item>"
        ])
    out.extend(["  </channel>", "</rss>"])
    
    os.makedirs(os.path.dirname(OUTPUT_XML), exist_ok=True)
    with open(OUTPUT_XML, "w", encoding="utf-8") as f:
        f.write("\n".join(out))

def main():
    conn = init_db()
    load_csv_to_db(conn)
    c = conn.cursor()
    
    c.execute("SELECT url, name, country, career_url, last_hash FROM clubs")
    clubs = c.fetchall()
    
    print(f"Beginning scan of {len(clubs)} domains...")
    
    updates_to_make = []
    jobs_to_insert = []
    
    # Run the workload concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = executor.map(process_club, clubs)
        
        for result in results:
            if not result: continue
            extracted_jobs, base_url, career_url, current_hash = result
            
            if career_url:
                updates_to_make.append((career_url, current_hash, base_url))
                
            if extracted_jobs:
                print(f"Found {len(extracted_jobs)} jobs at {base_url}")
                for j in extracted_jobs:
                    jobs_to_insert.append((j["job_reference"], j["title"], j["company"], j["apply_url"], 
                                           j["description"], j["location"], j["category"], j["pubDate"]))

    # Update Database
    c.executemany("UPDATE clubs SET career_url = ?, last_hash = ? WHERE url = ?", updates_to_make)
    c.execute("DELETE FROM jobs") # Clear old jobs before inserting new batch
    c.executemany("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", jobs_to_insert)
    conn.commit()
    
    build_rss(conn)
    print(f"Scan complete. Wrote jobs to {OUTPUT_XML}")

if __name__ == "__main__":
    main()
