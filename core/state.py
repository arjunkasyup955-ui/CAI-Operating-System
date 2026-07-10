import operator
from typing import Annotated, Any, Literal, TypedDict

Phase = Literal[
    "idea", "research", "decision", "product", "launch", "marketing", "sales", "revenue",
]


class VentureState(TypedDict, total=False):
    """Shared state contract passed through every graph. Agents only read/write this -
    no side-channel globals. Extend this, never create a parallel state shape.
    """

    venture_id: str
    phase: Phase
    idea: str

    research_findings: Annotated[list[dict[str, Any]], operator.add]
    validation_result: dict[str, Any]

    mvp_spec: dict[str, Any]
    product_artifacts: Annotated[list[dict[str, Any]], operator.add]

    marketing_assets: Annotated[list[dict[str, Any]], operator.add]
    campaigns: Annotated[list[dict[str, Any]], operator.add]

    leads: Annotated[list[dict[str, Any]], operator.add]
    deals: Annotated[list[dict[str, Any]], operator.add]

    metrics: dict[str, Any]
    pending_approval: dict[str, Any] | None

    history: Annotated[list[dict[str, Any]], operator.add]
