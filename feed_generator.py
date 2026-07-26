#!/usr/bin/env python3
"""
Golf Jobs — master XML feed generator for JBoard.

Pulls live vacancies from each golf employer's applicant tracking system (ATS)
and writes ONE combined RSS feed that JBoard's XML importer can ingest.

Supported ATS adapters (Tier 1 — clean JSON APIs, no scraping needed):
    greenhouse, lever, recruitee, smartrecruiters, workable, ashby, workday

Usage:
    python3 feed_generator.py --config sources.json --out feed.xml
    python3 feed_generator.py --config sources.json --out feed.xml --gzip

Design notes:
- Every job becomes one <item> carrying its own <company> tag, so a SINGLE
  JBoard importer can auto-create every employer from the feed.
- <job_reference> is a stable unique ID (source-slug + native id) so JBoard
  UPDATES rather than duplicates on each 24h run.
- <pubDate> is set to the import time by default (JBoard best practice: stops
  old native posting dates from making jobs expire on arrival). Set
  USE_NATIVE_DATE = True to use the ATS's original date instead.
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
USE_NATIVE_DATE = False       # False = use import date (recommended for JBoard)
REQUEST_TIMEOUT = 25          # seconds per HTTP request
RETRIES = 3                   # retries per request
RETRY_BACKOFF = 3             # seconds, multiplied by attempt number
USER_AGENT = "golf-jobs-feed/1.0 (+https://golf-jobs.com)"


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
    """GET returning decoded text (for RSS/XML feeds). Handles gzip."""
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
    """Keep basic HTML for job descriptions; ensure it is a string."""
    if text is None:
        return ""
    return str(text)


def _now_rfc822():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _to_rfc822(value):
    """Best-effort convert an ATS date (ISO string or epoch ms) to RFC-822."""
    if not value:
        return _now_rfc822()
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
# ATS adapters  — each returns a list of make_job(...) dicts
# ---------------------------------------------------------------------------
def fetch_greenhouse(src):
    # token = the greenhouse board token, e.g. "whoop"
    token = src["token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    data = _http(url)
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        dept = ""
        if j.get("departments"):
            dept = j["departments"][0].get("name", "")
        jobs.append(make_job(
            src["slug"], j["id"], j.get("title"), src["company"],
            j.get("absolute_url"),
            description=html.unescape(j.get("content", "")),
            location=loc, category=dept, posted=j.get("updated_at"),
        ))
    return jobs


def fetch_lever(src):
    token = src["token"]  # lever company handle, e.g. "whoop"
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    data = _http(url)
    jobs = []
    for j in data:
        cats = j.get("categories", {}) or {}
        jobs.append(make_job(
            src["slug"], j["id"], j.get("text"), src["company"],
            j.get("applyUrl") or j.get("hostedUrl"),
            description=j.get("description") or j.get("descriptionPlain", ""),
            location=cats.get("location", ""),
            category=cats.get("team") or cats.get("department", ""),
            job_type=cats.get("commitment", ""),
            remote=("remote" in (cats.get("location", "") or "").lower()),
            posted=j.get("createdAt"),
        ))
    return jobs


def fetch_recruitee(src):
    company = src["token"]  # subdomain, e.g. "trackman"
    url = f"https://{company}.recruitee.com/api/offers/"
    data = _http(url)
    jobs = []
    for j in data.get("offers", []):
        loc = j.get("location") or ", ".join(
            x for x in [j.get("city"), j.get("country")] if x)
        jobs.append(make_job(
            src["slug"], j["id"], j.get("title"), src["company"],
            j.get("careers_url") or j.get("careers_apply_url"),
            description=j.get("description", ""),
            location=loc, category=j.get("department", ""),
            job_type=j.get("employment_type_code", ""),
            remote=bool(j.get("remote")), posted=j.get("published_at") or j.get("created_at"),
        ))
    return jobs


def fetch_smartrecruiters(src):
    company = src["token"]  # e.g. "AmericanGolfUKLtd"
    base = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    jobs, offset, limit = [], 0, 100
    while True:
        data = _http(f"{base}?limit={limit}&offset={offset}")
        content = data.get("content", [])
        for p in content:
            loc = p.get("location", {}) or {}
            loc_str = ", ".join(x for x in [loc.get("city"), loc.get("region"),
                                            loc.get("country")] if x)
            apply_url = (p.get("ref", {}) or {}).get("jobAd") \
                or f"https://jobs.smartrecruiters.com/{company}/{p['id']}"
            # detail call for full description
            desc = ""
            try:
                detail = _http(f"{base}/{p['id']}")
                sections = (detail.get("jobAd", {}) or {}).get("sections", {}) or {}
                desc = "".join((sections.get(k, {}) or {}).get("text", "")
                               for k in ("companyDescription", "jobDescription",
                                         "qualifications", "additionalInformation"))
            except Exception:
                pass
            jobs.append(make_job(
                src["slug"], p["id"], p.get("name"), src["company"], apply_url,
                description=desc, location=loc_str,
                category=(p.get("function", {}) or {}).get("label", ""),
                job_type=(p.get("typeOfEmployment", {}) or {}).get("label", ""),
                remote=bool(loc.get("remote")), posted=p.get("releasedDate"),
            ))
        if len(content) < limit:
            break
        offset += limit
    return jobs


def fetch_ashby(src):
    org = src["token"]  # jobBoardName
    url = "https://api.ashbyhq.com/posting-api/job-board/" + org + "?includeCompensation=true"
    data = _http(url)
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(make_job(
            src["slug"], j["id"], j.get("title"), src["company"],
            j.get("applyUrl") or j.get("jobUrl"),
            description=j.get("descriptionHtml") or j.get("descriptionPlain", ""),
            location=j.get("location", ""),
            category=j.get("department", ""),
            job_type=j.get("employmentType", ""),
            remote=bool(j.get("isRemote")), posted=j.get("publishedAt"),
        ))
    return jobs


def fetch_workable(src):
    account = src["token"]  # subdomain
    url = f"https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"
    data = _http(url)
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location", {}) or {}
        loc_str = ", ".join(x for x in [loc.get("city"), loc.get("region"),
                                        loc.get("country")] if x)
        jobs.append(make_job(
            src["slug"], j.get("shortcode") or j.get("id"), j.get("title"),
            src["company"], j.get("url") or j.get("application_url"),
            description=j.get("description", ""), location=loc_str,
            category=j.get("department", ""), job_type=j.get("employment_type", ""),
            remote=bool(loc.get("telecommuting")), posted=j.get("published_on"),
        ))
    return jobs


def fetch_workday(src):
    """
    Workday exposes a hidden JSON endpoint used by its own career site UI:
        POST https://{host}/wday/cxs/{tenant}/{site}/jobs
    Provide in config:
        "host": "acushnetgolf.wd12.myworkdayjobs.com",
        "tenant": "acushnetgolf",
        "site": "ACU"
    """
    host, tenant, site = src["host"], src["tenant"], src["site"]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    jobs, offset, limit = [], 0, 20
    while True:
        data = _http(f"{base}/jobs", method="POST",
                     data={"limit": limit, "offset": offset, "searchText": "",
                           "appliedFacets": {}})
        postings = data.get("jobPostings", [])
        for p in postings:
            ext = p.get("externalPath", "")
            apply_url = f"https://{host}{ext}" if ext else ""
            native = ext.rsplit("/", 1)[-1] if ext else p.get("bulletFields", [""])[0]
            jobs.append(make_job(
                src["slug"], native, p.get("title"), src["company"], apply_url,
                description="",  # full desc needs per-job POST; title+link is enough for JBoard
                location=p.get("locationsText", ""),
                posted=p.get("postedOn", ""),
            ))
        total = data.get("total", 0)
        offset += limit
        if offset >= total or not postings:
            break
    return jobs


_TT_NS = {"tt": "https://teamtailor.com/locations"}


def fetch_teamtailor(src):
    """
    Teamtailor exposes a public RSS feed at /jobs.rss (no auth).
    Config: "token" = subdomain (e.g. "yourgolftravel"), OR "host" = full
    custom domain (e.g. "careers.castore.com"). Optional "per_page" (default 200).
    """
    host = src.get("host") or f"{src['token']}.teamtailor.com"
    per_page = src.get("per_page", 200)
    url = f"https://{host}/jobs.rss?per_page={per_page}"
    text = _http_text(url, headers={
        "Accept": "application/rss+xml, application/xml, text/xml"})
    if "<rss" not in text:
        raise RuntimeError("no RSS feed for this Teamtailor board")
    root = ET.fromstring(text)
    channel = root.find("channel")
    jobs = []
    for item in (channel.findall("item") if channel is not None else []):
        link = item.findtext("link", "") or ""
        m = re.search(r"/jobs/(\d+)", link)
        native = m.group(1) if m else (item.findtext("guid") or link)
        desc = html.unescape(item.findtext("description", "") or "")
        if any(e in desc for e in ("&lt;", "&gt;", "&amp;")):
            desc = html.unescape(desc)  # Teamtailor sometimes double-encodes
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
            if combo:
                locs.append(combo)
        remote_flag = (item.findtext("remoteStatus", "") or "").lower()
        jobs.append(make_job(
            src["slug"], native, item.findtext("title"), src["company"], link,
            description=desc, location="; ".join(locs), category=dept or "",
            remote=remote_flag not in ("", "none", "no", "false"),
            posted=item.findtext("pubDate"),
        ))
    return jobs


def fetch_dayforce(src):
    """
    Dayforce candidate portal public geo-search API.
    Config: "token" = client namespace (e.g. "pinehurst"); optional
    "job_board_code" (default CANDIDATEPORTAL) and "culture" (default en-US).

    Note: the endpoint sits behind Cloudflare + CSRF. We prime cookies with a
    GET to the board and the csrf endpoint, then POST. If Cloudflare blocks the
    runner (HTTP 403), this source will report FAIL and the run continues;
    handle those employers with JBoard's Web Page Scraper instead.
    """
    ns = src["token"]
    board = src.get("job_board_code", "CANDIDATEPORTAL")
    culture = src.get("culture", "en-US")
    base_hdr = {"User-Agent": USER_AGENT}
    cj = http.cookiejar.CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(cj))

    # prime Cloudflare + session cookies
    opener.open(request.Request(
        f"https://jobs.dayforcehcm.com/{culture}/{ns}/{board}", headers=base_hdr),
        timeout=REQUEST_TIMEOUT).read()
    csrf = ""
    try:
        r = opener.open(request.Request(
            "https://jobs.dayforcehcm.com/api/auth/csrf",
            headers={**base_hdr, "Accept": "application/json"}),
            timeout=REQUEST_TIMEOUT)
        csrf = json.loads(r.read().decode("utf-8")).get("csrfToken", "")
    except Exception:
        pass

    url = f"https://jobs.dayforcehcm.com/api/geo/{ns}/jobposting/search"
    jobs, offset = [], 0
    while True:
        payload = {"clientNamespace": ns, "jobBoardCode": board,
                   "cultureCode": culture, "distanceUnit": 0,
                   "paginationStart": offset}
        hdrs = {**base_hdr, "Content-Type": "application/json",
                "Accept": "application/json"}
        if csrf:
            hdrs["x-csrf-token"] = csrf
        req = request.Request(url, data=json.dumps(payload).encode("utf-8"),
                              headers=hdrs, method="POST")
        data = json.loads(opener.open(req, timeout=REQUEST_TIMEOUT).read().decode("utf-8"))
        postings = data.get("jobPostings", []) or []
        for p in postings:
            locs = p.get("postingLocations", []) or []
            pl = locs[0] if locs else {}
            loc_str = pl.get("formattedAddress") or ", ".join(
                x for x in [pl.get("cityName"), pl.get("stateCode"),
                            pl.get("isoCountryCode")] if x)
            jid = p.get("jobPostingId")
            apply_url = f"https://jobs.dayforcehcm.com/{culture}/{ns}/{board}/jobs/{jid}"
            jobs.append(make_job(
                src["slug"], jid, p.get("jobTitle"), src["company"], apply_url,
                description=p.get("jobDescription", ""), location=loc_str,
                posted=p.get("postingStartTimestampUTC"),
            ))
        max_count = data.get("maxCount", 0)
        offset += 25
        if offset >= max_count or not postings:
            break
    return jobs


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "recruitee": fetch_recruitee,
    "smartrecruiters": fetch_smartrecruiters,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "workday": fetch_workday,
    "teamtailor": fetch_teamtailor,
    "dayforce": fetch_dayforce,
}


# ---------------------------------------------------------------------------
# RSS writer  (JBoard-ready)
# ---------------------------------------------------------------------------
def build_rss(jobs):
    now = _now_rfc822()
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append('<rss version="2.0">')
    out.append("  <channel>")
    out.append("    <title>Golf Jobs — Aggregated Employer Feed</title>")
    out.append("    <link>https://golf-jobs.com</link>")
    out.append("    <description>Live vacancies aggregated from golf employer ATS platforms</description>")
    out.append(f"    <lastBuildDate>{now}</lastBuildDate>")
    for j in jobs:
        out.append("    <item>")
        out.append(f"      <title>{escape(j['title'])}</title>")
        out.append(f"      <company>{escape(j['company'])}</company>")
        out.append(f"      <location>{escape(j['location'])}</location>")
        out.append(f"      <category>{escape(j['category'])}</category>")
        out.append(f"      <job_type>{escape(j['job_type'])}</job_type>")
        out.append(f"      <remote>{'true' if j['remote'] else 'false'}</remote>")
        out.append(f"      <description><![CDATA[{j['description']}]]></description>")
        out.append(f"      <link>{escape(j['apply_url'])}</link>")
        out.append(f"      <apply_url>{escape(j['apply_url'])}</apply_url>")
        out.append(f"      <job_reference>{escape(j['job_reference'])}</job_reference>")
        out.append(f"      <guid isPermaLink=\"false\">{escape(j['job_reference'])}</guid>")
        out.append(f"      <pubDate>{j['pubDate']}</pubDate>")
        out.append("    </item>")
    out.append("  </channel>")
    out.append("</rss>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Golf Jobs master XML feed generator")
    ap.add_argument("--config", default="sources.json")
    ap.add_argument("--out", default="feed.xml")
    ap.add_argument("--gzip", action="store_true", help="also write <out>.gz")
    args = ap.parse_args()

    with open(args.config) as f:
        sources = json.load(f)["sources"]

    all_jobs, report = [], []
    for src in sources:
        if not isinstance(src, dict):
            continue  # skip label/separator strings in the config
        if not src.get("enabled", True):
            continue
        adapter = ADAPTERS.get(src["ats"])
        if not adapter:
            report.append(f"SKIP  {src['company']}: unknown ats '{src['ats']}'")
            continue
        try:
            jobs = adapter(src)
            all_jobs.extend(jobs)
            report.append(f"OK    {src['company']}: {len(jobs)} jobs")
        except Exception as e:
            report.append(f"FAIL  {src['company']}: {e}")

    # de-dupe by job_reference (safety net)
    seen, deduped = set(), []
    for j in all_jobs:
        if j["job_reference"] in seen:
            continue
        seen.add(j["job_reference"])
        deduped.append(j)

    xml = build_rss(deduped)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(xml)
    if args.gzip:
        with gzip.open(args.out + ".gz", "wb") as f:
            f.write(xml.encode("utf-8"))

    print("\n".join(report), file=sys.stderr)
    print(f"\nTOTAL: {len(deduped)} jobs written to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
