"""Data residency policy.

Environment:
    EVAL_DATA_RESIDENCY   CA (default) | ANY
    EVAL_DATA_REGION      where this deployment stores data, e.g. ca-central-1.
                          "local" is accepted only when EVAL_ENV != production.
    EVAL_ENV              development (default) | production

Two checks:
1. Storage: with residency CA the server refuses to start unless the
   storage region is Canadian.
2. Outbound: personal data leaving through a CRM destination is the same
   data, so each destination declares its hosting region. With residency CA
   only Canadian destinations receive leads, unless the destination is
   marked allowCrossBorder AND the template discloses cross-border storage
   to the person in its privacy notice (compliance.crossBorderDisclosure).
"""
import os
from dataclasses import dataclass

CANADIAN_REGIONS = {
    "ca-central-1", "ca-west-1",                    # AWS Montreal, Calgary
    "canadacentral", "canadaeast",                  # Azure Toronto, Quebec City
    "northamerica-northeast1", "northamerica-northeast2",  # GCP Montreal, Toronto
    "ca",                                           # generic: provider confirms Canadian hosting
}


class ResidencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    residency: str = "CA"
    region: str = "local"
    env: str = "development"

    @classmethod
    def from_env(cls, environ=None):
        e = os.environ if environ is None else environ
        return cls(residency=e.get("EVAL_DATA_RESIDENCY", "CA").upper(),
                   region=e.get("EVAL_DATA_REGION", "local").lower(),
                   env=e.get("EVAL_ENV", "development").lower())

    @property
    def enforced(self):
        return self.residency == "CA"

    def check_storage(self):
        if not self.enforced:
            return
        if self.region in CANADIAN_REGIONS:
            return
        if self.region == "local" and self.env != "production":
            return
        raise ResidencyError(
            f"EVAL_DATA_RESIDENCY=CA but EVAL_DATA_REGION={self.region!r} is not a Canadian region "
            f"(allowed: {sorted(CANADIAN_REGIONS)})")

    def destination_allowed(self, destination_region, allow_cross_border, template):
        """Returns (allowed, reason)."""
        if not self.enforced:
            return True, "residency not enforced"
        if (destination_region or "").lower() in CANADIAN_REGIONS:
            return True, "destination hosted in Canada"
        disclosed = bool((template.get("compliance") or {}).get("crossBorderDisclosure"))
        if allow_cross_border and disclosed:
            return True, "cross-border transfer approved and disclosed to the person"
        if allow_cross_border:
            return False, "destination allows cross-border but the template has no crossBorderDisclosure"
        return False, f"destination region {destination_region!r} is outside Canada"

    def describe(self):
        return {"residency": self.residency, "storageRegion": self.region, "env": self.env,
                "canadianRegions": sorted(CANADIAN_REGIONS)}
