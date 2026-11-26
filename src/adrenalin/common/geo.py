import pycountry


def region_to_geo_id(region: str) -> int | None:
    """Map an ISO 3166-1 alpha-2 code to a Google geo id; 'anywhere' means no filter."""
    if region.lower() == "anywhere":
        return None
    country = pycountry.countries.get(alpha_2=region.upper())
    if country is None:
        raise ValueError(f"Unknown region: {region}")
    return 2000 + int(country.numeric)


def geo_id_to_region(geo_id: int) -> str | None:
    """The ISO 3166-1 alpha-2 code of a Google country geo ID (2000 + ISO numeric), or None for any other ID."""
    if not 2000 < geo_id < 3000:
        return None
    country = pycountry.countries.get(numeric=f"{geo_id - 2000:03d}")
    return country.alpha_2 if country else None


def region_label(region: str) -> str:
    """`anywhere`, or the upper-case ISO code: a Region's name in run folders and CSV rows."""
    return "anywhere" if region.lower() == "anywhere" else region.upper()
