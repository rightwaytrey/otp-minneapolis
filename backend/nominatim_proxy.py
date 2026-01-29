#!/usr/bin/env python3
"""
Nominatim to Pelias Proxy for OTP Minneapolis
Proxies geocoding requests to Nominatim and translates responses to Pelias GeoJSON format.
"""

import time
import httpx
from typing import Optional
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI(title="Nominatim-to-Pelias Proxy")

# CORS middleware to allow requests from OTP frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting: 1 second between requests to Nominatim
last_request_time = 0
MIN_REQUEST_INTERVAL = 1.0

# Minneapolis metro viewbox for biasing results
MINNEAPOLIS_VIEWBOX = "-93.8,45.2,-92.8,44.7"  # west,north,east,south

# Nominatim base URL
NOMINATIM_URL = "https://nominatim.openstreetmap.org"

# User-Agent required by Nominatim usage policy
USER_AGENT = "OTP-Minneapolis/1.0 (OpenTripPlanner deployment)"


def rate_limit():
    """Enforce rate limiting: minimum 1 second between requests."""
    global last_request_time
    now = time.time()
    elapsed = now - last_request_time
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    last_request_time = time.time()


def nominatim_to_pelias(nominatim_result: dict) -> dict:
    """
    Convert a single Nominatim result to Pelias GeoJSON Feature format.

    Nominatim structure:
    {
        "place_id": 123456,
        "lat": "44.978",
        "lon": "-93.265",
        "display_name": "123 Main Street, Minneapolis, MN 55401, USA",
        "address": {
            "house_number": "123",
            "road": "Main Street",
            "city": "Minneapolis",
            "state": "Minnesota",
            "postcode": "55401",
            "country": "United States"
        }
    }
    """
    address = nominatim_result.get("address", {})
    lat = float(nominatim_result.get("lat", 0))
    lon = float(nominatim_result.get("lon", 0))
    place_id = nominatim_result.get("place_id", "")

    # Determine layer based on OSM type
    osm_type = nominatim_result.get("type", "")
    osm_class = nominatim_result.get("class", "")

    # Map OSM types to Pelias layers
    if address.get("house_number"):
        layer = "address"
    elif osm_class == "highway":
        layer = "street"
    elif osm_type in ["city", "town", "village", "hamlet"]:
        layer = "locality"
    elif osm_type == "administrative":
        layer = "region"
    else:
        layer = "venue"

    # Build name from address components
    name_parts = []
    if address.get("house_number"):
        name_parts.append(address["house_number"])
    if address.get("road"):
        name_parts.append(address["road"])

    name = " ".join(name_parts) if name_parts else nominatim_result.get("display_name", "")

    # Build Pelias properties
    properties = {
        "id": f"nominatim:{place_id}",
        "gid": f"nominatim:{layer}:{place_id}",
        "layer": layer,
        "source": "nominatim",
        "name": name,
        "label": nominatim_result.get("display_name", ""),
        "confidence": nominatim_result.get("importance", 0.5),
    }

    # Add address components if available
    if address.get("house_number"):
        properties["housenumber"] = address["house_number"]
    if address.get("road"):
        properties["street"] = address["road"]
    if address.get("city") or address.get("town") or address.get("village"):
        properties["locality"] = address.get("city") or address.get("town") or address.get("village")
    if address.get("county"):
        properties["county"] = address["county"]
    if address.get("state"):
        properties["region"] = address["state"]
        # Add state abbreviation if possible
        properties["region_a"] = address.get("state_code", "")
    if address.get("postcode"):
        properties["postalcode"] = address["postcode"]
    if address.get("country"):
        properties["country"] = address["country"]
        properties["country_a"] = address.get("country_code", "").upper()

    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [lon, lat]
        },
        "properties": properties
    }


async def query_nominatim(endpoint: str, params: dict) -> dict:
    """Query Nominatim API with rate limiting and proper headers."""
    rate_limit()

    headers = {
        "User-Agent": USER_AGENT
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{NOMINATIM_URL}/{endpoint}",
            params=params,
            headers=headers
        )
        response.raise_for_status()
        return response.json()


@app.get("/v1/autocomplete")
async def autocomplete(
    text: str = Query(..., description="Search query text"),
    size: int = Query(10, description="Number of results to return"),
    focus_point_lat: Optional[float] = Query(None, alias="focus.point.lat"),
    focus_point_lon: Optional[float] = Query(None, alias="focus.point.lon"),
):
    """
    Pelias-compatible autocomplete endpoint.
    Proxies to Nominatim search with viewbox biasing for Minneapolis.
    """
    params = {
        "q": text,
        "format": "json",
        "addressdetails": 1,
        "limit": size,
        "countrycodes": "us",
        "viewbox": MINNEAPOLIS_VIEWBOX,
        "bounded": 0,  # Prefer viewbox but don't restrict
    }

    try:
        nominatim_results = await query_nominatim("search", params)

        # Convert to Pelias FeatureCollection format
        features = [nominatim_to_pelias(result) for result in nominatim_results]

        return {
            "type": "FeatureCollection",
            "features": features,
            "bbox": None,
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.get("/v1/search")
async def search(
    text: str = Query(..., description="Search query text"),
    size: int = Query(10, description="Number of results to return"),
    focus_point_lat: Optional[float] = Query(None, alias="focus.point.lat"),
    focus_point_lon: Optional[float] = Query(None, alias="focus.point.lon"),
):
    """
    Pelias-compatible search endpoint.
    Proxies to Nominatim search with viewbox biasing for Minneapolis.
    """
    # Same implementation as autocomplete for Nominatim
    return await autocomplete(text, size, focus_point_lat, focus_point_lon)


@app.get("/v1/reverse")
async def reverse(
    point_lat: float = Query(..., alias="point.lat", description="Latitude"),
    point_lon: float = Query(..., alias="point.lon", description="Longitude"),
    size: int = Query(1, description="Number of results to return"),
):
    """
    Pelias-compatible reverse geocoding endpoint.
    Proxies to Nominatim reverse geocoding.
    """
    params = {
        "lat": point_lat,
        "lon": point_lon,
        "format": "json",
        "addressdetails": 1,
        "zoom": 18,  # Building/address level
    }

    try:
        nominatim_result = await query_nominatim("reverse", params)

        # Nominatim reverse returns a single object, not an array
        if nominatim_result and "place_id" in nominatim_result:
            feature = nominatim_to_pelias(nominatim_result)
            features = [feature]
        else:
            features = []

        return {
            "type": "FeatureCollection",
            "features": features,
            "bbox": None,
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "nominatim-proxy"}


# Aliases without /v1/ prefix for compatibility with Pelias API
@app.get("/autocomplete")
async def autocomplete_alias(
    text: str = Query(..., description="Search query text"),
    size: int = Query(10, description="Number of results to return"),
    focus_point_lat: Optional[float] = Query(None, alias="focus.point.lat"),
    focus_point_lon: Optional[float] = Query(None, alias="focus.point.lon"),
):
    """Alias for /v1/autocomplete (Pelias API compatibility)."""
    return await autocomplete(text, size, focus_point_lat, focus_point_lon)


@app.get("/search")
async def search_alias(
    text: str = Query(..., description="Search query text"),
    size: int = Query(10, description="Number of results to return"),
    focus_point_lat: Optional[float] = Query(None, alias="focus.point.lat"),
    focus_point_lon: Optional[float] = Query(None, alias="focus.point.lon"),
):
    """Alias for /v1/search (Pelias API compatibility)."""
    return await search(text, size, focus_point_lat, focus_point_lon)


@app.get("/reverse")
async def reverse_alias(
    point_lat: float = Query(..., alias="point.lat", description="Latitude"),
    point_lon: float = Query(..., alias="point.lon", description="Longitude"),
    size: int = Query(1, description="Number of results to return"),
):
    """Alias for /v1/reverse (Pelias API compatibility)."""
    return await reverse(point_lat, point_lon, size)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
