#!/usr/bin/env python3
"""
Golf Jobs — master XML feed generator for JBoard.

Pulls live vacancies from each golf employer's applicant tracking system (ATS)
and writes ONE combined RSS feed that JBoard's XML importer can ingest.
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

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
USE_NATIVE_DATE = False       
REQUEST_TIMEOUT = 25          
RETRIES = 3                   
RETRY_BACKOFF = 3             
USER_AGENT = "golf-jobs-feed/1.0 (+https://www.golf-jobs.com)"

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _http(url, method="GET", data=None, headers=None):
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = request.Request(url, data=body, headers=hdrs, method=method)
            with request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"request failed after {RETRIES} attempts: {url} :: {last_err}")

def _http_text(url, headers=None):
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            with request.urlopen(request.Request(url, headers=hdrs),
                                 timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except (error.HTTPError, error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"text request failed after {RETRIES} attempts: {url} :: {last_err}")

def _clean_html(text):
    if text is None: return ""
    return str(text)

def _now_rfc822():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

def _to_rfc822(value):
    if not value: return _now_rfc822()
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 1e12 else value
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    except Exception:
        return _now_rfc822()

# ---------------------------------------------------------------------------
# Normalised job record
# ---------------------------------------------------------------------------
def make_job(source_slug, native_id, title, company, apply_url,
             description="", location="", category="", job_type="",
             remote=False, posted=None):
    return {
        "job_reference": f"{source_slug}-{native_id}",
        "title": title or "",
        "company": company or source_slug,
        "apply_url": apply_url or "",
        "description": _clean_html(description),
        "location": location or "",
        "category": category or "",
        "job_type": job_type or "",
        "remote": bool(remote),
        "pubDate": (_to_rfc822(posted) if USE_NATIVE_DATE else _now_rfc822()),
    }

# ---------------------------------------------------------------------------
# ATS adapters
# ---------------------------------------------------------------------------
def fetch_greenhouse(src):
    token = src["token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    data = _http(url)
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        dept = ""
        if j.get("departments"): dept = j["departments"][0].get("name", "")
        jobs.append(make_job(
            src["slug"], j["id"], j.get("title"), src["company"],
            j.get("absolute_url"), description=html.unescape(j.get("content", "")),
            location=loc, category=dept, posted=j.get("updated_at")
        ))
    return jobs

def fetch_lever(src):
    token = src["token"] 
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    data = _http(url)
    jobs = []
    for j in data:
        cats = j.get("categories", {}) or {}
        jobs.append(make_job(
            src["slug"], j["id"], j.get("text"), src["company"],
            j.get("applyUrl") or j.get("hostedUrl"),
            description=j.get("description") or j.get("descriptionPlain", ""),
            location=cats.get("location", ""), category=cats.get("team") or cats.get("department", ""),
            job_type=cats.get("commitment", ""), remote=("remote" in (cats.get("location", "") or "").lower()),
            posted=j.get("createdAt")
        ))
    return jobs

def fetch_recruitee(src):
    company = src["token"]
    url = f"https://{company}.recruitee.com/api/offers/"
    data = _http(url)
    jobs = []
    for j in data.get("offers", []):
        loc = j.get("location") or ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        jobs.append(make_job(
            src["slug"], j["id"], j.get("title"), src["company"],
            j.get("careers_url") or j.get("careers_apply_url"),
            description=j.get("description", ""), location=loc, category=j.get("department", ""),
            job_type=j.get("employment_type_code", ""), remote=bool(j.get("remote")), posted=j.get("published_at") or j.get("created_at")
        ))
    return jobs

def fetch_smartrecruiters(src):
    company = src["token"] 
    base = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    jobs, offset, limit = [], 0, 100
    while True:
        data = _http(f"{base}?limit={limit}&offset={offset}")
        content = data.get("content", [])
        for p in content:
            loc = p.get("location", {}) or {}
            loc_str = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
            apply_url = (p.get("ref", {}) or {}).get("jobAd") or f"https://jobs.smartrecruiters.com/{company}/{p['id']}"
            desc = ""
            try:
                detail = _http(f"{base}/{p['id']}")
                sections = (detail.get("jobAd", {}) or {}).get("sections", {}) or {}
                desc = "".join((sections.get(k, {}) or {}).get("text", "")
                               for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"))
            except Exception: pass
            jobs.append(make_job(
                src["slug"], p["id"], p.get("name"), src["company"], apply_url,
                description=desc, location=loc_str, category=(p.get("function", {}) or {}).get("label", ""),
                job_type=(p.get("typeOfEmployment", {}) or {}).get("label", ""), remote=bool(loc.get("remote")), posted=p.get("releasedDate")
            ))
        if len(content) < limit: break
        offset += limit
    return jobs

def fetch_ashby(src):
    org = src["token"]
    url = "https://api.ashbyhq.com/posting-api/job-board/" + org + "?includeCompensation=true"
    data = _http(url)
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(make_job(
            src["slug"], j["id"], j.get("title"), src["company"],
            j.get("applyUrl") or j.get("jobUrl"),
            description=j.get("descriptionHtml") or j.get("descriptionPlain", ""),
            location=j.get("location", ""), category=j.get("department", ""),
            job_type=j.get("employmentType", ""), remote=bool(j.get("isRemote")), posted=j.get("publishedAt")
        ))
    return jobs

def fetch_workable(src):
    account = src["token"] 
    url = f"https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"
    data = _http(url)
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location", {}) or {}
        loc_str = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
        jobs.append(make_job(
            src["slug"], j.get("shortcode") or j.get("id"), j.get("title"),
            src["company"], j.get("url") or j.get("application_url"),
            description=j.get("description", ""), location=loc_str, category=j.get("department", ""), 
            job_type=j.get("employment_type", ""), remote=bool(loc.get("telecommuting")), posted=j.get("published_on")
        ))
    return jobs

def fetch_workday(src):
    host, tenant, site = src["host"], src["tenant"], src["site"]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    jobs, offset, limit = [], 0, 20
    while True:
        data = _http(f"{base}/jobs", method="POST",
                     data={"limit": limit, "offset": offset, "searchText": "", "appliedFacets": {}})
        postings = data.get("jobPostings", [])
        for p in postings:
            ext = p.get("externalPath", "")
            apply_url = f"https://{host}{ext}" if ext else ""
            native = ext.rsplit("/", 1)[-1] if ext else p.get("bulletFields", [""])[0]
            desc_html = f"<p>View full details and apply on the {src['company']} career site.</p>"
            if ext:
                try:
                    detail_data = _http(f"https://{host}/wday/cxs/{tenant}/{site}{ext}", method="GET")
                    fetched_desc = detail_data.get("jobPostingInfo", {}).get("jobDescription", "")
                    if fetched_desc: desc_html = fetched_desc
                except Exception: pass 
            jobs.append(make_job(
                src["slug"], native, p.get("title"), src["company"], apply_url,
                description=desc_html, location=p.get("locationsText", ""), posted=p.get("postedOn", "")
            ))
        total = data.get("total", 0)
        offset += limit
        if offset >= total or not postings: break
    return jobs

_TT_NS = {"tt": "https://teamtailor.com/locations"}
def fetch_teamtailor(src):
    host = src.get("host") or f"{src['token']}.teamtailor.com"
    per_page = src.get("per_page", 200)
    url = f"https://{host}/jobs.rss?per_page={per_page}"
    text = _http_text(url, headers={"Accept": "application/rss+xml, application/xml, text/xml"})
    if "<rss" not in text: raise RuntimeError("no RSS feed for this Teamtailor board")
    root = ET.fromstring(text)
    channel = root.find("channel")
    jobs = []
    for item in (channel.findall("item") if channel is not None else []):
        link = item.findtext("link", "") or ""
        m = re.search(r"/jobs/(\d+)", link)
        native = m.group(1) if m else (item.findtext("guid") or link)
        desc = html.unescape(item.findtext("description", "") or "")
        if any(e in desc for e in ("&lt;", "&gt;", "&amp;")): desc = html.unescape(desc)
        dept = item.findtext("tt:department", default="", namespaces=_TT_NS)
        locs = []
        for loc in item.findall("tt:locations/tt:location", _TT_NS):
            name = loc.findtext("tt:name", namespaces=_TT_NS)
            if name and name.strip():
                locs.append(name.strip())
                continue
            city = (loc.findtext("tt:city", namespaces=_TT_NS) or "").strip()
            country = (loc.findtext("tt:country", namespaces=_TT_NS) or "").strip()
            combo = ", ".join(p for p in (city, country) if p)
            if combo: locs.append(combo)
        remote_flag = (item.findtext("remoteStatus", "") or "").lower()
        jobs.append(make_job(
            src["slug"], native, item.findtext("title"), src["company"], link,
            description=desc, location="; ".join(locs), category=dept or "",
            remote=remote_flag not in ("", "none", "no", "false"), posted=item.findtext("pubDate")
        ))
    return jobs

def fetch_dayforce(src):
    ns = src["token"]
    board = src.get("job_board_code", "CANDIDATEPORTAL")
    culture = src.get("culture", "en-US")
    base_hdr = {"User-Agent": USER_AGENT}
    cj = http.cookiejar.CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(cj))
    opener.open(request.Request(f"https://jobs.dayforcehcm.com/{culture}/{ns}/{board}", headers=base_hdr), timeout=REQUEST_TIMEOUT).read()
    csrf = ""
    try:
        r = opener.open(request.Request("https://jobs.dayforcehcm.com/api/auth/csrf", headers={**base_hdr, "Accept": "application/json"}), timeout=REQUEST_TIMEOUT)
        csrf = json.loads(r.read().decode("utf-8")).get("csrfToken", "")
    except Exception: pass
    url = f"https://jobs.dayforcehcm.com/api/geo/{ns}/jobposting/search"
    jobs, offset = [], 0
    while True:
        payload = {"clientNamespace": ns, "jobBoardCode": board, "cultureCode": culture, "distanceUnit": 0, "paginationStart": offset}
        hdrs = {**base_hdr, "Content-Type": "application/json", "Accept": "application/json"}
        if csrf: hdrs["x-csrf-token"] = csrf
        req = request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=hdrs, method="POST")
        data = json.loads(opener.open(req, timeout=REQUEST_TIMEOUT).read().decode("utf-8"))
        postings = data.get("jobPostings", []) or []
        for p in postings:
            locs = p.get("postingLocations", []) or []
            pl = locs[0] if locs else {}
            loc_str = pl.get("formattedAddress") or ", ".join(x for x in [pl.get("cityName"), pl.get("stateCode"), pl.get("isoCountryCode")] if x)
            jid = p.get("jobPostingId")
            apply_url = f"https://jobs.dayforcehcm.com/{culture}/{ns}/{board}/jobs/{jid}"
            jobs.append(make_job(
                src["slug"], jid, p.get("jobTitle"), src["company"], apply_url,
                description=p.get("jobDescription", ""), location=loc_str, posted=p.get("postingStartTimestampUTC")
            ))
        max_count = data.get("maxCount", 0)
        offset += 25
        if offset >= max_count or not postings: break
    return jobs

def fetch_cornerstone(src):
    host = src["host"]
    site_id = src.get("token", "1")
    try:
        text = _http_text(f"https://{host}/ats/careersite/rss.aspx?site={site_id}")
        root = ET.fromstring(text)
        channel = root.find("channel")
    except Exception: return []
    jobs = []
    for item in (channel.findall("item") if channel is not None else []):
        link = item.findtext("link", "")
        native = link.split("id=")[-1] if "id=" in link else item.findtext("guid")
        jobs.append(make_job(
            src["slug"], native, item.findtext("title"), src["company"], link,
            description=html.unescape(item.findtext("description", "")), posted=item.findtext("pubDate")
        ))
    return jobs

def fetch_networx(src):
    host = src["host"] 
    text = _http_text(f"https://{host}/Jobs/Search")
    jobs = []
    matches = re.finditer(r'<a[^>]+href="(/Jobs/Advert/(\d+)[^"]*)"[^>]*>(.*?)</a>', text, re.IGNORECASE)
    seen = set()
    for m in matches:
        native_id = m.group(2)
        if native_id in seen: continue
        seen.add(native_id)
        apply_url = f"https://{host}{m.group(1)}"
        title = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        if not title or "apply" in title.lower(): continue
        jobs.append(make_job(
            src["slug"], native_id, title, src["company"], apply_url,
            description=f"<p>View full job details and apply on the {src['company']} careers site.</p>"
        ))
    return jobs

def fetch_harri(src):
    brand = src["token"]
    text = _http_text(f"https://harri.com/{brand}")
    jobs = []
    matches = re.finditer(rf'href="(/({brand}|[A-Za-z0-9\-]+)/job/(\d+)-([^"]+))"', text, re.IGNORECASE)
    seen = set()
    for m in matches:
        job_url_path, native_id = m.group(1), m.group(3)
        if native_id in seen: continue
        seen.add(native_id)
        title = m.group(4).replace("-", " ").title()
        jobs.append(make_job(
            src["slug"], native_id, title, src["company"], f"https://harri.com{job_url_path}",
            description=f"<p>View full job details and apply on the {src['company']} careers site.</p>"
        ))
    return jobs

def fetch_quintadolago(src):
    url = "https://www.quintadolago.com/en/careers/"
    text = _http_text(url)
    jobs = []
    for m in re.finditer(r'\*\s*([^\*]+?)\s*Department:\s*([^\.]+)\.', text, re.IGNORECASE):
        title, dept = m.group(1).strip(), m.group(2).strip()
        jobs.append(make_job(
            src["slug"], title.replace(" ", "-").lower(), title, src["company"], url,
            description="<p>View full job details and apply on the Quinta do Lago careers site.</p>", category=dept
        ))
    return jobs

def fetch_rezoomo(src):
    token = src["token"] 
    text = _http_text(f"https://www.rezoomo.com/company/{token}/jobs/")
    jobs = []
    for m in re.finditer(r'<a[^>]+href="(/job/[^"]+)"[^>]*>.*?<h[23][^>]*>(.*?)</h[23]>', text, re.IGNORECASE | re.DOTALL):
        apply_url = "https://www.rezoomo.com" + m.group(1)
        title = m.group(2).strip()
        native = m.group(1).split("/")[-1] or m.group(1).split("/")[-2]
        jobs.append(make_job(
            src["slug"], native, title, src["company"], apply_url,
            description=f"<p>View full job details on the {src['company']} careers site.</p>"
        ))
    return jobs

def fetch_adidas(src):
    try:
        data = _http("https://careers.adidas-group.com/api/jobs/search?brand=adidas&locale=en&limit=1000")
    except Exception: return []
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(make_job(
            src["slug"], str(j.get("id", "")), j.get("title"), src["company"], 
            f"https://careers.adidas-group.com/jobs/{j.get('id')}",
            description=j.get("description", ""), location=j.get("location", ""), category=j.get("team", "")
        ))
    return jobs

def fetch_ultipro(src):
    host, tenant, board = src["host"], src["tenant"], src["token"]
    url = f"https://{host}/{tenant}/JobBoard/{board}/JobBoardView/LoadSearchResults"
    jobs, skip, top = [], 0, 50
    while True:
        payload = {
            "opportunitySearch": {"Top": top, "Skip": skip, "QueryString": "", "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}]},
            "matchCriteria": {"LanguageId": 1}
        }
        try:
            data = _http(url, method="POST", data=payload)
        except Exception:
            return jobs
            
        opportunities = data.get("opportunities", [])
        for p in opportunities:
            jid = p.get("Id")
            apply_url = f"https://{host}/{tenant}/JobBoard/{board}/OpportunityDetail?opportunityId={jid}"
            loc_str = ""
            if p.get("Locations"):
                addr = p["Locations"][0].get("Address", {})
                state = addr.get("State", {}).get("Name", "") if isinstance(addr.get("State"), dict) else addr.get("State", "")
                country = addr.get("Country", {}).get("Name", "") if isinstance(addr.get("Country"), dict) else addr.get("Country", "")
                loc_str = ", ".join(filter(None, [addr.get("City", ""), state, country]))
            
            jobs.append(make_job(
                src["slug"], jid, p.get("Title"), src["company"], apply_url,
                description=f"<p>View full job details and apply on the {src['company']} careers site.</p>", 
                location=loc_str, posted=p.get("PostedDate")
            ))
            
        if len(opportunities) < top:
            break
        skip += top
    return jobs

def fetch_rss(src):
    url = src["url"]
    text = _http_text(url)
    root = ET.fromstring(text)
    channel = root.find("channel")
    if channel is None: channel = root
    jobs = []
    for item in channel.findall("item"):
        link = item.findtext("link", "")
        title = item.findtext("title", "")
        desc = html.unescape(item.findtext("description", ""))
        native = link.split("/")[-1] or item.findtext("guid") or str(hash(title))
        jobs.append(make_job(
            src["slug"], native, title, src["company"], link, 
            description=desc, posted=item.findtext("pubDate")
        ))
    return jobs

def fetch_schema_scraper(src):
    list_url = src["url"]
    try: text = _http_text(list_url)
    except Exception: return []
    domain = "/".join(list_url.split("/")[:3])
    links = set()
    for m in re.finditer(r'href=["\'](/[^"\']+|https?://[^"\']+)["\']', text, re.IGNORECASE):
        link = m.group(1)
        if not link.startswith("http"): link = domain + link
        if any(ext in link.lower() for ext in ['.css', '.js', '.png', '.jpg', '.jpeg', '#']): continue
        links.add(link)
        
    jobs, seen = [], set()
    for link in list(links)[:40]:
        try:
            page_text = _http_text(link)
            for ld_match in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page_text, re.IGNORECASE | re.DOTALL):
                try:
                    raw_json = ld_match.group(1).strip().replace("<![CDATA[", "").replace("]]>", "")
                    data = json.loads(raw_json)
                    if isinstance(data, dict): data = [data]
                    for item in data:
                        nodes = item["@graph"] if "@graph" in item else [item]
                        for node in nodes:
                            if node.get("@type") == "JobPosting":
                                title = node.get("title", "")
                                if title in seen: continue
                                seen.add(title)
                                desc = node.get("description", "")
                                loc_obj, loc = node.get("jobLocation", {}), ""
                                if isinstance(loc_obj, dict):
                                    addr = loc_obj.get("address", {})
                                    if isinstance(addr, dict):
                                        loc = ", ".join(filter(None, [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]))
                                jobs.append(make_job(
                                    src["slug"], link.split("/")[-1] or str(hash(title)), title, src["company"], link,
                                    description=desc, location=loc
                                ))
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
    now = _now_rfc822()
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0">', '  <channel>',
           '    <title>Golf Jobs — Aggregated Employer Feed</title>', '    <link>https://www.golf-jobs.com</link>',
           '    <description>Live vacancies aggregated from golf employer ATS platforms</description>', f'    <lastBuildDate>{now}</lastBuildDate>']
    for j in jobs:
        out.extend([
            "    <item>", f"      <title>{escape(j['title'])}</title>", f"      <company>{escape(j['company'])}</company>",
            f"      <location>{escape(j['location'])}</location>", f"      <category>{escape(j['category'])}</category>",
            f"      <job_type>{escape(j['job_type'])}</job_type>", f"      <remote>{'true' if j['remote'] else 'false'}</remote>",
            f"      <description><![CDATA[{j['description']}]]></description>", f"      <link>{escape(j['apply_url'])}</link>",
            f"      <apply_url>{escape(j['apply_url'])}</apply_url>", f"      <job_reference>{escape(j['job_reference'])}</job_reference>",
            f"      <guid isPermaLink=\"false\">{escape(j['job_reference'])}</guid>", f"      <pubDate>{j['pubDate']}</pubDate>", "    </item>"
        ])
    out.extend(["  </channel>", "</rss>"])
    return "\n".join(out)

def main():
    ap = argparse.ArgumentParser(description="Golf Jobs master XML feed generator")
    ap.add_argument("--config", default="sources.json")
    ap.add_argument("--out", default="feed.xml")
    ap.add_argument("--gzip", action="store_true", help="also write <out>.gz")
    args = ap.parse_args()

    with open(args.config) as f: sources = json.load(f)["sources"]
    all_jobs, report = [], []
    for src in sources:
        if not isinstance(src, dict) or not src.get("enabled", True): continue
        adapter = ADAPTERS.get(src["ats"])
        if not adapter:
            report.append(f"SKIP  {src['company']}: unknown ats '{src['ats']}'")
            continue
        try:
            jobs = adapter(src)
            if src.get("golf_only"):
                filtered = []
                keywords = ["golf", "pga", "greenkeeper", "agronomy", "turf", "caddie", "clubhouse", "titleist", "taylormade", "trackman", "putting", "whistling straits", "old course", "blackwolf run"]
                for j in jobs:
                    search_text = f"{j.get('title','')} {j.get('category','')} {j.get('description','')} {j.get('company','')}".lower()
                    if any(k in search_text for k in keywords): filtered.append(j)
                report.append(f"FILTER {src['company']}: kept {len(filtered)} of {len(jobs)} jobs")
                jobs = filtered
            else:
                report.append(f"OK    {src['company']}: {len(jobs)} jobs added")
            all_jobs.extend(jobs)
        except Exception as e:
            report.append(f"FAIL  {src['company']}: {e}")

    seen, deduped = set(), []
    for j in all_jobs:
        if j["job_reference"] in seen: continue
        seen.add(j["job_reference"])
        deduped.append(j)

    xml = build_rss(deduped)
    with open(args.out, "w", encoding="utf-8") as f: f.write(xml)
    if args.gzip:
        with gzip.open(args.out + ".gz", "wb") as f: f.write(xml.encode("utf-8"))
    print("\n".join(report), file=sys.stderr)
    print(f"\nTOTAL: {len(deduped)} jobs written to {args.out}", file=sys.stderr)

if __name__ == "__main__": main()
