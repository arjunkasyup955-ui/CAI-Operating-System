from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.ads.ads_providers import get_ads_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 20.0

# Field names deliberately never "name" - see the docstring in ads_providers.py for
# why.


class HealthCheckArgs(BaseModel):
    pass


class CreateCampaignArgs(BaseModel):
    campaign_name: str
    budget: float = 0.0
    objective: str = "conversions"
    platform: str = ""


class UpdateCampaignArgs(BaseModel):
    campaign_id: str
    campaign_name: str | None = None
    budget: float | None = None
    objective: str | None = None


class PauseCampaignArgs(BaseModel):
    campaign_id: str


class ResumeCampaignArgs(BaseModel):
    campaign_id: str


class DeleteCampaignArgs(BaseModel):
    campaign_id: str


class GetCampaignArgs(BaseModel):
    campaign_id: str


class ListCampaignsArgs(BaseModel):
    pass


class CampaignAnalyticsArgs(BaseModel):
    campaign_id: str


class BudgetUpdateArgs(BaseModel):
    campaign_id: str
    budget: float


class AudienceLookupArgs(BaseModel):
    query: str
    max_results: int = 10


class KeywordSuggestionsArgs(BaseModel):
    seed_keyword: str
    max_results: int = 10


class AdPreviewArgs(BaseModel):
    ad_id: str


class CreateAdGroupArgs(BaseModel):
    campaign_id: str
    ad_group_name: str


class CreateAdArgs(BaseModel):
    ad_group_id: str
    headline: str
    body: str


def ads_health_check() -> dict:
    return get_ads_provider().health_check().model_dump()


def ads_create_campaign(campaign_name: str, budget: float = 0.0, objective: str = "conversions", platform: str = "") -> dict:
    return get_ads_provider().create_campaign(campaign_name, budget, objective, platform).model_dump()


def ads_update_campaign(campaign_id: str, campaign_name: str | None = None, budget: float | None = None, objective: str | None = None) -> dict:
    return get_ads_provider().update_campaign(campaign_id, campaign_name, budget, objective).model_dump()


def ads_pause_campaign(campaign_id: str) -> dict:
    return get_ads_provider().pause_campaign(campaign_id).model_dump()


def ads_resume_campaign(campaign_id: str) -> dict:
    return get_ads_provider().resume_campaign(campaign_id).model_dump()


def ads_delete_campaign(campaign_id: str) -> dict:
    return get_ads_provider().delete_campaign(campaign_id).model_dump()


def ads_get_campaign(campaign_id: str) -> dict:
    return get_ads_provider().get_campaign(campaign_id).model_dump()


def ads_list_campaigns() -> dict:
    return get_ads_provider().list_campaigns().model_dump()


def ads_campaign_analytics(campaign_id: str) -> dict:
    return get_ads_provider().campaign_analytics(campaign_id).model_dump()


def ads_budget_update(campaign_id: str, budget: float) -> dict:
    return get_ads_provider().budget_update(campaign_id, budget).model_dump()


def ads_audience_lookup(query: str, max_results: int = 10) -> dict:
    return get_ads_provider().audience_lookup(query, max_results).model_dump()


def ads_keyword_suggestions(seed_keyword: str, max_results: int = 10) -> dict:
    return get_ads_provider().keyword_suggestions(seed_keyword, max_results).model_dump()


def ads_ad_preview(ad_id: str) -> dict:
    return get_ads_provider().ad_preview(ad_id).model_dump()


def ads_create_ad_group(campaign_id: str, ad_group_name: str) -> dict:
    return get_ads_provider().create_ad_group(campaign_id, ad_group_name).model_dump()


def ads_create_ad(ad_group_id: str, headline: str, body: str) -> dict:
    return get_ads_provider().create_ad(ad_group_id, headline, body).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("ads_health_check", "Check whether the configured ads provider is reachable", HealthCheckArgs, ads_health_check),
    ("ads_create_campaign", "Create an ad campaign", CreateCampaignArgs, ads_create_campaign),
    ("ads_update_campaign", "Update an ad campaign", UpdateCampaignArgs, ads_update_campaign),
    ("ads_pause_campaign", "Pause an ad campaign", PauseCampaignArgs, ads_pause_campaign),
    ("ads_resume_campaign", "Resume a paused ad campaign", ResumeCampaignArgs, ads_resume_campaign),
    ("ads_delete_campaign", "Delete an ad campaign", DeleteCampaignArgs, ads_delete_campaign),
    ("ads_get_campaign", "Get a campaign by id", GetCampaignArgs, ads_get_campaign),
    ("ads_list_campaigns", "List campaigns", ListCampaignsArgs, ads_list_campaigns),
    ("ads_campaign_analytics", "Get analytics for a campaign", CampaignAnalyticsArgs, ads_campaign_analytics),
    ("ads_budget_update", "Update a campaign's budget", BudgetUpdateArgs, ads_budget_update),
    ("ads_audience_lookup", "Look up audience segments", AudienceLookupArgs, ads_audience_lookup),
    ("ads_keyword_suggestions", "Get keyword suggestions from a seed keyword", KeywordSuggestionsArgs, ads_keyword_suggestions),
    ("ads_ad_preview", "Preview a rendered ad", AdPreviewArgs, ads_ad_preview),
    ("ads_create_ad_group", "Create an ad group within a campaign", CreateAdGroupArgs, ads_create_ad_group),
    ("ads_create_ad", "Create an ad within an ad group", CreateAdArgs, ads_create_ad),
]

for _name, _description, _schema, _func in _TOOLS:
    get_tool_registry().register(
        ToolSpec(
            name=_name,
            description=_description,
            input_schema=_schema,
            permissions=["advertisement"],
            retry_policy=_RETRY_POLICY,
            timeout_seconds=_TIMEOUT_SECONDS,
            cost_per_call_usd=0.0,
        ),
        _func,
    )
