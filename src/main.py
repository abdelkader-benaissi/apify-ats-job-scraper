"""ATS Job Scraper — scrape public job boards (Greenhouse, Ashby, Lever, Workday)
behind one unified schema. HTTP-only, no browser needed."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from apify import Actor
from bs4 import BeautifulSoup

log = logging.getLogger("ats-job-scraper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 30.0
DESCRIPTION_MAX_CHARS = 2000

# ---------------------------------------------------------------- SSRF guard

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved:
        return True
    return any(addr in net for net in _PRIVATE_NETWORKS)


async def assert_public_host(host: str) -> None:
    """Refuse to fetch URLs whose host resolves to a non-public IP (SSRF guard)."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError as exc:
        raise ValueError(f"cannot resolve host {host!r}: {exc}") from exc
    ips = {info[4][0] for info in infos}
    if not ips:
        raise ValueError(f"host {host!r} resolved to no addresses")
    for ip in ips:
        if _ip_is_blocked(ip):
            raise ValueError(f"refusing to fetch non-public host {host!r} (resolves to {ip})")


async def fetch_json(client: httpx.AsyncClient, method: str, url: str,
                     json_body: dict | None = None) -> Any:
    await assert_public_host(urlparse(url).hostname or "")
    resp = await client.request(method, url, json=json_body, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

# ---------------------------------------------------------------- helpers


def html_to_text(html: str | None, max_chars: int = DESCRIPTION_MAX_CHARS) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def prettify_token(token: str) -> str:
    return re.sub(r"[-_]+", " ", token).strip().title() or token


def norm_workplace(value: Any) -> str:
    v = str(value or "").strip().lower()
    if "remote" in v:
        return "Remote"
    if "hybrid" in v:
        return "Hybrid"
    if "on-site" in v or "onsite" in v or "office" in v:
        return "On-site"
    return str(value or "").strip()


_MONEY_RE = re.compile(r"[$€£]?\s*(\d[\d,]*(?:\.\d+)?)\s*[kK]?")


def parse_pay_range(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Parse strings like '$100,000 - $140,000' or '€80k–€95k' into (min, max, currency)."""
    if not text:
        return None, None, None
    currency = None
    if "$" in text:
        currency = "USD"
    elif "€" in text:
        currency = "EUR"
    elif "£" in text:
        currency = "GBP"
    nums: list[float] = []
    for m in _MONEY_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        # 'k' suffix handling: check the char right after the number in original text
        tail = text[m.end():m.end() + 1].lower()
        if tail == "k":
            val *= 1000
        nums.append(val)
    if not nums:
        return None, None, currency
    lo = min(nums)
    hi = max(nums) if len(nums) > 1 else None
    return lo, hi, currency


def epoch_ms_to_iso(ms: Any) -> str:
    try:
        dt = datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError):
        return str(ms or "")


def matches_search(item: dict, search: str) -> bool:
    if not search:
        return True
    hay = f"{item.get('title', '')} {item.get('description', '')}".lower()
    return search.lower() in hay

# ---------------------------------------------------------------- board scrapers
# Each returns a list of raw job dicts: {"id", "title", "company", "locations",
# "workplaceType", "salaryMin", "salaryMax", "salaryCurrency", "applyUrl",
# "description", "postedAt"}.


async def scrape_greenhouse(client: httpx.AsyncClient, token: str,
                            max_jobs: int, include_desc: bool) -> tuple[list[dict], str]:
    last_err: Exception | None = None
    for host in ("boards-api.greenhouse.io", "boards.greenhouse.io"):
        url = f"https://{host}/v1/boards/{token}/jobs?content=true"
        try:
            data = await fetch_json(client, "GET", url)
            break
        except Exception as exc:  # noqa: BLE001 - try fallback host, then report
            last_err = exc
            log.warning("greenhouse host %s failed: %s", host, exc)
    else:
        raise RuntimeError(f"greenhouse board {token!r} unreachable: {last_err}")

    jobs: list[dict] = []
    for j in (data.get("jobs") or [])[:max_jobs]:
        salary_min = salary_max = salary_cur = None
        for meta in j.get("metadata") or []:
            name = str(meta.get("name", "")).lower()
            if "pay" in name or "salary" in name or "compensation" in name:
                salary_min, salary_max, salary_cur = parse_pay_range(str(meta.get("value") or ""))
                if salary_min is not None:
                    break
        loc = j.get("location") or {}
        jobs.append({
            "id": str(j.get("id")),
            "title": j.get("title") or "",
            "company": j.get("company_name") or prettify_token(token),
            "locations": loc.get("name", "") if isinstance(loc, dict) else str(loc),
            "workplaceType": "",
            "salaryMin": salary_min,
            "salaryMax": salary_max,
            "salaryCurrency": salary_cur or "",
            "applyUrl": j.get("absolute_url") or "",
            "description": html_to_text(j.get("content")) if include_desc else "",
            "postedAt": j.get("updated_at") or j.get("first_published") or "",
        })
    return jobs, f"greenhouse:{token}"


async def scrape_ashby(client: httpx.AsyncClient, token: str,
                       max_jobs: int, include_desc: bool) -> tuple[list[dict], str]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    data = await fetch_json(client, "GET", url)
    jobs: list[dict] = []
    for j in (data.get("jobs") or [])[:max_jobs]:
        salary_min = salary_max = salary_cur = None
        comp = j.get("compensationTierSummary")
        if isinstance(comp, dict):
            try:
                salary_min = float(comp["min"]) if comp.get("min") is not None else None
                salary_max = float(comp["max"]) if comp.get("max") is not None else None
                salary_cur = str(comp.get("currency") or "")
            except (TypeError, ValueError):
                pass
        elif isinstance(comp, str):
            salary_min, salary_max, salary_cur = parse_pay_range(comp)
        locs = [str(j.get("location") or "")]
        locs += [str(s) for s in (j.get("secondaryLocations") or []) if s]
        jobs.append({
            "id": str(j.get("id")),
            "title": j.get("title") or "",
            "company": prettify_token(token),
            "locations": ", ".join(l for l in locs if l),
            "workplaceType": norm_workplace(j.get("workplaceType") or ("Remote" if j.get("isRemote") else "")),
            "salaryMin": salary_min,
            "salaryMax": salary_max,
            "salaryCurrency": salary_cur or "",
            "applyUrl": j.get("jobUrl") or j.get("applyUrl") or "",
            "description": html_to_text(j.get("descriptionHtml") or j.get("descriptionPlain")) if include_desc else "",
            "postedAt": j.get("publishedAt") or "",
        })
    return jobs, f"ashby:{token}"


async def scrape_lever(client: httpx.AsyncClient, token: str,
                      max_jobs: int, include_desc: bool) -> tuple[list[dict], str]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    data = await fetch_json(client, "GET", url)
    jobs: list[dict] = []
    for j in (data if isinstance(data, list) else [])[:max_jobs]:
        cats = j.get("categories") or {}
        locs = [str(cats.get("location") or "")]
        locs += [str(x) for x in (cats.get("allLocations") or []) if x and x != cats.get("location")]
        jobs.append({
            "id": str(j.get("id")),
            "title": j.get("text") or "",
            "company": prettify_token(token),
            "locations": ", ".join(l for l in locs if l),
            "workplaceType": norm_workplace(j.get("workplaceType") or ""),
            "salaryMin": None,
            "salaryMax": None,
            "salaryCurrency": "",
            "applyUrl": j.get("hostedUrl") or j.get("applyUrl") or "",
            "description": html_to_text(j.get("description")) if include_desc else "",
            "postedAt": epoch_ms_to_iso(j.get("createdAt")),
        })
    return jobs, f"lever:{token}"


_WORKDAY_SHARDS = ("wd1", "wd3", "wd5", "wd2")


async def scrape_workday(client: httpx.AsyncClient, token: str,
                        max_jobs: int, include_desc: bool) -> tuple[list[dict], str]:
    # token format: TENANT/SITE  (e.g. capgroup/capitalgroupcareers)
    if "/" not in token:
        raise ValueError(
            f"workday board {token!r}: expected format workday:TENANT/SITE "
            "(e.g. workday:capgroup/capitalgroupcareers)"
        )
    tenant, site = token.split("/", 1)
    tenant, site = tenant.strip(), site.strip()
    base_url: str | None = None
    for shard in _WORKDAY_SHARDS:
        url = f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        try:
            await assert_public_host(urlparse(url).hostname or "")
            resp = await client.post(url, json={"appliedFacets": {}, "limit": 1,
                                                "offset": 0, "searchText": ""},
                                     timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                base_url = url
                break
            log.warning("workday %s returned HTTP %s", url, resp.status_code)
        except Exception as exc:  # noqa: BLE001 - try next shard
            log.warning("workday %s failed: %s", url, exc)
    if not base_url:
        raise RuntimeError(f"workday board {token!r}: no responding shard (tried {', '.join(_WORKDAY_SHARDS)})")

    jobs: list[dict] = []
    offset = 0
    while len(jobs) < max_jobs:
        limit = min(100, max_jobs - len(jobs))
        data = await fetch_json(client, "POST", base_url,
                                {"appliedFacets": {}, "limit": limit,
                                 "offset": offset, "searchText": ""})
        postings = data.get("jobPostings") or []
        if not postings:
            break
        shard = urlparse(base_url).hostname.split(".")[1]
        for jp in postings:
            ext = str(jp.get("externalPath") or "")
            jobs.append({
                "id": ext.rsplit("_", 1)[-1] if "_" in ext else ext,
                "title": jp.get("title") or "",
                "company": prettify_token(tenant),
                "locations": jp.get("locationsText") or "",
                "workplaceType": "",
                "salaryMin": None,
                "salaryMax": None,
                "salaryCurrency": "",
                "applyUrl": (f"https://{tenant}.{shard}.myworkdayjobs.com/en-US/{site}{ext}"
                             if ext else ""),
                "description": "",  # list endpoint has no description; detail fetch is per-job
                "postedAt": jp.get("postedOn") or "",
            })
        offset += len(postings)
        if offset >= (data.get("total") or 0):
            break
    return jobs, f"workday:{token}"


_SCRAPERS = {
    "greenhouse": scrape_greenhouse,
    "ashby": scrape_ashby,
    "lever": scrape_lever,
    "workday": scrape_workday,
}

# ---------------------------------------------------------------- main


def parse_boards(raw: str) -> list[tuple[str, str]]:
    boards: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"invalid board line {line!r}: expected type:token")
        btype, token = line.split(":", 1)
        btype, token = btype.strip().lower(), token.strip()
        if btype not in _SCRAPERS:
            raise ValueError(f"unknown board type {btype!r} (use: {', '.join(sorted(_SCRAPERS))})")
        if not token:
            raise ValueError(f"empty token in board line {line!r}")
        boards.append((btype, token))
    if not boards:
        raise ValueError("no boards given: add at least one line like 'greenhouse:airtable'")
    return boards


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        boards = parse_boards(str(inp.get("boards") or ""))
        max_jobs = int(inp.get("maxJobsPerBoard") or 100)
        search = str(inp.get("search") or "").strip()
        include_desc = bool(inp.get("includeDescription", True))

        Actor.log.info("Scraping %d board(s), max %d jobs each", len(boards), max_jobs)
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT,
                                              "Accept": "application/json"}) as client:
            total = pushed = failed_boards = 0
            for btype, token in boards:
                label = f"{btype}:{token}"
                try:
                    raw_jobs, board_label = await _SCRAPERS[btype](client, token,
                                                                   max_jobs, include_desc)
                except Exception as exc:  # noqa: BLE001 - per-board graceful failure
                    failed_boards += 1
                    Actor.log.exception("Board %s failed: %s", label, exc)
                    continue
                for rj in raw_jobs:
                    item = {
                        "jobUid": f"{btype}:{token}:{rj['id']}",
                        "title": rj["title"],
                        "company": rj["company"],
                        "locations": rj["locations"],
                        "workplaceType": rj["workplaceType"],
                        "salaryMin": rj["salaryMin"],
                        "salaryMax": rj["salaryMax"],
                        "salaryCurrency": rj["salaryCurrency"],
                        "applyUrl": rj["applyUrl"],
                        "description": rj["description"],
                        "postedAt": rj["postedAt"],
                        "board": board_label,
                    }
                    if not matches_search(item, search):
                        continue
                    total += 1
                    try:
                        # Push first, charge only after a successful push — never
                        # charge for a job the buyer did not receive.
                        await Actor.push_data(item)
                        await Actor.charge("job-scraped")
                        pushed += 1
                    except Exception as exc:  # noqa: BLE001 - keep the run alive
                        Actor.log.exception("Failed to store job %s: %s", item["jobUid"], exc)
                Actor.log.info("Board %s: %d jobs scraped", label, len(raw_jobs))

        await Actor.set_value("OUTPUT", {
            "total": total,
            "pushed": pushed,
            "boardsRequested": len(boards),
            "boardsFailed": failed_boards,
            "finishedAt": datetime.now(timezone.utc).isoformat(),
        })
        Actor.log.info("Done: %d jobs pushed (%d boards failed)", pushed, failed_boards)
