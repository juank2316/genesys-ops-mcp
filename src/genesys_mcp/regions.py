"""Genesys Cloud regions -> base domain.

API is https://api.<domain>, login is https://login.<domain>.
A profile's `region` may be the AWS-style code (us-east-1) or the domain itself.
"""

from __future__ import annotations

REGION_DOMAINS: dict[str, str] = {
    "us-east-1": "mypurecloud.com",
    "us-east-2": "use2.us-gov-pure.cloud",
    "us-west-2": "usw2.pure.cloud",
    "ca-central-1": "cac1.pure.cloud",
    "sa-east-1": "sae1.pure.cloud",
    "mx-central-1": "mxc1.pure.cloud",
    "eu-west-1": "mypurecloud.ie",
    "eu-west-2": "euw2.pure.cloud",
    "eu-central-1": "mypurecloud.de",
    "eu-central-2": "euc2.pure.cloud",
    "me-central-1": "mec1.pure.cloud",
    "ap-south-1": "aps1.pure.cloud",
    "ap-southeast-1": "apse1.pure.cloud",
    "ap-southeast-2": "mypurecloud.com.au",
    "ap-northeast-1": "mypurecloud.jp",
    "ap-northeast-2": "apne2.pure.cloud",
    "ap-northeast-3": "apne3.pure.cloud",
}


def resolve_domain(region: str) -> str:
    value = region.strip().lower()
    for prefix in ("https://", "http://", "api.", "login.", "apps."):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.rstrip("/")
    if value in REGION_DOMAINS:
        return REGION_DOMAINS[value]
    if value in REGION_DOMAINS.values():
        return value
    valid = ", ".join(sorted(REGION_DOMAINS))
    raise ValueError(f"Región desconocida {region!r}. Usá uno de: {valid} (o el dominio, p.ej. mypurecloud.com).")
