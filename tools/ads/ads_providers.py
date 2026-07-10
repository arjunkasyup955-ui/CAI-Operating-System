import os
from typing import Any, Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this
# tool module never needs the frozen Phase 0 kernel to change to gain a new config
# key - same convention as tools/n8n, tools/searxng, tools/crm.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class AdsHealthStatus(BaseModel):
    healthy: bool
    configured: bool
    provider_type: str = ""
    error: str = ""


class AdCampaign(BaseModel):
    campaign_id: str
    campaign_name: str = ""
    status: str = "active"
    budget: float = 0.0
    objective: str = "conversions"
    platform: str = ""


class AdGroup(BaseModel):
    ad_group_id: str
    ad_group_name: str = ""
    campaign_id: str = ""
    status: str = "active"


class Ad(BaseModel):
    ad_id: str
    ad_group_id: str = ""
    headline: str = ""
    body: str = ""
    status: str = "active"


class AdAnalytics(BaseModel):
    campaign_id: str
    impressions: int = 0
    clicks: int = 0
    spend: float = 0.0
    ctr: float = 0.0
    conversions: int = 0


class AudienceSegment(BaseModel):
    segment_id: str
    segment_name: str = ""
    estimated_size: int = 0


class KeywordSuggestion(BaseModel):
    keyword: str
    estimated_volume: int = 0
    competition: str = "low"


class AdPreview(BaseModel):
    ad_id: str = ""
    preview_text: str = ""
    preview_image_url: str = ""


class AdsOperationResult(BaseModel):
    success: bool
    operation: str
    campaign: AdCampaign | None = None
    campaigns: list[AdCampaign] = []
    ad_group: AdGroup | None = None
    ad: Ad | None = None
    analytics: AdAnalytics | None = None
    audiences: list[AudienceSegment] = []
    keywords: list[KeywordSuggestion] = []
    preview: AdPreview | None = None
    error: str = ""


class AdsProvider(Protocol):
    """Ad platform connectivity, abstracted across Google Ads/Meta Ads/LinkedIn
    Ads/X Ads behind one interface (which concrete backend the real provider
    targets is a config choice, not a code choice - see AdsRESTProvider). Every
    method either returns an AdsOperationResult/AdsHealthStatus on success or
    raises on failure - the agent layer's try/except (with ToolRegistry's
    RetryPolicy) handles graceful degradation uniformly, the same pattern as every
    prior provider. Field names deliberately avoid "name" anywhere (ToolRegistry.
    invoke()'s own positional parameter is literally called `name` - a schema field
    called `name` collides with it; found and fixed in the Chroma component, Phase
    3 Component 3; avoided proactively here via "campaign_name"/"ad_group_name").
    """

    name: str

    def health_check(self) -> AdsHealthStatus: ...
    def create_campaign(self, campaign_name: str, budget: float = 0.0, objective: str = "conversions", platform: str = "") -> AdsOperationResult: ...
    def update_campaign(self, campaign_id: str, campaign_name: str | None = None, budget: float | None = None, objective: str | None = None) -> AdsOperationResult: ...
    def pause_campaign(self, campaign_id: str) -> AdsOperationResult: ...
    def resume_campaign(self, campaign_id: str) -> AdsOperationResult: ...
    def delete_campaign(self, campaign_id: str) -> AdsOperationResult: ...
    def get_campaign(self, campaign_id: str) -> AdsOperationResult: ...
    def list_campaigns(self) -> AdsOperationResult: ...
    def campaign_analytics(self, campaign_id: str) -> AdsOperationResult: ...
    def budget_update(self, campaign_id: str, budget: float) -> AdsOperationResult: ...
    def audience_lookup(self, query: str, max_results: int = 10) -> AdsOperationResult: ...
    def keyword_suggestions(self, seed_keyword: str, max_results: int = 10) -> AdsOperationResult: ...
    def ad_preview(self, ad_id: str) -> AdsOperationResult: ...
    def create_ad_group(self, campaign_id: str, ad_group_name: str) -> AdsOperationResult: ...
    def create_ad(self, ad_group_id: str, headline: str, body: str) -> AdsOperationResult: ...


# Per-vendor resource paths - a deliberately terse mapping (not a full field-level
# schema translation layer for each vendor's actual payload shape, and not
# accounting for the account-ID-scoped URL segments most of these real APIs
# require) since none of these 4 accounts exist in this sandbox to verify
# field-level fidelity against. Illustrative of the architecture (one interface,
# config-selected backend), not a production-ready client for any of the 4.
_PROVIDER_CONFIGS: dict[str, dict[str, Any]] = {
    "google_ads": {
        "base_url": "https://googleads.googleapis.com",
        "paths": {"campaigns": "/v17/campaigns", "ad_groups": "/v17/adGroups", "ads": "/v17/ads", "audiences": "/v17/audiences", "keywords": "/v17/keywordPlanIdeas"},
    },
    "meta_ads": {
        "base_url": "https://graph.facebook.com",
        "paths": {"campaigns": "/v19.0/campaigns", "ad_groups": "/v19.0/adsets", "ads": "/v19.0/ads", "audiences": "/v19.0/audiences", "keywords": "/v19.0/keywordinsights"},
    },
    "linkedin_ads": {
        "base_url": "https://api.linkedin.com",
        "paths": {"campaigns": "/rest/adCampaigns", "ad_groups": "/rest/adCampaignGroups", "ads": "/rest/adCreatives", "audiences": "/rest/audienceCounts", "keywords": "/rest/keywordSuggestions"},
    },
    "x_ads": {
        "base_url": "https://ads-api.twitter.com",
        "paths": {"campaigns": "/12/campaigns", "ad_groups": "/12/line_items", "ads": "/12/promoted_tweets", "audiences": "/12/targeting_criteria", "keywords": "/12/keyword_insights"},
    },
}


class AdsRESTProvider:
    """Default AdsProvider - real integration over httpx (already an AFOS
    dependency), lazily initialized. Which of the 4 supported vendors it targets is
    a config choice (ADS_PROVIDER env var: google_ads/meta_ads/linkedin_ads/x_ads,
    default google_ads), not a code choice - adding a 5th platform means adding one
    entry to _PROVIDER_CONFIGS, not a new class. Endpoint paths follow each
    vendor's commonly documented REST shape and are unverified against any live ad
    account in this sandbox (no ADS_API_KEY configured for any of the 4) - adjust
    paths/auth if your account's API version differs. Every call gracefully
    surfaces the 6 named failure modes (missing key, auth failure, rate limiting,
    invalid campaign ID, network timeout, provider unavailable) rather than
    crashing.
    """

    name = "ads_rest"

    def __init__(self, provider_type: str | None = None) -> None:
        self._provider_type = (provider_type or _env("ADS_PROVIDER", "google_ads")).lower()
        self._api_key = _env("ADS_API_KEY")
        self._client = None

    def _paths(self) -> dict[str, str]:
        config = _PROVIDER_CONFIGS.get(self._provider_type)
        if config is None:
            raise RuntimeError(f"unsupported ads provider '{self._provider_type}' (expected one of {sorted(_PROVIDER_CONFIGS)})")
        return config["paths"]

    def _get_client(self):
        if not self._api_key:
            raise RuntimeError(f"ADS_API_KEY is not configured for provider '{self._provider_type}'")
        config = _PROVIDER_CONFIGS.get(self._provider_type)
        if config is None:
            raise RuntimeError(f"unsupported ads provider '{self._provider_type}' (expected one of {sorted(_PROVIDER_CONFIGS)})")
        if self._client is None:
            import httpx

            self._client = httpx.Client(base_url=config["base_url"], headers={"Authorization": f"Bearer {self._api_key}"}, timeout=20.0)
        return self._client

    def health_check(self) -> AdsHealthStatus:
        if not self._api_key:
            return AdsHealthStatus(healthy=False, configured=False, provider_type=self._provider_type, error=f"ADS_API_KEY is not configured for provider '{self._provider_type}'")
        try:
            self._request("GET", "/")
            return AdsHealthStatus(healthy=True, configured=True, provider_type=self._provider_type)
        except Exception as exc:
            return AdsHealthStatus(healthy=False, configured=True, provider_type=self._provider_type, error=str(exc))

    def _request(self, method: str, path: str, **kwargs: Any):
        import httpx

        client = self._get_client()
        try:
            response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise RuntimeError("network timeout while contacting ads provider") from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(f"ads provider '{self._provider_type}' is currently unavailable") from exc
        if response.status_code in (401, 403):
            raise RuntimeError("authentication failed - invalid or expired credentials")
        if response.status_code == 429:
            raise RuntimeError("rate limited by ads provider - too many requests")
        if response.status_code == 404:
            raise RuntimeError("campaign not found (invalid campaign ID)")
        response.raise_for_status()
        return response

    def create_campaign(self, campaign_name: str, budget: float = 0.0, objective: str = "conversions", platform: str = "") -> AdsOperationResult:
        response = self._request("POST", self._paths()["campaigns"], json={"campaign_name": campaign_name, "budget": budget, "objective": objective})
        campaign_id = str(response.json().get("id", ""))
        return AdsOperationResult(success=True, operation="create_campaign", campaign=AdCampaign(campaign_id=campaign_id, campaign_name=campaign_name, budget=budget, objective=objective, platform=platform or self._provider_type))

    def update_campaign(self, campaign_id: str, campaign_name: str | None = None, budget: float | None = None, objective: str | None = None) -> AdsOperationResult:
        body = {k: v for k, v in {"campaign_name": campaign_name, "budget": budget, "objective": objective}.items() if v is not None}
        self._request("PATCH", f"{self._paths()['campaigns']}/{campaign_id}", json=body)
        return AdsOperationResult(success=True, operation="update_campaign", campaign=AdCampaign(campaign_id=campaign_id, **body))

    def pause_campaign(self, campaign_id: str) -> AdsOperationResult:
        self._request("PATCH", f"{self._paths()['campaigns']}/{campaign_id}", json={"status": "paused"})
        return AdsOperationResult(success=True, operation="pause_campaign", campaign=AdCampaign(campaign_id=campaign_id, status="paused"))

    def resume_campaign(self, campaign_id: str) -> AdsOperationResult:
        self._request("PATCH", f"{self._paths()['campaigns']}/{campaign_id}", json={"status": "active"})
        return AdsOperationResult(success=True, operation="resume_campaign", campaign=AdCampaign(campaign_id=campaign_id, status="active"))

    def delete_campaign(self, campaign_id: str) -> AdsOperationResult:
        self._request("DELETE", f"{self._paths()['campaigns']}/{campaign_id}")
        return AdsOperationResult(success=True, operation="delete_campaign")

    def get_campaign(self, campaign_id: str) -> AdsOperationResult:
        response = self._request("GET", f"{self._paths()['campaigns']}/{campaign_id}")
        data = response.json()
        return AdsOperationResult(success=True, operation="get_campaign", campaign=AdCampaign(campaign_id=campaign_id, campaign_name=data.get("campaign_name", ""), status=data.get("status", "active"), budget=data.get("budget", 0.0), objective=data.get("objective", "conversions")))

    def list_campaigns(self) -> AdsOperationResult:
        response = self._request("GET", self._paths()["campaigns"])
        data = response.json()
        campaigns = [AdCampaign(campaign_id=str(c.get("id", "")), campaign_name=c.get("campaign_name", ""), status=c.get("status", "active"), budget=c.get("budget", 0.0)) for c in data.get("results", [])]
        return AdsOperationResult(success=True, operation="list_campaigns", campaigns=campaigns)

    def campaign_analytics(self, campaign_id: str) -> AdsOperationResult:
        response = self._request("GET", f"{self._paths()['campaigns']}/{campaign_id}/analytics")
        data = response.json()
        analytics = AdAnalytics(campaign_id=campaign_id, impressions=data.get("impressions", 0), clicks=data.get("clicks", 0), spend=data.get("spend", 0.0), ctr=data.get("ctr", 0.0), conversions=data.get("conversions", 0))
        return AdsOperationResult(success=True, operation="campaign_analytics", analytics=analytics)

    def budget_update(self, campaign_id: str, budget: float) -> AdsOperationResult:
        if budget <= 0:
            raise ValueError("budget must be positive")
        self._request("PATCH", f"{self._paths()['campaigns']}/{campaign_id}/budget", json={"budget": budget})
        return AdsOperationResult(success=True, operation="budget_update", campaign=AdCampaign(campaign_id=campaign_id, budget=budget))

    def audience_lookup(self, query: str, max_results: int = 10) -> AdsOperationResult:
        response = self._request("GET", self._paths()["audiences"], params={"q": query, "limit": max_results})
        data = response.json()
        audiences = [AudienceSegment(segment_id=str(a.get("id", "")), segment_name=a.get("segment_name", ""), estimated_size=a.get("estimated_size", 0)) for a in data.get("results", [])[:max_results]]
        return AdsOperationResult(success=True, operation="audience_lookup", audiences=audiences)

    def keyword_suggestions(self, seed_keyword: str, max_results: int = 10) -> AdsOperationResult:
        response = self._request("GET", self._paths()["keywords"], params={"seed": seed_keyword, "limit": max_results})
        data = response.json()
        keywords = [KeywordSuggestion(keyword=k.get("keyword", ""), estimated_volume=k.get("estimated_volume", 0), competition=k.get("competition", "low")) for k in data.get("results", [])[:max_results]]
        return AdsOperationResult(success=True, operation="keyword_suggestions", keywords=keywords)

    def ad_preview(self, ad_id: str) -> AdsOperationResult:
        response = self._request("GET", f"{self._paths()['ads']}/{ad_id}/preview")
        data = response.json()
        preview = AdPreview(ad_id=ad_id, preview_text=data.get("preview_text", ""), preview_image_url=data.get("preview_image_url", ""))
        return AdsOperationResult(success=True, operation="ad_preview", preview=preview)

    def create_ad_group(self, campaign_id: str, ad_group_name: str) -> AdsOperationResult:
        response = self._request("POST", self._paths()["ad_groups"], json={"campaign_id": campaign_id, "ad_group_name": ad_group_name})
        ad_group_id = str(response.json().get("id", ""))
        return AdsOperationResult(success=True, operation="create_ad_group", ad_group=AdGroup(ad_group_id=ad_group_id, ad_group_name=ad_group_name, campaign_id=campaign_id))

    def create_ad(self, ad_group_id: str, headline: str, body: str) -> AdsOperationResult:
        response = self._request("POST", self._paths()["ads"], json={"ad_group_id": ad_group_id, "headline": headline, "body": body})
        ad_id = str(response.json().get("id", ""))
        return AdsOperationResult(success=True, operation="create_ad", ad=Ad(ad_id=ad_id, ad_group_id=ad_group_id, headline=headline, body=body))


class FakeAdsProvider:
    """In-memory AdsProvider - deterministic, no real ad account needed. Genuine
    CRUD/state (not canned responses): unknown campaign/ad-group/ad IDs raise,
    pausing an already-paused campaign (or resuming an already-active one) raises,
    mirroring real ad-platform semantics. Also supports an optional `fault`
    constructor flag ("auth_failure" | "rate_limited" | "network_timeout" |
    "provider_unavailable") that makes every operation raise the corresponding
    error - a deterministic, testable substitute for the 4 real-world failure modes
    that only genuinely occur at the network/vendor layer (the other 2 named
    scenarios - missing API key and invalid campaign ID - are demonstrated
    directly: the former via the real provider's network-free config check, the
    latter via this class's own CRUD gating).
    """

    name = "fake_ads"

    _FAULTS = {
        "auth_failure": "authentication failed - invalid or expired credentials",
        "rate_limited": "rate limited by ads provider - too many requests",
        "network_timeout": "network timeout while contacting ads provider",
        "provider_unavailable": "ads provider is currently unavailable",
    }

    _AUDIENCE_CATALOG = [
        {"segment_id": "aud-1", "segment_name": "Tech-savvy founders", "estimated_size": 250000},
        {"segment_id": "aud-2", "segment_name": "Small business owners", "estimated_size": 1200000},
        {"segment_id": "aud-3", "segment_name": "Marketing managers", "estimated_size": 480000},
    ]

    def __init__(self, fault: str | None = None) -> None:
        self._fault = fault
        self._campaigns: dict[str, dict[str, Any]] = {}
        self._ad_groups: dict[str, dict[str, Any]] = {}
        self._ads: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def _check_fault(self) -> None:
        if self._fault in self._FAULTS:
            raise RuntimeError(self._FAULTS[self._fault])

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def health_check(self) -> AdsHealthStatus:
        self._check_fault()
        return AdsHealthStatus(healthy=True, configured=True, provider_type="fake")

    def create_campaign(self, campaign_name: str, budget: float = 0.0, objective: str = "conversions", platform: str = "") -> AdsOperationResult:
        self._check_fault()
        campaign_id = self._next_id("campaign")
        self._campaigns[campaign_id] = {"campaign_id": campaign_id, "campaign_name": campaign_name, "status": "active", "budget": budget, "objective": objective, "platform": platform or "fake"}
        return AdsOperationResult(success=True, operation="create_campaign", campaign=AdCampaign(**self._campaigns[campaign_id]))

    def _get_campaign_raw(self, campaign_id: str) -> dict[str, Any]:
        if campaign_id not in self._campaigns:
            raise ValueError(f"campaign '{campaign_id}' not found")
        return self._campaigns[campaign_id]

    def get_campaign(self, campaign_id: str) -> AdsOperationResult:
        self._check_fault()
        return AdsOperationResult(success=True, operation="get_campaign", campaign=AdCampaign(**self._get_campaign_raw(campaign_id)))

    def update_campaign(self, campaign_id: str, campaign_name: str | None = None, budget: float | None = None, objective: str | None = None) -> AdsOperationResult:
        self._check_fault()
        campaign = self._get_campaign_raw(campaign_id)
        for field, value in {"campaign_name": campaign_name, "budget": budget, "objective": objective}.items():
            if value is not None:
                campaign[field] = value
        return AdsOperationResult(success=True, operation="update_campaign", campaign=AdCampaign(**campaign))

    def pause_campaign(self, campaign_id: str) -> AdsOperationResult:
        self._check_fault()
        campaign = self._get_campaign_raw(campaign_id)
        if campaign["status"] == "paused":
            raise RuntimeError(f"campaign '{campaign_id}' is already paused")
        campaign["status"] = "paused"
        return AdsOperationResult(success=True, operation="pause_campaign", campaign=AdCampaign(**campaign))

    def resume_campaign(self, campaign_id: str) -> AdsOperationResult:
        self._check_fault()
        campaign = self._get_campaign_raw(campaign_id)
        if campaign["status"] == "active":
            raise RuntimeError(f"campaign '{campaign_id}' is already active")
        campaign["status"] = "active"
        return AdsOperationResult(success=True, operation="resume_campaign", campaign=AdCampaign(**campaign))

    def delete_campaign(self, campaign_id: str) -> AdsOperationResult:
        self._check_fault()
        self._get_campaign_raw(campaign_id)
        del self._campaigns[campaign_id]
        return AdsOperationResult(success=True, operation="delete_campaign")

    def list_campaigns(self) -> AdsOperationResult:
        self._check_fault()
        return AdsOperationResult(success=True, operation="list_campaigns", campaigns=[AdCampaign(**c) for c in self._campaigns.values()])

    def campaign_analytics(self, campaign_id: str) -> AdsOperationResult:
        self._check_fault()
        self._get_campaign_raw(campaign_id)
        seed = sum(ord(c) for c in campaign_id)
        impressions = 1000 + seed * 10
        clicks = 50 + seed
        spend = round(25.0 + seed * 0.5, 2)
        conversions = 5 + (seed % 10)
        ctr = round(clicks / impressions, 4)
        return AdsOperationResult(success=True, operation="campaign_analytics", analytics=AdAnalytics(campaign_id=campaign_id, impressions=impressions, clicks=clicks, spend=spend, ctr=ctr, conversions=conversions))

    def budget_update(self, campaign_id: str, budget: float) -> AdsOperationResult:
        self._check_fault()
        if budget <= 0:
            raise ValueError("budget must be positive")
        campaign = self._get_campaign_raw(campaign_id)
        campaign["budget"] = budget
        return AdsOperationResult(success=True, operation="budget_update", campaign=AdCampaign(**campaign))

    def audience_lookup(self, query: str, max_results: int = 10) -> AdsOperationResult:
        self._check_fault()
        q = query.lower()
        matches = [AudienceSegment(**a) for a in self._AUDIENCE_CATALOG if q in a["segment_name"].lower()]
        return AdsOperationResult(success=True, operation="audience_lookup", audiences=matches[:max_results])

    def keyword_suggestions(self, seed_keyword: str, max_results: int = 10) -> AdsOperationResult:
        self._check_fault()
        templates = ["{kw} online", "best {kw}", "{kw} near me", "buy {kw}", "{kw} reviews", "cheap {kw}", "{kw} for beginners", "{kw} vs alternatives"]
        suggestions = [
            KeywordSuggestion(keyword=t.format(kw=seed_keyword), estimated_volume=1000 - (i * 100), competition=["low", "medium", "high"][i % 3])
            for i, t in enumerate(templates)
        ]
        return AdsOperationResult(success=True, operation="keyword_suggestions", keywords=suggestions[:max_results])

    def create_ad_group(self, campaign_id: str, ad_group_name: str) -> AdsOperationResult:
        self._check_fault()
        self._get_campaign_raw(campaign_id)
        ad_group_id = self._next_id("adgroup")
        self._ad_groups[ad_group_id] = {"ad_group_id": ad_group_id, "ad_group_name": ad_group_name, "campaign_id": campaign_id, "status": "active"}
        return AdsOperationResult(success=True, operation="create_ad_group", ad_group=AdGroup(**self._ad_groups[ad_group_id]))

    def create_ad(self, ad_group_id: str, headline: str, body: str) -> AdsOperationResult:
        self._check_fault()
        if ad_group_id not in self._ad_groups:
            raise ValueError(f"ad group '{ad_group_id}' not found")
        ad_id = self._next_id("ad")
        self._ads[ad_id] = {"ad_id": ad_id, "ad_group_id": ad_group_id, "headline": headline, "body": body, "status": "active"}
        return AdsOperationResult(success=True, operation="create_ad", ad=Ad(**self._ads[ad_id]))

    def ad_preview(self, ad_id: str) -> AdsOperationResult:
        self._check_fault()
        if ad_id not in self._ads:
            raise ValueError(f"ad '{ad_id}' not found")
        ad = self._ads[ad_id]
        preview = AdPreview(ad_id=ad_id, preview_text=f"{ad['headline']} - {ad['body']}", preview_image_url=f"https://example.com/preview/{ad_id}.png")
        return AdsOperationResult(success=True, operation="ad_preview", preview=preview)


_provider: AdsProvider = AdsRESTProvider()


def get_ads_provider() -> AdsProvider:
    return _provider


def set_ads_provider(provider: AdsProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
