#!/usr/bin/env python3
"""
Golf Jobs — master XML feed generator for JBoard.
Enforces strict taxonomy mapping and anti-keyword filtering.
"""

import argparse
import gzip
import html
import http.cookiejar
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib import request, error
from xml.sax.saxutils import escape

USE_NATIVE_DATE = False       
REQUEST_TIMEOUT = 25          
RETRIES = 3                   
RETRY_BACKOFF = 3             
USER_AGENT = "golf-jobs-feed/1.0 (+https://www.golf-jobs.com)"

# --- STRICT TAXONOMY MAPPERS ---
def map_category(raw_cat, title, desc):
    """Maps raw ATS data ONLY to approved JBoard Categories, or returns blank."""
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
    
    return "" # If no match is found, leave it blank to protect taxonomy

def map_job_type(raw_type, title):
    """Maps raw ATS data ONLY to approved JBoard Job Types, or returns blank."""
    search_text = f"{str(raw_type)} {str(title)}".lower()
    
    if "part" in search_text or "pt " in search_text: return "Part-time"
    if "full" in search_text or "ft " in search_text: return "Full-time"
    if "contract" in search_text or "freelance" in search_text: return "Contract"
    if "intern" in search_text: return "Internship"
    if "temp" in search_text or "seasonal" in search_text or "summer" in search_text: return "Temp"
    
    return "" # If no match is found, leave it blank

# --- HTTP HELPERS ---
def _http(url, method="GET", data=None, headers=None):
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers: hdrs.update(headers)
    body = json.dumps(data).encode("utf-8") if data is not None else None
    if data is not None: hdrs["Content-Type"] = "application/json"
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = request.Request(url, data=body, headers=hdrs, method=method)
            with request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < RETRIES: time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"request failed after {RETRIES} attempts: {url} :: {last_err}")

def _http_text(url, headers=None):
    hdrs = {"User-Agent": USER_AGENT}
    if headers: hdrs.update(headers)
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            with request.urlopen(request.Request(url, headers=hdrs), timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read()
                return gzip.decompress(raw).decode("utf-8", "replace") if resp.headers.get("Content-Encoding") == "gzip" else raw.decode("utf-8", "replace")
        except (error.HTTPError, error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < RETRIES: time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"text request failed after {RETRIES} attempts: {url} :: {last_err}")

def _clean_html(text): return str(text) if text else ""

def _to_rfc822(value):
    if not value: return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 1e12 else value
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    except Exception: return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

def make_job(source_slug, native_id, title, company, apply_url, description="", location="", category="", job_type="", remote=False, posted=None):
    return {
        "job_reference": f"{source_slug}-{native_id}",
        "title": title or "", "company": company or source_slug, "apply_url": apply_url or "",
        "description": _clean_html(description), "location": location or "",
        "category": category or "", "job_type": job_type or "",
        "remote": bool(remote), "pubDate": _to_rfc822(posted)
    }

# --- ADAPTERS (Truncated for space, they operate exactly as before but data gets mapped later) ---
def fetch_greenhouse(src):
    data = _http(f"https://boards-api.greenhouse.io/v1/boards/{src['token']}/jobs?content=true")
    return [make_job(src["slug"], j["id"], j.get("title"), src["company"], j.get("absolute_url"), html.unescape(j.get("content", "")), (j.get("location") or {}).get("name", ""), j["departments"][0].get("name", "") if j.get("departments") else "", posted=j.get("updated_at")) for j in data.get("jobs", [])]

def fetch_lever(src):
    data = _http(f"https://api.lever.co/v0/postings/{src['token']}?mode=json")
    return [make_job(src["slug"], j["id"], j.get("text"), src["company"], j.get("applyUrl") or j.get("hostedUrl"), j.get("descriptionPlain", ""), (j.get("categories") or {}).get("location", ""), (j.get("categories") or {}).get("department", ""), (j.get("categories") or {}).get("commitment", ""), posted=j.get("createdAt")) for j in data]

def fetch_recruitee(src):
    data = _http(f"https://{src['token']}.recruitee.com/api/offers/")
    return [make_job(src["slug"], j["id"], j.get("title"), src["company"], j.get("careers_url"), j.get("description", ""), j.get("location", ""), j.get("department", ""), j.get("employment_type_code", ""), bool(j.get("remote")), j.get("published_at")) for j in data.get("offers", [])]

def fetch_smartrecruiters(src):
    base = f"https://api.smartrecruiters.com/v1/companies/{src['token']}/postings"
    jobs, offset, limit = [], 0, 100
    while True:
        content = _http(f"{base}?limit={limit}&offset={offset}").get("content", [])
        for p in content:
            loc = p.get("location", {}) or {}
            jobs.append(make_job(src["slug"], p["id"], p.get("name"), src["company"], f"https://jobs.smartrecruiters.com/{src['token']}/{p['id']}", f"<p>View full job details and apply on the {src['company']} careers site.</p>", ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")])), (p.get("function") or {}).get("label", ""), (p.get("typeOfEmployment") or {}).get("label", ""), posted=p.get("releasedDate")))
        if len(content) < limit: break
        offset += limit
    return jobs

def fetch_ashby(src):
    data = _http(f"https://api.ashbyhq.com/posting-api/job-board/{src['token']}?includeCompensation=true")
    return [make_job(src["slug"], j["id"], j.get("title"), src["company"], j.get("applyUrl") or j.get("jobUrl"), j.get("descriptionHtml", ""), j.get("location", ""), j.get("department", ""), j.get("employmentType", ""), bool(j.get("isRemote")), j.get("publishedAt")) for j in data.get("jobs", [])]

def fetch_workable(src):
    data = _http(f"https://apply.workable.com/api/v1/widget/accounts/{src['token']}?details=true")
    return [make_job(src["slug"], j.get("shortcode") or j.get("id"), j.get("title"), src["company"], j.get("url"), j.get("description", ""), ", ".join(filter(None, [(j.get("location") or {}).get("city"), (j.get("location") or {}).get("country")])), j.get("department", ""), j.get("employment_type", ""), posted=j.get("published_on")) for j in data.get("jobs", [])]

def fetch_workday(src):
    host, tenant, site = src["host"], src["tenant"], src["site"]
    base, jobs, offset = f"https://{host}/wday/cxs/{tenant}/{site}", [], 0
    while True:
        data = _http(f"{base}/jobs", method="POST", data={"limit": 20, "offset": offset, "searchText": "", "appliedFacets": {}})
        postings = data.get("jobPostings", [])
        for p in postings:
            ext = p.get("externalPath", "")
            apply_url = f"https://{host}{ext}" if ext else ""
            native = ext.rsplit("/", 1)[-1] if ext else p.get("bulletFields", [""])[0]
            desc_html = f"<p>View full details and apply on the {src['company']} career site.</p>"
            if ext:
                try:
                    fetched_desc = _http(f"{base}{ext}", method="GET").get("jobPostingInfo", {}).get("jobDescription", "")
                    if fetched_desc: desc_html = fetched_desc
                except Exception: pass
            jobs.append(make_job(src["slug"], native, p.get("title"), src["company"], apply_url, desc_html, p.get("locationsText", ""), posted=p.get("postedOn", "")))
        if offset >= data.get("total", 0) or not postings: break
        offset += 20
    return jobs

def fetch_teamtailor(src):
    host = src.get("host") or f"{src['token']}.teamtailor.com"
    text = _http_text(f"https://{host}/jobs.rss?per_page=200", headers={"Accept": "application/rss+xml, application/xml, text/xml"})
    root = ET.fromstring(text)
    channel = root.find("channel")
    jobs = []
    for item in (channel.findall("item") if channel is not None else []):
        link = item.findtext("link", "") or ""
        native = (re.search(r"/jobs/(\d+)", link) or type('obj', (object,), {'group': lambda self, x: item.findtext("guid") or link})()).group(1)
        jobs.append(make_job(src["slug"], native, item.findtext("title"), src["company"], link, item.findtext("description", ""), "", "", "", posted=item.findtext("pubDate")))
    return jobs

def fetch_dayforce(src):
    ns, board, culture = src["token"], src.get("job_board_code", "CANDIDATEPORTAL"), src.get("culture", "en-US")
    base_hdr = {"User-Agent": USER_AGENT}
    cj = http.cookiejar.CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(cj))
    opener.open(request.Request(f"https://jobs.dayforcehcm.com/{culture}/{ns}/{board}", headers=base_hdr), timeout=REQUEST_TIMEOUT).read()
    url, jobs, offset = f"https://jobs.dayforcehcm.com/api/geo/{ns}/jobposting/search", [], 0
    while True:
        req = request.Request(url, data=json.dumps({"clientNamespace": ns, "jobBoardCode": board, "cultureCode": culture, "distanceUnit": 0, "paginationStart": offset}).encode("utf-8"), headers={**base_hdr, "Content-Type": "application/json"}, method="POST")
        data = json.loads(opener.open(req, timeout=REQUEST_TIMEOUT).read().decode("utf-8"))
        postings = data.get("jobPostings", []) or []
        for p in postings:
            jobs.append(make_job(src["slug"], p.get("jobPostingId"), p.get("jobTitle"), src["company"], f"https://jobs.dayforcehcm.com/{culture}/{ns}/{board}/jobs/{p.get('jobPostingId')}", p.get("jobDescription", ""), posted=p.get("postingStartTimestampUTC")))
        if offset >= data.get("maxCount", 0) or not postings: break
        offset += 25
    return jobs

def fetch_cornerstone(src):
    try:
        root = ET.fromstring(_http_text(f"https://{src['host']}/ats/careersite/rss.aspx?site={src.get('token', '1')}"))
        channel = root.find("channel")
    except Exception: return []
    return [make_job(src["slug"], item.findtext("link", "").split("id=")[-1] if "id=" in item.findtext("link", "") else item.findtext("guid"), item.findtext("title"), src["company"], item.findtext("link", ""), html.unescape(item.findtext("description", "")), posted=item.findtext("pubDate")) for item in (channel.findall("item") if channel is not None else [])]

def fetch_networx(src):
    text, jobs, seen = _http_text(f"https://{src['host']}/Jobs/Search"), [], set()
    for m in re.finditer(r'<a[^>]+href="(/Jobs/Advert/(\d+)[^"]*)"[^>]*>(.*?)</a>', text, re.IGNORECASE):
        if m.group(2) in seen: continue
        seen.add(m.group(2))
        title = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        if title and "apply" not in title.lower(): jobs.append(make_job(src["slug"], m.group(2), title, src["company"], f"https://{src['host']}{m.group(1)}", f"<p>View full job details and apply on the {src['company']} careers site.</p>"))
    return jobs

def fetch_harri(src):
    text, jobs, seen = _http_text(f"https://harri.com/{src['token']}"), [], set()
    for m in re.finditer(rf'href="(/({src["token"]}|[A-Za-z0-9\-]+)/job/(\d+)-([^"]+))"', text, re.IGNORECASE):
        if m.group(3) in seen: continue
        seen.add(m.group(3))
        jobs.append(make_job(src["slug"], m.group(3), m.group(4).replace("-", " ").title(), src["company"], f"https://harri.com{m.group(1)}", f"<p>View full job details and apply on the {src['company']} careers site.</p>"))
    return jobs

def fetch_quintadolago(src):
    text = _http_text("https://www.quintadolago.com/en/careers/")
    return [make_job(src["slug"], m.group(1).strip().replace(" ", "-").lower(), m.group(1).strip(), src["company"], "https://www.quintadolago.com/en/careers/", "<p>View full job details on the Quinta do Lago careers site.</p>", category=m.group(2).strip()) for m in re.finditer(r'\*\s*([^\*]+?)\s*Department:\s*([^\.]+)\.', text, re.IGNORECASE)]

def fetch_rezoomo(src):
    text = _http_text(f"https://www.rezoomo.com/company/{src['token']}/jobs/")
    return [make_job(src["slug"], m.group(1).split("/")[-1] or m.group(1).split("/")[-2], m.group(2).strip(), src["company"], "https://www.rezoomo.com" + m.group(1), f"<p>View full job details on the {src['company']} careers site.</p>") for m in re.finditer(r'<a[^>]+href="(/job/[^"]+)"[^>]*>.*?<h[23][^>]*>(.*?)</h[23]>', text, re.IGNORECASE | re.DOTALL)]

def fetch_adidas(src):
    try: data = _http("https://careers.adidas-group.com/api/jobs/search?brand=adidas&locale=en&limit=1000")
    except Exception: return []
    return [make_job(src["slug"], str(j.get("id", "")), j.get("title"), src["company"], f"https://careers.adidas-group.com/jobs/{j.get('id')}", j.get("description", ""), j.get("location", ""), j.get("team", "")) for j in data.get("jobs", [])]

def fetch_ultipro(src):
    host, tenant, board = src["host"], src["tenant"], src["token"]
    url, jobs, skip = f"https://{host}/{tenant}/JobBoard/{board}/JobBoardView/LoadSearchResults", [], 0
    while True:
        try: data = _http(url, method="POST", data={"opportunitySearch": {"Top": 50, "Skip": skip, "QueryString": "", "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}]}, "matchCriteria": {"LanguageId": 1}})
        except Exception: return jobs
        opportunities = data.get("opportunities", [])
        for p in opportunities:
            jobs.append(make_job(src["slug"], p.get("Id"), p.get("Title"), src["company"], f"https://{host}/{tenant}/JobBoard/{board}/OpportunityDetail?opportunityId={p.get('Id')}", f"<p>View full job details and apply on the {src['company']} careers site.</p>", posted=p.get("PostedDate")))
        if len(opportunities) < 50: break
        skip += 50
    return jobs

def fetch_rss(src):
    try:
        root = ET.fromstring(_http_text(src["url"]))
        channel = root.find("channel") if root.find("channel") is not None else root
        return [make_job(src["slug"], item.findtext("link", "").split("/")[-1] or str(hash(item.findtext("title", ""))), item.findtext("title", ""), src["company"], item.findtext("link", ""), html.unescape(item.findtext("description", "")), posted=item.findtext("pubDate")) for item in channel.findall("item")]
    except Exception: return []

def fetch_schema_scraper(src):
    try: text = _http_text(src["url"])
    except Exception: return []
    domain = "/".join(src["url"].split("/")[:3])
    links = {domain + m.group(1) if not m.group(1).startswith("http") else m.group(1) for m in re.finditer(r'href=["\'](/[^"\']+|https?://[^"\']+)["\']', text, re.IGNORECASE) if not any(ext in m.group(1).lower() for ext in ['.css', '.js', '.png', '.jpg', '.jpeg', '#'])}
    jobs, seen = [], set()
    for link in list(links)[:40]:
        try:
            for ld_match in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', _http_text(link), re.IGNORECASE | re.DOTALL):
                try:
                    data = json.loads(ld_match.group(1).strip().replace("<![CDATA[", "").replace("]]>", ""))
                    for item in (data if isinstance(data, list) else [data]):
                        for node in (item["@graph"] if "@graph" in item else [item]):
                            if node.get("@type") == "JobPosting" and node.get("title", "") not in seen:
                                seen.add(node.get("title", ""))
                                jobs.append(make_job(src["slug"], link.split("/")[-1] or str(hash(node.get("title", ""))), node.get("title", ""), src["company"], link, node.get("description", "")))
                except Exception: continue
        except Exception: continue
    return jobs

ADAPTERS = {
    "greenhouse": fetch_greenhouse, "lever": fetch_lever, "recruitee": fetch_recruitee,
    "smartrecruiters": fetch_smartrecruiters, "ashby": fetch_ashby, "workable": fetch_workable,
    "workday": fetch_workday, "teamtailor": fetch_teamtailor, "dayforce": fetch_dayforce,
    "cornerstone": fetch_cornerstone, "networx": fetch_networx, "harri": fetch_harri,
    "quintadolago": fetch_quintadolago, "rezoomo": fetch_rezoomo, "adidas": fetch_adidas,
    "rss": fetch_rss, "schema_scraper": fetch_schema_scraper, "ultipro": fetch_ultipro
}

def build_rss(jobs):
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0">', '  <channel>',
           '    <title>Golf Jobs — Aggregated Employer Feed</title>', '    <link>https://www.golf-jobs.com</link>',
           '    <description>Live vacancies aggregated from golf employer ATS platforms</description>', f'    <lastBuildDate>{now}</lastBuildDate>']
    for j in jobs:
        # STRICT TAXONOMY ENFORCEMENT HAPPENS HERE BEFORE XML CREATION
        mapped_category = map_category(j.get("category", ""), j.get("title", ""), j.get("description", ""))
        mapped_job_type = map_job_type(j.get("job_type", ""), j.get("title", ""))
        
        out.extend([
            "    <item>", f"      <title>{escape(j['title'])}</title>", f"      <company>{escape(j['company'])}</company>",
            f"      <location>{escape(j['location'])}</location>", f"      <category>{escape(mapped_category)}</category>",
            f"      <job_type>{escape(mapped_job_type)}</job_type>", f"      <remote>{'true' if j['remote'] else 'false'}</remote>",
            f"      <description><![CDATA[{j['description']}]]></description>", f"      <link>{escape(j['apply_url'])}</link>",
            f"      <apply_url>{escape(j['apply_url'])}</apply_url>", f"      <job_reference>{escape(j['job_reference'])}</job_reference>",
            f"      <guid isPermaLink=\"false\">{escape(j['job_reference'])}</guid>", f"      <pubDate>{j['pubDate']}</pubDate>", "    </item>"
        ])
    out.extend(["  </channel>", "</rss>"])
    return "\n".join(out)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="sources.json")
    ap.add_argument("--out", default="feed.xml")
    args = ap.parse_args()

    with open(args.config) as f: sources = json.load(f)["sources"]
    all_jobs, report = [], []
    for src in sources:
        if not isinstance(src, dict) or not src.get("enabled", True): continue
        if not ADAPTERS.get(src["ats"]): continue
        try:
            jobs = ADAPTERS[src["ats"]](src)
            if src.get("golf_only"):
                filtered = []
                # THE ANTI-KEYWORD RESORT FILTER
                anti_keywords = ["tennis", "spa ", "spa,", "massage", "yoga", "ski ", "snow", "esthetician", "hair stylist", "childcare", "nanny", "pool", "swim", "lifeguard"]
                keywords = ["golf", "pga", "greenkeeper", "agronomy", "turf", "caddie", "clubhouse", "titleist", "taylormade", "trackman", "putting", "whistling straits", "old course", "blackwolf run"]
                
                for j in jobs:
                    title_lower = j.get('title','').lower()
                    if any(anti in title_lower for anti in anti_keywords):
                        continue # Kill the job if it's a tennis/spa/pool role
                        
                    search_text = f"{title_lower} {j.get('category','').lower()} {j.get('description','').lower()} {j.get('company','').lower()}"
                    if any(k in search_text for k in keywords): filtered.append(j)
                report.append(f"FILTER {src['company']}: kept {len(filtered)} of {len(jobs)} jobs")
                jobs = filtered
            else:
                report.append(f"OK    {src['company']}: {len(jobs)} jobs added")
            all_jobs.extend(jobs)
        except Exception as e: report.append(f"FAIL  {src['company']}: {e}")

    seen, deduped = set(), []
    for j in all_jobs:
        if j["job_reference"] not in seen:
            seen.add(j["job_reference"])
            deduped.append(j)

    with open(args.out, "w", encoding="utf-8") as f: f.write(build_rss(deduped))
    print("\n".join(report) + f"\nTOTAL: {len(deduped)} jobs", file=sys.stderr)

if __name__ == "__main__": main()
