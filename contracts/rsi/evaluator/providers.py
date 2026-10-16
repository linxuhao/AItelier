"""NEW typed host seams; accepted native/data providers do not implement these.

These protocols specify integration obligations, not a second run/operation DB.
Handles are trusted host objects and are never deserialized from optimizer JSON.
"""
from typing import Literal, Protocol
from .contract import (Amount, Benchmark, Cell, CellReceipt, Digest, Envelope,
                       EvaluationRequest, Execution, Frozen, Name, PhysicalAttempt,
                       Runtime, Selection, Nonnegative)


class ProviderIdentity(Frozen):
    name: str
    digest: Digest


class InvocationGrant(Frozen):
    step: str
    path: Literal["agent", "context_source", "tool_node", "validation", "after_deliver", "capability"]
    providers: tuple[ProviderIdentity, ...]  # empty means deny EVERY invocation
    public_read_roots: tuple[Digest, ...]
    artifact_write_slots: tuple[str, ...]


class CompiledPolicy(Frozen):
    identity: Digest
    graph: Digest
    selections: tuple[Selection, ...]
    grants: tuple[InvocationGrant, ...]
    effective_templates: tuple[Digest, ...]
    implicit_self_roots: tuple[Digest, ...]
    implicit_repo_roots: tuple[Digest, ...]
    public_fixture: Digest
    output_interface: tuple[str, ...]


class PhysicalRequest(Frozen):
    descriptor_id: Name
    evaluation_id: Name
    cell: Cell
    runtime: Runtime  # final effective kwargs, not route alias before binding
    cause: Literal["initial", "retry", "fallback", "format_repair", "transport_retry"]
    upper: Amount
    elapsed_cell_ms: Nonnegative
    elapsed_round_ms: Nonnegative
    elapsed_campaign_ms: Nonnegative


class Reservation(Frozen):
    reservation_id: Name
    descriptor: PhysicalRequest
    scopes: tuple[Name, ...]


class PhysicalAdmission(Protocol):
    def reserve(self, request: PhysicalRequest) -> Reservation:
        """Atomic ALL scopes, immediately before physical send, nonretryable denial.

        No positive admission if pricing/bounds/time/effective identity is unknown.
        A transport's automatic retries must each call this or be PROVEN disabled.
        """
        ...

    def settle(self, reservation: Reservation, observation: PhysicalAttempt) -> None:
        """Known usage settles; unknown retains upper and closes further dispatch.

        Append facts; preserve original unknown observation. Resume without retained
        reliable scope refuses. Positive no-send evidence alone can release to zero.
        """
        ...


class AuthorizedHostScope(Protocol):
    def authorize(self, request: EvaluationRequest) -> None:
        """Check actual coordinator authority, budget scope, one-use holdout seal.

        Ceiling values and request.authorization_ref alone grant nothing.
        """
        ...

    def compile_policy(self, request: EvaluationRequest) -> CompiledPolicy:
        """Before launch/claim/advance effects, compile finite supported shapes.

        Resolve actual native versions/providers. Include all context/source-tool,
        capability, tool-node, hooks, extra-template/tool, implicit read/write paths.
        Recheck policy/provider/input identities at every real ingress and dispatch.
        Missing run/step/policy or empty grants ALWAYS deny.
        """
        ...


class ObservedCellHost(Protocol):
    def run_cell(self, request: EvaluationRequest, cell: Cell,
                 policy: CompiledPolicy, budget: PhysicalAdmission) -> CellReceipt:
        """NEW seam over normal launch/driver/SDK, not a pin/finish API.

        Materialize one public root/project, real registered graph, actual run ID
        and SDK pin; ordinary claim/confirm/delivery; no auto checkpoint approval.
        Freeze ALL delivered bytes, trusted checker, settle actual operations and
        append strict BenchmarkStore row. Native truth comes from owning sources;
        timeout/Meta summary alone is not execution settlement.
        """
        ...
