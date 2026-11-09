# SPDX-License-Identifier: AGPL-3.0-only
"""Company identity resolution: job rows -> a normalized company key.

The store is a flat jobs table keyed by posting URL; nothing groups a
company's postings together. This layer derives that identity from the
fields the discovery writers already hold — heuristics only, no LLM
entity resolution, no network (locked decisions, hiring-leads spec
art_u0YGUqpk §1).

Resolution order per job row (first hit wins):

1. The JobSpy writer's parsed ``company`` (persisted at ingestion).
2. A Workday subdomain (``{slug}.wd{N}.myworkdayjobs.com``) mapped
   through ``config/employers.yaml``.
3. The Composio writer's folded ``Company: X`` description header.

The domain follows the posting URL, not the name: a direct-ATS host or
a Workday subdomain yields a domain (domain-confidence identity); any
known job-board or multi-tenant-ATS host yields None and the identity
stays name-confidence (key prefixed ``name:``).
"""

import re

# Workday career-site host: {slug}.wd{N}.myworkdayjobs.com. The slug is
# the employer's chosen subdomain — stable per employer — and the
# employers.yaml registry turns it into a display name.
WORKDAY_HOST_RE = re.compile(
    r"^([a-z0-9][a-z0-9-]*)\.wd\d+\.myworkdayjobs\.com$", re.IGNORECASE
)

# Hosts that are never the employer's own domain: job boards and
# multi-tenant ATS platforms, where the URL's host belongs to the
# platform and the employer appears (if at all) as a path segment.
NON_COMPANY_DOMAINS: tuple[str, ...] = (
    # Job boards
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "simplyhired.com", "monster.com", "dice.com", "google.com",
    "wellfound.com", "builtin.com", "remoteok.com", "weworkremotely.com",
    "web3.career",
    # Multi-tenant ATS hosts (the employer is a path/tenant, not the host)
    "lever.co", "greenhouse.io", "workable.com", "ashbyhq.com",
    "smartrecruiters.com", "taleo.net", "icims.com", "jobvite.com",
    "eightfold.ai", "successfactors.com", "paylocity.com",
    "myworkdayjobs.com",  # Workday is resolved via the registry path below
)

# Leading host prefixes that carry no employer identity on a direct-ATS
# host (jobs.acme-robotics.com is Acme Robotics).
_CAREER_HOST_PREFIXES = ("www.", "jobs.", "careers.", "career.", "job.")

# The Composio writer folds identity into the description's first line
# ("Company: Acme") — map_web3_job in discovery/composio.py.
_COMPANY_HEADER_RE = re.compile(r"company:\s*(.+)", re.IGNORECASE)


def _host_of(url: str | None) -> str | None:
    """Lowercased hostname of a posting URL, or None when absent/invalid."""
    if not url or not url.strip():
        return None
    from urllib.parse import urlparse

    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    """True when host is one of domains or a subdomain of one."""
    return any(host == d or host.endswith("." + d) for d in domains)


def domain_for_company(url: str | None) -> str | None:
    """Employer-domain heuristic for one posting URL, or None.

    A direct-ATS posting (the URL's host is the employer itself) gives
    the real domain, with common career subdomain prefixes stripped. A
    Workday host gives its registry-stable subdomain. Any known
    job-board or multi-tenant-ATS host yields None — the identity stays
    name-confidence rather than inheriting the platform's domain.
    """
    host = _host_of(url)
    if not host:
        return None
    if WORKDAY_HOST_RE.match(host):
        return host
    if _host_matches(host, NON_COMPANY_DOMAINS):
        return None
    for prefix in _CAREER_HOST_PREFIXES:
        if host.startswith(prefix):
            stripped = host[len(prefix):]
            # Keep the original when nothing registrable remains ("jobs.com").
            if "." in stripped:
                return stripped
    return host


def company_from_workday_subdomain(url: str | None,
                                   registry: dict | None = None) -> str | None:
    """Display name for a Workday posting URL, via config/employers.yaml.

    The subdomain is identity the URL itself carries; the registry turns
    the slug into the employer's display name. Slugs the registry
    doesn't know resolve to None — an unknown employer is a visible gap
    the backfill leaves for a later run (once the registry grows), not a
    guess.
    """
    host = _host_of(url)
    if not host:
        return None
    match = WORKDAY_HOST_RE.match(host)
    if not match:
        return None
    if registry is None:
        from kestrel.discovery.workday import load_employers

        registry = load_employers()
    return registry.get(match.group(1).lower(), {}).get("name")


def company_from_description_header(description: str | None) -> str | None:
    """Recover the Composio writer's folded ``Company: X`` header.

    map_web3_job folds the company into the description as its first
    line because the shared store had no dedicated column. Only the
    leading line is inspected — the exact shape the writer emits — so a
    "Company:" mention deeper in a body never masquerades as identity.
    """
    if not description:
        return None
    first_line = description.lstrip().split("\n", 1)[0].strip()
    match = _COMPANY_HEADER_RE.match(first_line)
    if not match:
        return None
    return match.group(1).strip() or None


def normalize_name_key(name: str) -> str:
    """Normalize a display name to a stable key fragment.

    Lowercase, with runs of non-alphanumeric characters collapsed to a
    single hyphen ("Acme Robotics, Inc." -> "acme-robotics-inc").
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def company_key_for(name: str, domain: str | None) -> str:
    """The company_identity primary key for one resolved identity.

    The lowercased domain when one is derivable (highest confidence),
    else the normalized display name under the ``name:`` prefix — the
    marker that this key is name-confidence, not a verified web domain.
    """
    if domain:
        return domain.lower()
    return "name:" + (normalize_name_key(name) or "unknown-company")


def resolve_company_identity(url: str | None,
                             company: str | None,
                             description: str | None,
                             registry: dict | None = None,
                             ) -> tuple[str | None, str | None]:
    """Resolve (display_name, domain) for one job row, or (None, None).

    Order: the writer's parsed name, the Workday registry, the Composio
    description header. The domain comes from the posting URL alone
    (direct ATS or Workday host); when none is derivable the identity is
    name-confidence and the domain is None.
    """
    name = (company
            or company_from_workday_subdomain(url, registry)
            or company_from_description_header(description))
    if not name:
        return None, None
    return name, domain_for_company(url)
