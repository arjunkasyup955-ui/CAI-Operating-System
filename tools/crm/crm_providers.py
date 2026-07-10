import os
from typing import Any, Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this
# tool module never needs the frozen Phase 0 kernel to change to gain a new config
# key - same convention as tools/n8n, tools/searxng, tools/mcp.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class CRMHealthStatus(BaseModel):
    healthy: bool
    configured: bool
    provider_type: str = ""
    error: str = ""


class CRMLead(BaseModel):
    lead_id: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    company: str = ""
    status: str = "new"


class CRMContact(BaseModel):
    contact_id: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""


class CRMCompany(BaseModel):
    company_id: str
    company_name: str = ""
    domain: str = ""


class CRMDeal(BaseModel):
    deal_id: str
    deal_name: str = ""
    amount: float = 0.0
    stage: str = "new"
    pipeline_id: str = ""


class CRMNote(BaseModel):
    note_id: str
    record_id: str = ""
    body: str = ""


class CRMPipeline(BaseModel):
    pipeline_id: str
    pipeline_name: str = ""
    stages: list[str] = []


class CRMOperationResult(BaseModel):
    success: bool
    operation: str
    lead: CRMLead | None = None
    leads: list[CRMLead] = []
    contact: CRMContact | None = None
    company: CRMCompany | None = None
    deal: CRMDeal | None = None
    note: CRMNote | None = None
    pipelines: list[CRMPipeline] = []
    error: str = ""


class CRMProvider(Protocol):
    """CRM connectivity, abstracted across HubSpot/Salesforce/Zoho CRM/Pipedrive
    behind one interface (which concrete backend the real provider targets is a
    config choice, not a code choice - see CRMRESTProvider). Every method either
    returns a CRMOperationResult/CRMHealthStatus on success or raises on failure -
    the agent layer's try/except (with ToolRegistry's RetryPolicy) handles graceful
    degradation uniformly, the same pattern as every prior provider. Field names
    deliberately avoid "name" anywhere (ToolRegistry.invoke()'s own positional
    parameter is literally called `name` - a schema field called `name` collides
    with it; found and fixed in the Chroma component, Phase 3 Component 3; avoided
    proactively here via "company_name"/"deal_name"/"pipeline_name").
    """

    name: str

    def health_check(self) -> CRMHealthStatus: ...
    def create_lead(self, first_name: str, last_name: str, email: str, company: str = "", status: str = "new") -> CRMOperationResult: ...
    def update_lead(self, lead_id: str, first_name: str | None = None, last_name: str | None = None, email: str | None = None, company: str | None = None, status: str | None = None) -> CRMOperationResult: ...
    def delete_lead(self, lead_id: str) -> CRMOperationResult: ...
    def get_lead(self, lead_id: str) -> CRMOperationResult: ...
    def search_leads(self, query: str, max_results: int = 10) -> CRMOperationResult: ...
    def create_contact(self, first_name: str, last_name: str, email: str) -> CRMOperationResult: ...
    def update_contact(self, contact_id: str, first_name: str | None = None, last_name: str | None = None, email: str | None = None) -> CRMOperationResult: ...
    def create_company(self, company_name: str, domain: str = "") -> CRMOperationResult: ...
    def create_deal(self, deal_name: str, amount: float = 0.0, stage: str = "new", pipeline_id: str = "") -> CRMOperationResult: ...
    def update_deal(self, deal_id: str, deal_name: str | None = None, amount: float | None = None, stage: str | None = None, pipeline_id: str | None = None) -> CRMOperationResult: ...
    def add_note(self, record_id: str, body: str) -> CRMOperationResult: ...
    def list_pipelines(self) -> CRMOperationResult: ...


# Per-vendor resource paths - a deliberately terse mapping (not a full field-level
# schema translation layer for each vendor's actual payload shape) since none of
# these 4 accounts exist in this sandbox to verify field-level fidelity against.
# HubSpot models "leads" as Contacts with a lifecycle stage (it has no native Lead
# object); Salesforce/Zoho/Pipedrive all have a native Lead-like resource.
_PROVIDER_CONFIGS: dict[str, dict[str, Any]] = {
    "hubspot": {
        "base_url": "https://api.hubapi.com",
        "paths": {
            "leads": "/crm/v3/objects/contacts", "contacts": "/crm/v3/objects/contacts",
            "companies": "/crm/v3/objects/companies", "deals": "/crm/v3/objects/deals",
            "notes": "/crm/v3/objects/notes", "pipelines": "/crm/v3/pipelines/deals",
        },
    },
    "salesforce": {
        "base_url": "https://login.salesforce.com",
        "paths": {
            "leads": "/services/data/v59.0/sobjects/Lead", "contacts": "/services/data/v59.0/sobjects/Contact",
            "companies": "/services/data/v59.0/sobjects/Account", "deals": "/services/data/v59.0/sobjects/Opportunity",
            "notes": "/services/data/v59.0/sobjects/Note", "pipelines": "/services/data/v59.0/query",
        },
    },
    "zoho": {
        "base_url": "https://www.zohoapis.com",
        "paths": {
            "leads": "/crm/v2/Leads", "contacts": "/crm/v2/Contacts", "companies": "/crm/v2/Accounts",
            "deals": "/crm/v2/Deals", "notes": "/crm/v2/Notes", "pipelines": "/crm/v2/settings/pipeline",
        },
    },
    "pipedrive": {
        "base_url": "https://api.pipedrive.com",
        "paths": {
            "leads": "/v1/leads", "contacts": "/v1/persons", "companies": "/v1/organizations",
            "deals": "/v1/deals", "notes": "/v1/notes", "pipelines": "/v1/pipelines",
        },
    },
}


class CRMRESTProvider:
    """Default CRMProvider - real integration over httpx (already an AFOS
    dependency), lazily initialized. Which of the 4 supported vendors it targets is
    a config choice (CRM_PROVIDER env var: hubspot/salesforce/zoho/pipedrive,
    default hubspot), not a code choice - adding a 5th vendor means adding one entry
    to _PROVIDER_CONFIGS, not a new class. Endpoint paths follow each vendor's
    commonly documented REST shape and are unverified against any live account in
    this sandbox (no CRM_API_KEY configured for any of the 4) - adjust paths/auth if
    your account's API version differs. Every call gracefully surfaces the 6 named
    failure modes (missing key, auth failure, rate limiting, invalid record ID,
    network timeout, provider unavailable) rather than crashing.
    """

    name = "crm_rest"

    def __init__(self, provider_type: str | None = None) -> None:
        self._provider_type = (provider_type or _env("CRM_PROVIDER", "hubspot")).lower()
        self._api_key = _env("CRM_API_KEY")
        self._client = None

    def _paths(self) -> dict[str, str]:
        config = _PROVIDER_CONFIGS.get(self._provider_type)
        if config is None:
            raise RuntimeError(f"unsupported CRM provider '{self._provider_type}' (expected one of {sorted(_PROVIDER_CONFIGS)})")
        return config["paths"]

    def _get_client(self):
        if not self._api_key:
            raise RuntimeError(f"CRM_API_KEY is not configured for provider '{self._provider_type}'")
        config = _PROVIDER_CONFIGS.get(self._provider_type)
        if config is None:
            raise RuntimeError(f"unsupported CRM provider '{self._provider_type}' (expected one of {sorted(_PROVIDER_CONFIGS)})")
        if self._client is None:
            import httpx

            self._client = httpx.Client(base_url=config["base_url"], headers={"Authorization": f"Bearer {self._api_key}"}, timeout=20.0)
        return self._client

    def health_check(self) -> CRMHealthStatus:
        if not self._api_key:
            return CRMHealthStatus(healthy=False, configured=False, provider_type=self._provider_type, error=f"CRM_API_KEY is not configured for provider '{self._provider_type}'")
        try:
            self._request("GET", "/")
            return CRMHealthStatus(healthy=True, configured=True, provider_type=self._provider_type)
        except Exception as exc:
            return CRMHealthStatus(healthy=False, configured=True, provider_type=self._provider_type, error=str(exc))

    def _request(self, method: str, path: str, **kwargs: Any):
        import httpx

        client = self._get_client()
        try:
            response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise RuntimeError("network timeout while contacting CRM provider") from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(f"CRM provider '{self._provider_type}' is currently unavailable") from exc
        if response.status_code in (401, 403):
            raise RuntimeError("authentication failed - invalid or expired credentials")
        if response.status_code == 429:
            raise RuntimeError("rate limited by CRM provider - too many requests")
        if response.status_code == 404:
            raise RuntimeError("record not found (invalid record ID)")
        response.raise_for_status()
        return response

    def create_lead(self, first_name: str, last_name: str, email: str, company: str = "", status: str = "new") -> CRMOperationResult:
        response = self._request("POST", self._paths()["leads"], json={"first_name": first_name, "last_name": last_name, "email": email, "company": company, "status": status})
        lead_id = str(response.json().get("id", ""))
        return CRMOperationResult(success=True, operation="create_lead", lead=CRMLead(lead_id=lead_id, first_name=first_name, last_name=last_name, email=email, company=company, status=status))

    def update_lead(self, lead_id: str, first_name: str | None = None, last_name: str | None = None, email: str | None = None, company: str | None = None, status: str | None = None) -> CRMOperationResult:
        body = {k: v for k, v in {"first_name": first_name, "last_name": last_name, "email": email, "company": company, "status": status}.items() if v is not None}
        self._request("PATCH", f"{self._paths()['leads']}/{lead_id}", json=body)
        return CRMOperationResult(success=True, operation="update_lead", lead=CRMLead(lead_id=lead_id, **{k: v for k, v in body.items()}))

    def delete_lead(self, lead_id: str) -> CRMOperationResult:
        self._request("DELETE", f"{self._paths()['leads']}/{lead_id}")
        return CRMOperationResult(success=True, operation="delete_lead")

    def get_lead(self, lead_id: str) -> CRMOperationResult:
        response = self._request("GET", f"{self._paths()['leads']}/{lead_id}")
        data = response.json()
        return CRMOperationResult(success=True, operation="get_lead", lead=CRMLead(lead_id=lead_id, first_name=data.get("first_name", ""), last_name=data.get("last_name", ""), email=data.get("email", ""), company=data.get("company", ""), status=data.get("status", "new")))

    def search_leads(self, query: str, max_results: int = 10) -> CRMOperationResult:
        response = self._request("GET", self._paths()["leads"], params={"q": query, "limit": max_results})
        data = response.json()
        leads = [CRMLead(lead_id=str(item.get("id", "")), first_name=item.get("first_name", ""), last_name=item.get("last_name", ""), email=item.get("email", ""), company=item.get("company", "")) for item in data.get("results", [])[:max_results]]
        return CRMOperationResult(success=True, operation="search_leads", leads=leads)

    def create_contact(self, first_name: str, last_name: str, email: str) -> CRMOperationResult:
        response = self._request("POST", self._paths()["contacts"], json={"first_name": first_name, "last_name": last_name, "email": email})
        contact_id = str(response.json().get("id", ""))
        return CRMOperationResult(success=True, operation="create_contact", contact=CRMContact(contact_id=contact_id, first_name=first_name, last_name=last_name, email=email))

    def update_contact(self, contact_id: str, first_name: str | None = None, last_name: str | None = None, email: str | None = None) -> CRMOperationResult:
        body = {k: v for k, v in {"first_name": first_name, "last_name": last_name, "email": email}.items() if v is not None}
        self._request("PATCH", f"{self._paths()['contacts']}/{contact_id}", json=body)
        return CRMOperationResult(success=True, operation="update_contact", contact=CRMContact(contact_id=contact_id, **body))

    def create_company(self, company_name: str, domain: str = "") -> CRMOperationResult:
        response = self._request("POST", self._paths()["companies"], json={"company_name": company_name, "domain": domain})
        company_id = str(response.json().get("id", ""))
        return CRMOperationResult(success=True, operation="create_company", company=CRMCompany(company_id=company_id, company_name=company_name, domain=domain))

    def create_deal(self, deal_name: str, amount: float = 0.0, stage: str = "new", pipeline_id: str = "") -> CRMOperationResult:
        response = self._request("POST", self._paths()["deals"], json={"deal_name": deal_name, "amount": amount, "stage": stage, "pipeline_id": pipeline_id})
        deal_id = str(response.json().get("id", ""))
        return CRMOperationResult(success=True, operation="create_deal", deal=CRMDeal(deal_id=deal_id, deal_name=deal_name, amount=amount, stage=stage, pipeline_id=pipeline_id))

    def update_deal(self, deal_id: str, deal_name: str | None = None, amount: float | None = None, stage: str | None = None, pipeline_id: str | None = None) -> CRMOperationResult:
        body = {k: v for k, v in {"deal_name": deal_name, "amount": amount, "stage": stage, "pipeline_id": pipeline_id}.items() if v is not None}
        self._request("PATCH", f"{self._paths()['deals']}/{deal_id}", json=body)
        return CRMOperationResult(success=True, operation="update_deal", deal=CRMDeal(deal_id=deal_id, **body))

    def add_note(self, record_id: str, body: str) -> CRMOperationResult:
        response = self._request("POST", self._paths()["notes"], json={"record_id": record_id, "body": body})
        note_id = str(response.json().get("id", ""))
        return CRMOperationResult(success=True, operation="add_note", note=CRMNote(note_id=note_id, record_id=record_id, body=body))

    def list_pipelines(self) -> CRMOperationResult:
        response = self._request("GET", self._paths()["pipelines"])
        data = response.json()
        pipelines = [CRMPipeline(pipeline_id=str(p.get("id", "")), pipeline_name=p.get("name", ""), stages=p.get("stages", [])) for p in data.get("results", [])]
        return CRMOperationResult(success=True, operation="list_pipelines", pipelines=pipelines)


class FakeCRMProvider:
    """In-memory CRMProvider - deterministic, no real CRM account needed. Genuine
    CRUD state (not canned responses): unknown record IDs raise on get/update/
    delete/add_note, mirroring real CRM semantics. Also supports an optional
    `fault` constructor flag ("auth_failure" | "rate_limited" | "network_timeout" |
    "provider_unavailable") that makes every operation raise the corresponding
    error - a deterministic, testable substitute for the 4 real-world failure modes
    that only genuinely occur at the network/vendor layer and can't be exercised
    without a live account (the other 2 named scenarios - missing API key and
    invalid record ID - are demonstrated directly: the former via the real
    provider's network-free config check, the latter via this class's own CRUD
    gating).
    """

    name = "fake_crm"

    _FAULTS = {
        "auth_failure": "authentication failed - invalid or expired credentials",
        "rate_limited": "rate limited by CRM provider - too many requests",
        "network_timeout": "network timeout while contacting CRM provider",
        "provider_unavailable": "CRM provider is currently unavailable",
    }

    def __init__(self, fault: str | None = None) -> None:
        self._fault = fault
        self._leads: dict[str, dict[str, Any]] = {}
        self._contacts: dict[str, dict[str, Any]] = {}
        self._companies: dict[str, dict[str, Any]] = {}
        self._deals: dict[str, dict[str, Any]] = {}
        self._notes: dict[str, list[dict[str, Any]]] = {}
        self._pipelines: dict[str, dict[str, Any]] = {
            "pipeline-default": {"pipeline_name": "Sales Pipeline", "stages": ["new", "qualified", "proposal", "won", "lost"]},
        }
        self._counter = 0

    def _check_fault(self) -> None:
        if self._fault in self._FAULTS:
            raise RuntimeError(self._FAULTS[self._fault])

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def health_check(self) -> CRMHealthStatus:
        self._check_fault()
        return CRMHealthStatus(healthy=True, configured=True, provider_type="fake")

    def create_lead(self, first_name: str, last_name: str, email: str, company: str = "", status: str = "new") -> CRMOperationResult:
        self._check_fault()
        lead_id = self._next_id("lead")
        self._leads[lead_id] = {"lead_id": lead_id, "first_name": first_name, "last_name": last_name, "email": email, "company": company, "status": status}
        return CRMOperationResult(success=True, operation="create_lead", lead=CRMLead(**self._leads[lead_id]))

    def _get_lead_raw(self, lead_id: str) -> dict[str, Any]:
        if lead_id not in self._leads:
            raise ValueError(f"lead '{lead_id}' not found")
        return self._leads[lead_id]

    def get_lead(self, lead_id: str) -> CRMOperationResult:
        self._check_fault()
        return CRMOperationResult(success=True, operation="get_lead", lead=CRMLead(**self._get_lead_raw(lead_id)))

    def update_lead(self, lead_id: str, first_name: str | None = None, last_name: str | None = None, email: str | None = None, company: str | None = None, status: str | None = None) -> CRMOperationResult:
        self._check_fault()
        lead = self._get_lead_raw(lead_id)
        for field, value in {"first_name": first_name, "last_name": last_name, "email": email, "company": company, "status": status}.items():
            if value is not None:
                lead[field] = value
        return CRMOperationResult(success=True, operation="update_lead", lead=CRMLead(**lead))

    def delete_lead(self, lead_id: str) -> CRMOperationResult:
        self._check_fault()
        self._get_lead_raw(lead_id)
        del self._leads[lead_id]
        return CRMOperationResult(success=True, operation="delete_lead")

    def search_leads(self, query: str, max_results: int = 10) -> CRMOperationResult:
        self._check_fault()
        q = query.lower()
        matches = [
            CRMLead(**lead) for lead in self._leads.values()
            if q in lead["first_name"].lower() or q in lead["last_name"].lower() or q in lead["email"].lower() or q in lead["company"].lower()
        ]
        return CRMOperationResult(success=True, operation="search_leads", leads=matches[:max_results])

    def create_contact(self, first_name: str, last_name: str, email: str) -> CRMOperationResult:
        self._check_fault()
        contact_id = self._next_id("contact")
        self._contacts[contact_id] = {"contact_id": contact_id, "first_name": first_name, "last_name": last_name, "email": email}
        return CRMOperationResult(success=True, operation="create_contact", contact=CRMContact(**self._contacts[contact_id]))

    def update_contact(self, contact_id: str, first_name: str | None = None, last_name: str | None = None, email: str | None = None) -> CRMOperationResult:
        self._check_fault()
        if contact_id not in self._contacts:
            raise ValueError(f"contact '{contact_id}' not found")
        contact = self._contacts[contact_id]
        for field, value in {"first_name": first_name, "last_name": last_name, "email": email}.items():
            if value is not None:
                contact[field] = value
        return CRMOperationResult(success=True, operation="update_contact", contact=CRMContact(**contact))

    def create_company(self, company_name: str, domain: str = "") -> CRMOperationResult:
        self._check_fault()
        company_id = self._next_id("company")
        self._companies[company_id] = {"company_id": company_id, "company_name": company_name, "domain": domain}
        return CRMOperationResult(success=True, operation="create_company", company=CRMCompany(**self._companies[company_id]))

    def create_deal(self, deal_name: str, amount: float = 0.0, stage: str = "new", pipeline_id: str = "") -> CRMOperationResult:
        self._check_fault()
        if pipeline_id and pipeline_id not in self._pipelines:
            raise ValueError(f"pipeline '{pipeline_id}' not found")
        deal_id = self._next_id("deal")
        self._deals[deal_id] = {"deal_id": deal_id, "deal_name": deal_name, "amount": amount, "stage": stage, "pipeline_id": pipeline_id or "pipeline-default"}
        return CRMOperationResult(success=True, operation="create_deal", deal=CRMDeal(**self._deals[deal_id]))

    def update_deal(self, deal_id: str, deal_name: str | None = None, amount: float | None = None, stage: str | None = None, pipeline_id: str | None = None) -> CRMOperationResult:
        self._check_fault()
        if deal_id not in self._deals:
            raise ValueError(f"deal '{deal_id}' not found")
        deal = self._deals[deal_id]
        for field, value in {"deal_name": deal_name, "amount": amount, "stage": stage, "pipeline_id": pipeline_id}.items():
            if value is not None:
                deal[field] = value
        return CRMOperationResult(success=True, operation="update_deal", deal=CRMDeal(**deal))

    def add_note(self, record_id: str, body: str) -> CRMOperationResult:
        self._check_fault()
        known_ids = set(self._leads) | set(self._contacts) | set(self._companies) | set(self._deals)
        if record_id not in known_ids:
            raise ValueError(f"record '{record_id}' not found")
        note_id = self._next_id("note")
        self._notes.setdefault(record_id, []).append({"note_id": note_id, "record_id": record_id, "body": body})
        return CRMOperationResult(success=True, operation="add_note", note=CRMNote(note_id=note_id, record_id=record_id, body=body))

    def list_pipelines(self) -> CRMOperationResult:
        self._check_fault()
        pipelines = [CRMPipeline(pipeline_id=pid, pipeline_name=p["pipeline_name"], stages=p["stages"]) for pid, p in self._pipelines.items()]
        return CRMOperationResult(success=True, operation="list_pipelines", pipelines=pipelines)


_provider: CRMProvider = CRMRESTProvider()


def get_crm_provider() -> CRMProvider:
    return _provider


def set_crm_provider(provider: CRMProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
