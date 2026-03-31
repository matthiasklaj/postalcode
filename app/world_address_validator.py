#!/usr/bin/env python3
"""
world_address_validator.py

A practical "country first, then fallback" address validator for worldwide use.

What this does:
- asks for a country code first
- uses a country-specific public/official adapter where one is implemented
- falls back to Google Address Validation for all other countries
- returns a normalized verdict and a confidence score

Implemented built-in country adapters:
- KR: Juso (official Korean road-name address service)
- FR: Géoplateforme / BAN geocoding service
- CH: Swiss geo.admin SearchServer
- FI: National Land Survey of Finland geocoding service
- US: USPS modern API slot included, but left as a provider hook because USPS auth/catalog details
      can vary by account; use Google fallback unless you wire your USPS credentials and request shape.

Everything else:
- falls back to Google Address Validation when GOOGLE_MAPS_API_KEY is set

Install:
    pip install requests python-dotenv

Create a .env file:
    GOOGLE_MAPS_API_KEY=...
    JUSO_CONFM_KEY=...

Optional:
    # if you later wire your own USPS account details
    USPS_CLIENT_ID=...
    USPS_CLIENT_SECRET=...

Run:
    python world_address_validator.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


USER_AGENT = "world-address-validator/2.0"


# ----------------------------
# Data model
# ----------------------------

@dataclass
class ValidationResult:
    provider: str
    country: str
    input_address: str
    verdict: str  # VALID / LIKELY_VALID / PARTIAL / NOT_FOUND / ERROR
    confidence: float
    normalized_address: Optional[str] = None
    postal_code: Optional[str] = None
    components: Optional[Dict[str, Any]] = None
    coordinates: Optional[Dict[str, float]] = None
    explanation: Optional[str] = None
    raw_match_count: Optional[int] = None


# ----------------------------
# Helpers
# ----------------------------

def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def clean_text(s: str) -> str:
    s = s or ""
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s


def normalize_for_compare(s: str) -> str:
    s = clean_text(s).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def token_overlap(a: str, b: str) -> float:
    ta = set(normalize_for_compare(a).split())
    tb = set(normalize_for_compare(b).split())
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def is_exactish(a: str, b: str) -> bool:
    return normalize_for_compare(a) == normalize_for_compare(b)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def pick_best_candidate(
    query: str,
    candidates: List[Tuple[str, Dict[str, Any]]]
) -> Tuple[Optional[Dict[str, Any]], float]:
    best_obj = None
    best_score = -1.0
    for text, payload in candidates:
        score = token_overlap(query, text)
        if score > best_score:
            best_score = score
            best_obj = payload
    return best_obj, max(best_score, 0.0)


def verdict_from_similarity(sim: float, has_postal: bool = False, exact: bool = False) -> Tuple[str, float]:
    if exact or sim >= 0.96:
        return "VALID", 0.98 if has_postal else 0.95
    if sim >= 0.78:
        return "LIKELY_VALID", 0.88 if has_postal else 0.84
    if sim >= 0.45:
        return "PARTIAL", 0.62
    return "NOT_FOUND", 0.10


# ----------------------------
# Google fallback
# ----------------------------

class GoogleAddressValidator:
    ENDPOINT = "https://addressvalidation.googleapis.com/v1:validateAddress"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.http = session()

    def validate(self, address: str, region_code: Optional[str] = None) -> ValidationResult:
        body: Dict[str, Any] = {"address": {"addressLines": [address]}}
        if region_code:
            body["address"]["regionCode"] = region_code.upper()

        resp = self.http.post(
            f"{self.ENDPOINT}?key={self.api_key}",
            json=body,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()

        result = data.get("result", {})
        verdict_data = result.get("verdict", {})
        address_out = result.get("address", {})
        geocode = result.get("geocode", {})

        formatted = address_out.get("formattedAddress")
        postal_code = None
        comps: Dict[str, Any] = {}

        for comp in address_out.get("addressComponents", []):
            ctype = comp.get("componentType")
            cname = comp.get("componentName", {}).get("text")
            if ctype and cname:
                comps[ctype] = cname
            if ctype == "postal_code":
                postal_code = cname

        coords = None
        loc = geocode.get("location", {})
        if "latitude" in loc and "longitude" in loc:
            coords = {"lat": loc["latitude"], "lng": loc["longitude"]}

        address_complete = bool(verdict_data.get("addressComplete"))
        has_unconfirmed = bool(verdict_data.get("hasUnconfirmedComponents"))
        has_inferred = bool(verdict_data.get("hasInferredComponents"))
        has_replaced = bool(verdict_data.get("hasReplacedComponents"))

        if formatted and address_complete and not has_unconfirmed:
            verdict = "VALID"
            confidence = 0.95
        elif formatted and not has_unconfirmed:
            verdict = "LIKELY_VALID"
            confidence = 0.84
        elif formatted:
            verdict = "PARTIAL"
            confidence = 0.60
        else:
            verdict = "NOT_FOUND"
            confidence = 0.10

        return ValidationResult(
            provider="google",
            country=(region_code or "").upper(),
            input_address=address,
            verdict=verdict,
            confidence=confidence,
            normalized_address=formatted,
            postal_code=postal_code,
            components=comps or None,
            coordinates=coords,
            explanation=(
                f"addressComplete={address_complete}, "
                f"hasUnconfirmedComponents={has_unconfirmed}, "
                f"hasInferredComponents={has_inferred}, "
                f"hasReplacedComponents={has_replaced}"
            ),
            raw_match_count=1 if formatted else 0,
        )


# ----------------------------
# KR - Juso
# ----------------------------

class KoreaJusoValidator:
    ENDPOINT = "https://www.juso.go.kr/addrlink/addrLinkApi.do"

    def __init__(self, confm_key: str):
        self.confm_key = confm_key
        self.http = session()

    def validate(self, address: str) -> ValidationResult:
        params = {
            "confmKey": self.confm_key,
            "currentPage": 1,
            "countPerPage": 10,
            "keyword": address,
            "resultType": "json",
        }
        resp = self.http.get(self.ENDPOINT, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", {})
        common = results.get("common", {})
        items = results.get("juso", []) or []

        if not items:
            return ValidationResult(
                provider="juso",
                country="KR",
                input_address=address,
                verdict="NOT_FOUND",
                confidence=0.05,
                explanation=common.get("errorMessage") or "No Juso match returned.",
                raw_match_count=0,
            )

        candidates = []
        for item in items:
            display = " | ".join(filter(None, [
                item.get("roadAddr"),
                item.get("engAddr"),
                item.get("jibunAddr"),
                item.get("zipNo"),
            ]))
            candidates.append((display, item))

        best, sim = pick_best_candidate(address, candidates)
        assert best is not None

        normalized = best.get("roadAddr") or best.get("engAddr") or best.get("jibunAddr")
        postal_code = best.get("zipNo")
        verdict, confidence = verdict_from_similarity(
            sim,
            has_postal=bool(postal_code),
            exact=is_exactish(address, normalized or "")
        )

        return ValidationResult(
            provider="juso",
            country="KR",
            input_address=address,
            verdict=verdict,
            confidence=confidence,
            normalized_address=normalized,
            postal_code=postal_code,
            components={
                "roadAddr": best.get("roadAddr"),
                "jibunAddr": best.get("jibunAddr"),
                "englishAddress": best.get("engAddr"),
                "siNm": best.get("siNm"),
                "sggNm": best.get("sggNm"),
                "emdNm": best.get("emdNm"),
                "roadNm": best.get("rn"),
                "buildingNoMain": best.get("buldMnnm"),
                "buildingNoSub": best.get("buldSlno"),
            },
            explanation=f"Best Juso match out of {len(items)} candidate(s); similarity={sim:.3f}",
            raw_match_count=len(items),
        )


# ----------------------------
# FR - Géoplateforme / BAN
# ----------------------------

class FranceGeoplateformeValidator:
    ENDPOINT = "https://data.geopf.fr/geocodage/search"

    def __init__(self):
        self.http = session()

    def validate(self, address: str) -> ValidationResult:
        params = {
            "q": address,
            "index": "address",
            "limit": 5,
        }
        resp = self.http.get(self.ENDPOINT, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        features = data.get("features", []) or []
        if not features:
            return ValidationResult(
                provider="geoplateforme",
                country="FR",
                input_address=address,
                verdict="NOT_FOUND",
                confidence=0.05,
                explanation="No official French address result returned.",
                raw_match_count=0,
            )

        candidates = []
        for feat in features:
            props = feat.get("properties", {})
            label = props.get("label", "")
            candidates.append((label, feat))

        best, sim = pick_best_candidate(address, candidates)
        assert best is not None

        props = best.get("properties", {})
        geom = best.get("geometry", {})
        normalized = props.get("label")
        postal_code = props.get("postcode")
        coords = None
        if geom.get("type") == "Point":
            lon, lat = geom.get("coordinates", [None, None])
            if lat is not None and lon is not None:
                coords = {"lat": lat, "lng": lon}

        verdict, confidence = verdict_from_similarity(
            sim,
            has_postal=bool(postal_code),
            exact=is_exactish(address, normalized or "")
        )

        return ValidationResult(
            provider="geoplateforme",
            country="FR",
            input_address=address,
            verdict=verdict,
            confidence=confidence,
            normalized_address=normalized,
            postal_code=postal_code,
            components={
                "city": props.get("city"),
                "street": props.get("street"),
                "housenumber": props.get("housenumber"),
                "context": props.get("context"),
                "id": props.get("id"),
            },
            coordinates=coords,
            explanation=f"Best French official match out of {len(features)} candidate(s); similarity={sim:.3f}",
            raw_match_count=len(features),
        )


# ----------------------------
# CH - Swiss geo.admin
# ----------------------------

class SwitzerlandGeoAdminValidator:
    ENDPOINT = "https://api3.geo.admin.ch/rest/services/ech/SearchServer"

    def __init__(self):
        self.http = session()

    def validate(self, address: str) -> ValidationResult:
        params = {
            "searchText": address,
            "type": "locations",
            "origins": "address",
            "limit": 10,
        }
        resp = self.http.get(self.ENDPOINT, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", []) or []
        if not results:
            return ValidationResult(
                provider="geo.admin",
                country="CH",
                input_address=address,
                verdict="NOT_FOUND",
                confidence=0.05,
                explanation="No Swiss address result returned.",
                raw_match_count=0,
            )

        candidates = []
        for item in results:
            attrs = item.get("attrs", {})
            label = re.sub(r"<[^>]+>", "", attrs.get("label", "") or "")
            detail = attrs.get("detail", "")
            display = " | ".join(filter(None, [label, detail]))
            candidates.append((display, item))

        best, sim = pick_best_candidate(address, candidates)
        assert best is not None

        attrs = best.get("attrs", {})
        label = re.sub(r"<[^>]+>", "", attrs.get("label", "") or "")
        detail = attrs.get("detail", "")
        normalized = clean_text(" ".join(filter(None, [label, detail]))) or label or detail
        postal_code = None
        m = re.search(r"\b(\d{4})\b", detail)
        if m:
            postal_code = m.group(1)

        coords = None
        if attrs.get("lat") is not None and attrs.get("lon") is not None:
            coords = {"lat": float(attrs["lat"]), "lng": float(attrs["lon"])}

        verdict, confidence = verdict_from_similarity(
            sim,
            has_postal=bool(postal_code),
            exact=is_exactish(address, normalized or "")
        )

        return ValidationResult(
            provider="geo.admin",
            country="CH",
            input_address=address,
            verdict=verdict,
            confidence=confidence,
            normalized_address=normalized,
            postal_code=postal_code,
            components={
                "detail": detail,
                "featureId": attrs.get("featureId"),
                "origin": attrs.get("origin"),
            },
            coordinates=coords,
            explanation=f"Best Swiss match out of {len(results)} candidate(s); similarity={sim:.3f}",
            raw_match_count=len(results),
        )


# ----------------------------
# FI - National Land Survey of Finland
# ----------------------------

class FinlandNLSValidator:
    ENDPOINT = "https://avoin-paikkatieto.maanmittauslaitos.fi/geocoding/v1/pelias/search"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or env("FINLAND_NLS_API_KEY")
        self.http = session()

    def validate(self, address: str) -> ValidationResult:
        params = {
            "text": address,
            "sources": "addresses",
            "lang": "fi",
        }
        headers = {}
        # The service examples note API-key usage; if the instance allows anonymous usage in your environment,
        # this header can be omitted. Leave it configurable.
        if self.api_key:
            headers["api-key"] = self.api_key

        resp = self.http.get(self.ENDPOINT, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        features = data.get("features", []) or []
        if not features:
            return ValidationResult(
                provider="maanmittauslaitos",
                country="FI",
                input_address=address,
                verdict="NOT_FOUND",
                confidence=0.05,
                explanation="No Finnish address result returned.",
                raw_match_count=0,
            )

        candidates = []
        for feat in features:
            props = feat.get("properties", {})
            label = props.get("label", "") or props.get("name", "")
            candidates.append((label, feat))

        best, sim = pick_best_candidate(address, candidates)
        assert best is not None

        props = best.get("properties", {})
        normalized = props.get("label") or props.get("name")
        postal_code = props.get("postalcode")
        coords = None
        geom = best.get("geometry", {})
        if geom.get("type") == "Point":
            lon, lat = geom.get("coordinates", [None, None])
            if lat is not None and lon is not None:
                coords = {"lat": lat, "lng": lon}

        verdict, confidence = verdict_from_similarity(
            sim,
            has_postal=bool(postal_code),
            exact=is_exactish(address, normalized or "")
        )

        return ValidationResult(
            provider="maanmittauslaitos",
            country="FI",
            input_address=address,
            verdict=verdict,
            confidence=confidence,
            normalized_address=normalized,
            postal_code=postal_code,
            components={
                "locality": props.get("locality"),
                "street": props.get("street"),
                "gid": props.get("gid"),
                "source": props.get("source"),
            },
            coordinates=coords,
            explanation=f"Best Finnish match out of {len(features)} candidate(s); similarity={sim:.3f}",
            raw_match_count=len(features),
        )


# ----------------------------
# USPS hook (optional)
# ----------------------------

class USPSValidator:
    """
    Placeholder hook for the modern USPS API platform.

    USPS moved off legacy Web Tools. The exact auth flow / endpoint shape can vary by account, catalog,
    and the spec you are using in the USPS developer portal. For most users, keeping Google fallback for US
    is the fastest path unless you already have USPS auth working internally.

    To use USPS directly, replace validate() with your account's exact endpoint, OAuth flow, and response mapping.
    """
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    def validate(self, address: str) -> ValidationResult:
        return ValidationResult(
            provider="usps",
            country="US",
            input_address=address,
            verdict="ERROR",
            confidence=0.0,
            explanation=(
                "USPS modern API hook is present but not wired in this template because USPS catalog/auth details "
                "must match your account. Use Google fallback or replace this adapter with your exact USPS spec."
            ),
        )


# ----------------------------
# Router
# ----------------------------

class AddressValidatorRouter:
    """
    Built-in strategy:
    1. Country-specific public/official adapter when available and configured
    2. Google Address Validation fallback for all countries
    """

    def __init__(self):
        self.google_api_key = env("GOOGLE_MAPS_API_KEY")
        self.juso_key = env("JUSO_CONFM_KEY")
        self.usps_client_id = env("USPS_CLIENT_ID")
        self.usps_client_secret = env("USPS_CLIENT_SECRET")
        self.finland_api_key = env("FINLAND_NLS_API_KEY")

        # Country codes where we know we will use the fallback if no local adapter is active.
        self.fallback_countries = "ALL"

    def validate(self, country: str, address: str) -> ValidationResult:
        country = clean_text(country).upper()
        address = clean_text(address)

        if not country or not address:
            return ValidationResult(
                provider="router",
                country=country,
                input_address=address,
                verdict="ERROR",
                confidence=0.0,
                explanation="Country code and address are required.",
            )

        try:
            # Country-specific adapters first
            if country == "KR" and self.juso_key:
                return KoreaJusoValidator(self.juso_key).validate(address)

            if country == "FR":
                return FranceGeoplateformeValidator().validate(address)

            if country == "CH":
                return SwitzerlandGeoAdminValidator().validate(address)

            if country == "FI":
                return FinlandNLSValidator(self.finland_api_key).validate(address)

            if country == "US" and self.usps_client_id and self.usps_client_secret:
                # only if the user has fully wired their modern USPS adapter
                return USPSValidator(self.usps_client_id, self.usps_client_secret).validate(address)

            # Global fallback for everything else
            if self.google_api_key:
                return GoogleAddressValidator(self.google_api_key).validate(address, region_code=country)

            return ValidationResult(
                provider="router",
                country=country,
                input_address=address,
                verdict="ERROR",
                confidence=0.0,
                explanation=(
                    "No country adapter matched and GOOGLE_MAPS_API_KEY is missing. "
                    "Add Google fallback or configure a local adapter."
                ),
            )

        except requests.HTTPError as e:
            body = ""
            try:
                body = e.response.text[:1000]
            except Exception:
                pass
            return ValidationResult(
                provider="router",
                country=country,
                input_address=address,
                verdict="ERROR",
                confidence=0.0,
                explanation=f"HTTP error: {e}. Response excerpt: {body}",
            )
        except Exception as e:
            return ValidationResult(
                provider="router",
                country=country,
                input_address=address,
                verdict="ERROR",
                confidence=0.0,
                explanation=f"Unexpected error: {type(e).__name__}: {e}",
            )


# ----------------------------
# CLI
# ----------------------------

def pretty_result(res: ValidationResult) -> str:
    return json.dumps(asdict(res), ensure_ascii=False, indent=2)


def main() -> int:
    print("Worldwide address validator")
    print("Examples: KR, FR, CH, FI, US, DE, JP, BR")
    country = input("Country code: ").strip().upper()
    address = input("Address: ").strip()

    router = AddressValidatorRouter()
    result = router.validate(country, address)

    print("\n=== RESULT ===")
    print(pretty_result(result))

    if result.verdict in {"VALID", "LIKELY_VALID", "PARTIAL"}:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
