import pycountry


def region_to_geo_id(region: str) -> int | None:
    """Map an ISO 3166-1 alpha-2 code to a Google geo id; 'anywhere' means no filter."""
    if region.lower() == "anywhere":
        return None
    country = pycountry.countries.get(alpha_2=region.upper())
    if country is None:
        raise ValueError(f"Unknown region: {region}")
    return 2000 + int(country.numeric)
