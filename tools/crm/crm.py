from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.crm.crm_providers import get_crm_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 20.0

# Field names deliberately never "name" - see the docstring in crm_providers.py for
# why.


class HealthCheckArgs(BaseModel):
    pass


class CreateLeadArgs(BaseModel):
    first_name: str
    last_name: str
    email: str
    company: str = ""
    status: str = "new"


class UpdateLeadArgs(BaseModel):
    lead_id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    company: str | None = None
    status: str | None = None


class DeleteLeadArgs(BaseModel):
    lead_id: str


class GetLeadArgs(BaseModel):
    lead_id: str


class SearchLeadsArgs(BaseModel):
    query: str
    max_results: int = 10


class CreateContactArgs(BaseModel):
    first_name: str
    last_name: str
    email: str


class UpdateContactArgs(BaseModel):
    contact_id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None


class CreateCompanyArgs(BaseModel):
    company_name: str
    domain: str = ""


class CreateDealArgs(BaseModel):
    deal_name: str
    amount: float = 0.0
    stage: str = "new"
    pipeline_id: str = ""


class UpdateDealArgs(BaseModel):
    deal_id: str
    deal_name: str | None = None
    amount: float | None = None
    stage: str | None = None
    pipeline_id: str | None = None


class AddNoteArgs(BaseModel):
    record_id: str
    body: str


class ListPipelinesArgs(BaseModel):
    pass


def crm_health_check() -> dict:
    return get_crm_provider().health_check().model_dump()


def crm_create_lead(first_name: str, last_name: str, email: str, company: str = "", status: str = "new") -> dict:
    return get_crm_provider().create_lead(first_name, last_name, email, company, status).model_dump()


def crm_update_lead(lead_id: str, first_name: str | None = None, last_name: str | None = None, email: str | None = None, company: str | None = None, status: str | None = None) -> dict:
    return get_crm_provider().update_lead(lead_id, first_name, last_name, email, company, status).model_dump()


def crm_delete_lead(lead_id: str) -> dict:
    return get_crm_provider().delete_lead(lead_id).model_dump()


def crm_get_lead(lead_id: str) -> dict:
    return get_crm_provider().get_lead(lead_id).model_dump()


def crm_search_leads(query: str, max_results: int = 10) -> dict:
    return get_crm_provider().search_leads(query, max_results).model_dump()


def crm_create_contact(first_name: str, last_name: str, email: str) -> dict:
    return get_crm_provider().create_contact(first_name, last_name, email).model_dump()


def crm_update_contact(contact_id: str, first_name: str | None = None, last_name: str | None = None, email: str | None = None) -> dict:
    return get_crm_provider().update_contact(contact_id, first_name, last_name, email).model_dump()


def crm_create_company(company_name: str, domain: str = "") -> dict:
    return get_crm_provider().create_company(company_name, domain).model_dump()


def crm_create_deal(deal_name: str, amount: float = 0.0, stage: str = "new", pipeline_id: str = "") -> dict:
    return get_crm_provider().create_deal(deal_name, amount, stage, pipeline_id).model_dump()


def crm_update_deal(deal_id: str, deal_name: str | None = None, amount: float | None = None, stage: str | None = None, pipeline_id: str | None = None) -> dict:
    return get_crm_provider().update_deal(deal_id, deal_name, amount, stage, pipeline_id).model_dump()


def crm_add_note(record_id: str, body: str) -> dict:
    return get_crm_provider().add_note(record_id, body).model_dump()


def crm_list_pipelines() -> dict:
    return get_crm_provider().list_pipelines().model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("crm_health_check", "Check whether the configured CRM provider is reachable", HealthCheckArgs, crm_health_check),
    ("crm_create_lead", "Create a lead", CreateLeadArgs, crm_create_lead),
    ("crm_update_lead", "Update a lead", UpdateLeadArgs, crm_update_lead),
    ("crm_delete_lead", "Delete a lead", DeleteLeadArgs, crm_delete_lead),
    ("crm_get_lead", "Get a lead by id", GetLeadArgs, crm_get_lead),
    ("crm_search_leads", "Search leads", SearchLeadsArgs, crm_search_leads),
    ("crm_create_contact", "Create a contact", CreateContactArgs, crm_create_contact),
    ("crm_update_contact", "Update a contact", UpdateContactArgs, crm_update_contact),
    ("crm_create_company", "Create a company", CreateCompanyArgs, crm_create_company),
    ("crm_create_deal", "Create a deal", CreateDealArgs, crm_create_deal),
    ("crm_update_deal", "Update a deal", UpdateDealArgs, crm_update_deal),
    ("crm_add_note", "Add a note to a lead/contact/company/deal", AddNoteArgs, crm_add_note),
    ("crm_list_pipelines", "List deal pipelines", ListPipelinesArgs, crm_list_pipelines),
]

for _name, _description, _schema, _func in _TOOLS:
    get_tool_registry().register(
        ToolSpec(
            name=_name,
            description=_description,
            input_schema=_schema,
            permissions=["crm"],
            retry_policy=_RETRY_POLICY,
            timeout_seconds=_TIMEOUT_SECONDS,
            cost_per_call_usd=0.0,
        ),
        _func,
    )
