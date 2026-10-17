"""Optimizer r2 wire statement. Data consistency never grants host authority."""
from __future__ import annotations
from typing import Annotated, Literal, Protocol
from pydantic import Field, model_validator
from contracts.rsi.evaluator.contract import (
    Benchmark, Budget, Digest, Frozen, Name, Runtime, Selection, TrainProjection,
    require,
)

DESIGN = 'a23c04fa9409e991f1ce4dc3c55a2bcb1cdb17022c6e084b69f1eee33cd9ba94'
UnitCAS = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Text = Annotated[str, Field(min_length=1, max_length=100000)]


class Target(Frozen):
    kind: Literal['generated', 'builtin']
    name: str
    expected_current: UnitCAS | None
    source_commit: Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')]

    @model_validator(mode='after')
    def owned(self):
        from contracts.rsi.pipeline_versioning.contract import check_target
        if self.kind == 'generated':
            check_target(self.name)
            require(self.expected_current is not None, 'generated target requires whole-unit CAS')
        else:
            require(self.name == 'investigate' and self.expected_current is None,
                    'builtin pilot stays Source-owned; no runtime restore/adopt')
        return self


class RoundRequest(Frozen):
    schema: Literal['rsi.optimizer.round.v2']
    campaign_id: Name
    round_id: Name
    round_number: Annotated[int, Field(strict=True, ge=1, le=2)]
    design: Literal['a23c04fa9409e991f1ce4dc3c55a2bcb1cdb17022c6e084b69f1eee33cd9ba94']
    mode: Literal['manual_candidate'] = 'manual_candidate'
    target: Target
    reference: Selection
    benchmark: Benchmark             # trusted-private; NOT optimizer context
    runtime: Runtime
    budget: Budget                   # existing host grant lookup, never spend permission

    @model_validator(mode='after')
    def fixed(self):
        require(self.reference.arm == 'reference', 'reference arm')
        require(self.budget.pricing_identity == self.runtime.pricing, 'pricing')
        for env, cap in [(self.budget.cell, (8,128000,16000,300000)),
                         (self.budget.round, (1000,16000000,2000000,14400000)),
                         (self.budget.campaign, (3000,48000000,6000000,43200000))]:
            require(env.upper.requests <= cap[0] and env.upper.input_tokens <= cap[1]
                    and env.upper.output_tokens <= cap[2] and env.elapsed_ms <= cap[3], 'design ceiling')
        for key in ('requests','input_tokens','output_tokens','usd_micros'):
            require(getattr(self.budget.cell.upper,key)<=getattr(self.budget.round.upper,key)
                    <=getattr(self.budget.campaign.upper,key), 'nested envelopes')
        require(self.budget.cell.elapsed_ms<=self.budget.round.elapsed_ms<=self.budget.campaign.elapsed_ms, 'nested deadlines')
        require(self.budget.round.upper.usd_micros <= 25000000
                and self.budget.campaign.upper.usd_micros <= 75000000, 'design USD ceiling')
        return self


class Empty(Frozen):
    pass


class Diagnosis(Frozen):
    observation: Text
    train_case_ids: tuple[Name, ...] = Field(min_length=1)


class Hypothesis(Frozen):
    hypothesis_id: Name
    change: Text
    falsifiable_prediction: Text
    metric: Literal['mean_normalized']
    train_case_ids: tuple[Name, ...] = Field(min_length=1)


class Edit(Frozen):
    # EXACT accepted native derive_candidate edit keys; prompt keys resolve from
    # reference native bundle (e.g. inline:<role> or template:<relative path>).
    prompts: dict[str, Text] = Field(default_factory=dict)
    graph: dict | None = None


class HoldoutAuthorization(Frozen):
    selection: Digest    # resolved against EXISTING trusted evaluator grant


class Decision(Frozen):
    action: Literal['candidate', 'retain', 'adopt', 'source_patch'] = 'candidate'
    review_ref: Name | None = None
    owner_decision_ref: Name | None = None


class Stop(Frozen):
    reason: Literal['target', 'round_limit', 'budget', 'contamination',
                    'no_admissible_edit', 'no_train_improvement', 'holdout_rejected',
                    'invalid', 'unknown', 'manual_candidate', 'retained', 'adopted',
                    'source_patch', 'owner_stop']


class RoundView(Frozen):
    schema: Literal['rsi.optimizer.view.v2'] = 'rsi.optimizer.view.v2'
    phase: Literal['freeze','train_reference','diagnose','hypothesis','candidate','admissibility',
                   'train_candidate','train_compare','holdout_seal','holdout','compare','decision',
                   'adoption_review','adopt','restore','source_patch','stop']
    reference_version: Digest
    candidate_version: Digest | None
    current_version: Digest
    public_train: TrainProjection | None
    stopped: bool
    stop_reason: str | None
    retained_unknown_usd_micros: Annotated[int, Field(strict=True, ge=0)]
    quiescent: bool
    action: Literal['candidate','retain','adopt','source_patch','stop'] = 'candidate'
    grants_authority: Literal[False] = False


class GraphResult(Frozen):
    continue_: bool = Field(alias='continue')
    action: Literal['candidate','retain','adopt','source_patch','stop']
    view: RoundView


def graph_result(view: RoundView) -> dict:
    """Exact NEW tool-port return -> SDK tool-result flags (no host effects)."""
    return GraphResult(**{'continue':view.phase not in ('stop','restore') and not view.stopped,
                          'action':view.action,'view':view}).model_dump(mode='json',by_alias=True)


class OptimizerError(ValueError):
    """Loud, no-effect entry refusal. After-launch outcomes are retained views."""
    def __init__(self, code, detail=''):
        self.code = code
        super().__init__(f'{code}: {detail}')


# NEW trusted binding ports, not existing MCP tool names. Graph tool nodes bind
# to one port each. Caller role is host-supplied context, never a payload field.
PORTS = {
    'freeze': RoundRequest, 'train_reference': Empty, 'diagnose': Diagnosis,
    'hypothesis': Hypothesis, 'candidate': Edit, 'admissibility': Empty,
    'train_candidate': Empty, 'train_compare': Empty,
    'holdout_seal': HoldoutAuthorization, 'holdout': Empty, 'compare': Empty,
    'decision': Decision, 'adoption_review': Empty, 'adopt': Empty, 'restore': Empty,
    'source_patch': Empty, 'stop': Stop,
}
ROLES = {p: 'coordinator' for p in PORTS}
ROLES.update(diagnose='proposer', hypothesis='proposer', candidate='proposer',
             holdout_seal='owner', decision='owner', adoption_review='owner', adopt='owner', source_patch='owner')


class RoundBinding(Protocol):
    """A production implementation is a FUTURE proof obligation.

    It binds actual owned graph-step/port grants, existing provider identities,
    genuine evaluator settlement and existing whole-unit CAS paths. It must not
    implement fake runs/pins or accept untrusted receipts as host facts.
    """
    def invoke(self, port: str, payload: dict) -> RoundView: ...
