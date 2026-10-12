#!/usr/bin/env python3
"""
Global Independent Club Crawler & AI Parser
Discovers career pages, uses AI to extract jobs, features a self-cleaning dead-link database, and maintains 1:1 XML parity.
Strict taxonomy enforcement added to prevent AI hallucinations.
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

# Automatically strips invisible spaces from the GitHub Secret
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip() 

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
MAX_WORKERS = 2  
TIMEOUT = 10
MAX_RUNTIME_SECONDS = (5 * 3600) + (15 * 60) # 5 hours and 15 minutes

# --- STRICT TAXONOMY MAPPERS ---
def map_category(raw_cat, title, desc):
    search_text = f"{str(raw_cat)} {str(title)}".lower()
    if any(k in search_text for k in ["agronomy", "turf", "greenkeep", "grounds", "landscap", "course maintenance", "mechanic", "superintendent"]): return "Greenkeeping"
    if any(k in search_text for k in ["food", "beverage", "f&b", "culinary", "chef", "cook", "bartender", "server", "restaurant", "hospitality", "kitchen", "waiter", "banquet"]): return "Hospitality"
    if any(k in search_text for k in ["pga", "teaching", "instructor", "assistant pro", "head pro", "golf professional", "coach"]): return "PGA Professional"
    if any(k in search_text for k in ["general manager", "director of golf", "club manager", "operations manager"]): return "Club Management"
    if any(k in search_text for k in ["retail", "merchandis", "buyer", "apparel"]): return "Retail"
    if any(k in search_text for k in ["pro shop", "golf shop", "outside service", "bag drop", "cart attendant", "starter", "ranger"]): return "Pro Shop"
    if any(k in search_text for k in ["sales", "account executive", "business development"]): return "Sales"
    if any(k in search_text for k in ["marketing", "social media", "content", "pr", "communications", "brand"]): return "Marketing"
    if any(k in search_text for k in ["finance", "account", "controller", "audit", "tax"]): return "Finance"
    if any(k in search_text for k in ["hr ", "human resources", "talent", "recruit"]): return "Human Resources"
    if any(k in search_text for k in ["tech", "software", "developer", "it ", "data", "engineer", "product manager"]): return "Technology"
    if any(k in search_text for k in ["caddie", "caddy"]): return "Caddie"
    if any(k in search_text for k in ["event", "tournament", "wedding"]): return "Events"
    if any(k in search_text for k in ["customer service", "guest service", "reception", "front desk"]): return "Customer Service"
    if any(k in search_text for k in ["manufactur", "production", "assembl", "factory"]): return "Manufacturing"
    if any(k in search_text for k in ["logistic", "warehouse", "supply chain", "shipping", "distribution"]): return "Logistics"
    if any(k in search_text for k in ["leisure", "fitness", "recreation"]): return "Leisure"
    if any(k in search_text for k in ["legal", "counsel"]): return "Legal"
    if any(k in search_text for k in ["travel"]): return "Travel"
    if any(k in search_text for k in ["construction"]): return "Construction"
    if any(k in search_text for k in ["driving range", "topgolf"]): return "Driving Range"
    if any(k in search_text for k in ["business", "strategy", "analyst"]): return "Business"
    if any(k in search_text for k in ["engineer"]): return "Engineering"
    return "" 

def map_job_type(raw_type, title):
    search_text = f"{str(raw_type)} {str(title)}".lower()
    if "part" in search_text or "pt " in search_text: return "Part-time"
    if "full" in search_text or "ft " in search_text: return "Full-time"
    if "contract" in search_text or "freelance" in search_text: return "Contract"
    if "intern" in search_text: return "Internship"
    if "temp" in search_text or "seasonal" in search_text or "summer" in search_text: return "Temp"
    return "" 

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS clubs
                 (url TEXT PRIMARY KEY, name TEXT, country TEXT, career_url TEXT, last_hash TEXT)''')
    
    c.execute('DROP TABLE IF EXISTS jobs')
    c.execute('''CREATE TABLE jobs
                 (job_reference TEXT PRIMARY KEY, title TEXT, company TEXT, apply_url TEXT, 
                  description TEXT, location TEXT, category TEXT, job_type TEXT, remote BOOLEAN, pubDate TEXT,
                  salary_min TEXT, salary_max TEXT, currency TEXT, interval TEXT)''')
    conn.commit()
    return conn

def load_csv_to_db(conn):
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM clubs")
    if c.fetchone()[0] > 0:
        return 

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
        
        links = re.finditer(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
        keywords = ['career', 'job', 'vacanc', 'employment', 'work-with']
        
        for m in links:
            link = m.group(1)
            if any(k in link.lower() for k in keywords):
                return urllib.parse.urljoin(club_url, link)
                
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
        
        raw_text = re.sub(r'<[^>]+>', ' ', html)
        raw_text = re.sub(r'\s+', ' ', raw_text).strip()
        
        content_hash = hashlib.sha256((raw_text + "v2").encode('utf-8')).hexdigest()
        return raw_text, content_hash
    except urllib.error.HTTPError as e:
        if e.code in [404, 403, 410]: 
            return None, "DEAD_LINK"
        return None, None
    except urllib.error.URLError:
        return None, "DEAD_LINK" 
    except Exception:
        return None, None

# --- PHASE 3: AI PARSING ---
def ai_extract_jobs(raw_text, company_name, apply_url):
    if not GEMINI_API_KEY:
        print("No Gemini API key found. Skipping AI extraction.")
        return []
        
    time.sleep(4) 
    
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""
    You are a data extraction bot. Read the following text from the career page of {company_name}.
    If there are actual job vacancies listed, extract them and return them as a strictly formatted JSON array of objects. 
    If there are no jobs, return an empty array [].
    
    JSON Object format:
    {{
      "title": "Job Title",
      "category": "Department (e.g., Agronomy, F&B, Golf Operations)",
      "job_type": "Full-time, Part-time, Seasonal, Temp, or Contract (if mentioned, otherwise leave blank)",
      "remote": true or false,
      "description": "A short 2-3 sentence summary of the role.",
      "salary_min": "Number only, e.g., 30000 (if mentioned, otherwise leave blank)",
      "salary_max": "Number only, e.g., 35000 (if mentioned, otherwise leave blank)",
      "currency": "GBP, USD, EUR (if mentioned, otherwise leave blank)",
      "interval": "YEARLY, HOURLY, MONTHLY (if mentioned, otherwise leave blank)"
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
        
        text_resp = text_resp.replace('```json', '').replace('```', '').strip()
        jobs_data = json.loads(text_resp)
        return jobs_data
    except Exception as e:
        print(f"AI Parse Error for {company_name}: {e}")
        return []

# --- PHASE 4: THE PRODUCTION PIPELINE ---
def process_club(club_data):
    base_url, name, country, stored_career_url, last_hash = club_data
    
    if last_hash in ["DEAD_LINK", "NO_CAREERS_PAGE"]:
        return None 
    
    career_url = stored_career_url
    if not career_url:
        career_url = discover_career_page(base_url)
        if not career_url: 
            return [], base_url, "", "NO_CAREERS_PAGE"
        
    raw_text, current_hash = fetch_and_hash(career_url)
    
    if current_hash == "DEAD_LINK":
        return [], base_url, career_url, "DEAD_LINK"
        
    if not raw_text or current_hash == last_hash:
        return None, base_url, career_url, current_hash 
        
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
            "job_type": j.get("job_type", ""),
            "remote": bool(j.get("remote", False)),
            "pubDate": now,
            "salary_min": j.get("salary_min", ""),
            "salary_max": j.get("salary_max", ""),
            "currency": j.get("currency", ""),
            "interval": j.get("interval", "")
        })
        
    return extracted_jobs, base_url, career_url, current_hash

def build_rss(conn):
    c = conn.cursor()
    c.execute("SELECT title, company, location, category, job_type, remote, description, apply_url, job_reference, pubDate, salary_min, salary_max, currency, interval FROM jobs")
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0">', '  <channel>',
           '    <title>Global Independent Golf Clubs Feed</title>', '    <link>https://www.golf-jobs.com</link>',
           '    <description>Jobs extracted via AI from 28,000+ independent club websites</description>', f'    <lastBuildDate>{now}</lastBuildDate>']
           
    jobs = c.fetchall()
    
    if not jobs:
        out.extend([
            "    <item>", 
            "      <title>System Mapping Placeholder</title>", 
            "      <company>Golf Jobs Internal</company>",
            "      <location>Manchester, UK</location>", 
            "      <category>Technology</category>",
            "      <job_type>Full-time</job_type>", 
            "      <remote>true</remote>",
            "      <description><![CDATA[This is a hidden placeholder job used to map XML fields in JBoard. You can delete this in JBoard.]]></description>", 
            "      <link>https://www.golf-jobs.com</link>",
            "      <apply_url>https://www.golf-jobs.com</apply_url>", 
            "      <job_reference>MAPPING-001</job_reference>",
            "      <guid isPermaLink=\"false\">MAPPING-001</guid>", 
            f"      <pubDate>{now}</pubDate>",
            "      <salary_min>30000</salary_min>",
            "      <salary_max>40000</salary_max>",
            "      <salary_currency>GBP</salary_currency>",
            "      <salary_interval>YEARLY</salary_interval>", 
            "    </item>"
        ])
    else:
        for row in jobs:
            # Enforce the strict taxonomy mapping on the AI output
            mapped_category = map_category(row[3], row[0], row[6])
            mapped_job_type = map_job_type(row[4], row[0])
            
            out.extend([
                "    <item>", f"      <title>{escape(row[0])}</title>", f"      <company>{escape(row[1])}</company>",
                f"      <location>{escape(row[2])}</location>", f"      <category>{escape(mapped_category)}</category>",
                f"      <job_type>{escape(mapped_job_type)}</job_type>", f"      <remote>{'true' if row[5] else 'false'}</remote>",
                f"      <description><![CDATA[{row[6]}]]></description>", f"      <link>{escape(row[7])}</link>",
                f"      <apply_url>{escape(row[7])}</apply_url>", f"      <job_reference>{escape(row[8])}</job_reference>",
                f"      <guid isPermaLink=\"false\">{escape(row[8])}</guid>", f"      <pubDate>{row[9]}</pubDate>",
                f"      <salary_min>{escape(str(row[10]))}</salary_min>",
                f"      <salary_max>{escape(str(row[11]))}</salary_max>",
                f"      <salary_currency>{escape(str(row[12]))}</salary_currency>",
                f"      <salary_interval>{escape(str(row[13]))}</salary_interval>", "    </item>"
            ])
    out.extend(["  </channel>", "</rss>"])
    
    os.makedirs(os.path.dirname(OUTPUT_XML), exist_ok=True)
    with open(OUTPUT_XML, "w", encoding="utf-8") as f:
        f.write("\n".join(out))

def main():
    conn = init_db()
    load_csv_to_db(conn)
    c = conn.cursor()
    
    c.execute("SELECT url, name, country, career_url, last_hash FROM clubs ORDER BY last_hash ASC")
    clubs = c.fetchall()
    
    print(f"Beginning scan. Graceful timeout set to {MAX_RUNTIME_SECONDS} seconds...")
    
    updates_to_make = []
    jobs_to_insert = []
    start_time = time.time()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_club, club) for club in clubs]
        
        for future in concurrent.futures.as_completed(futures):
            if time.time() - start_time > MAX_RUNTIME_SECONDS:
                print("Approaching GitHub's 6-hour limit! Stopping gracefully to save data...")
                executor.shutdown(wait=False, cancel_futures=True)
                break
                
            result = future.result()
            if not result: continue
            
            extracted_jobs, base_url, career_url, current_hash = result
            
            updates_to_make.append((career_url, current_hash, base_url))
                
            if extracted_jobs:
                print(f"Found {len(extracted_jobs)} jobs at {base_url}")
                for j in extracted_jobs:
                    jobs_to_insert.append((j["job_reference"], j["title"], j["company"], j["apply_url"], 
                                           j["description"], j["location"], j["category"], j["job_type"],
                                           j["remote"], j["pubDate"], j["salary_min"], j["salary_max"], 
                                           j["currency"], j["interval"]))

    c.executemany("UPDATE clubs SET career_url = ?, last_hash = ? WHERE url = ?", updates_to_make)
    c.executemany("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", jobs_to_insert)
    conn.commit()
    
    build_rss(conn)
    print(f"Scan complete. Wrote jobs to {OUTPUT_XML}")

if __name__ == "__main__":
    main()
