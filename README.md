# ATS Job Scraper

Scrape public job boards from **Greenhouse, Ashby, Lever and Workday** behind one unified input and output schema. No API keys, no logins — just point it at public boards and get clean, structured job rows.

## Supported boards

Add one board per line in the `boards` input:

| Type | Format | Example |
|---|---|---|
| Greenhouse | `greenhouse:BOARD_TOKEN` | `greenhouse:airtable` |
| Ashby | `ashby:ORG_TOKEN` | `ashby:linear` |
| Lever | `lever:ORG_TOKEN` | `lever:spotify` |
| Workday | `workday:TENANT/SITE` | `workday:capgroup/capitalgroupcareers` |

The board token is the slug in the company's public careers URL (e.g. `jobs.ashbyhq.com/linear` → `ashby:linear`). For Workday you need both the tenant and the site slug from the board URL, e.g. `https://capgroup.wd1.myworkdayjobs.com/en-US/capitalgroupcareers` → `workday:capgroup/capitalgroupcareers`. The actor automatically tries the Workday shards (wd1/wd2/wd3/wd5) until one responds.

## Inputs

- **boards** (required) — one board per line, as above.
- **maxJobsPerBoard** — cap per board (default 100).
- **search** — optional keyword filter; only jobs whose title or description contains it are kept.
- **includeDescription** — include the plain-text description (truncated to 2000 chars). Disable for smaller rows.

## Output fields

One dataset row per job:

| Field | Description |
|---|---|
| jobUid | Stable id: `type:token:source-id` |
| title | Job title |
| company | Hiring company |
| locations | Location(s) as a string |
| workplaceType | Remote / Hybrid / On-site (when the board reports it) |
| salaryMin / salaryMax / salaryCurrency | Pay range when the board publishes one |
| applyUrl | Link to the posting |
| description | Plain-text description (2000 chars max) |
| postedAt | Publication timestamp (ISO 8601) |
| board | Source board, e.g. `greenhouse:airtable` |

## Pricing

| Event | Price |
|---|---|
| Job scraped | **$0.002** per job posting scraped |

You are charged once per job posting successfully scraped and stored — never for failed boards or failed jobs. Buyers also pay the standard Apify platform usage costs for the run.
