"""Offline prompt-contract and wiring tests, NOT an LLM behavioral evaluation.

Real role registration, template loading and message delivery are exercised;
only the provider is mocked. Fixtures are a separately labeled semantic rubric.
"""
from pathlib import Path
from unittest.mock import Mock
import json

import pytest
import yaml

from core.agents import AgentFactory
from core.prompt_assembler import PromptAssembler
from skillflow import PipelineGraph
from skillflow.agent_registry import AgentRegistry

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/novel_final_state_humanize.json'
ROLES = {}
for config in ('novel_init', 'novel_chapter'):
    ROLES.update(yaml.safe_load((ROOT / 'agent_configs' / f'{config}.yaml').read_text()))


def text(role):
    return (ROOT / 'templates' / ROLES[role]['template']).read_text(encoding='utf-8')


@pytest.fixture
def registry():
    reg = AgentRegistry()
    for role, config in ROLES.items():
        reg.register(role, **config)
    return reg


@pytest.mark.parametrize('role', sorted(ROLES))
@pytest.mark.parametrize('native', [False, True])
def test_final_state_contract_is_delivered_in_real_agent_system_message(
    role, native, registry, monkeypatch, tmp_path,
):
    provider = Mock()
    provider.generate.return_value = 'mock-provider-output'
    provider.generate_native.return_value = 'mock-provider-output'
    factory = AgentFactory(registry=registry, template_base=ROOT / 'templates')
    monkeypatch.setattr(factory, '_build_gateway', lambda name: provider)
    make_agent = factory.get_native_agent if native else factory.get_agent
    agent = make_agent(role)
    assert agent.system_prompt == text(role)
    assert 'Final-state writing' in agent.system_prompt
    # Use the real dynamic prompt path too: editing feedback remains input,
    # while the system template instructs the writer not to echo it as prose.
    workspace = tmp_path / 'artifacts'
    workspace.mkdir()
    prompt = PromptAssembler().assemble(
        'draft', workspace, feedback='电影A没有僵尸',
        resolved_context={'approved_outline': '仅写已批准的门厅场景。'}, native=native,
    )
    if native:
        messages = [{'role': 'user', 'content': prompt}]
        assert agent.turn(messages) == 'mock-provider-output'
        outbound = provider.generate_native.call_args.kwargs["messages"]
        assert outbound[0] == {'role': 'system', 'content': text(role)}
        assert '电影A没有僵尸' in outbound[1]['content']
    else:
        assert agent.run(prompt) == 'mock-provider-output'
        outbound = provider.generate.call_args.kwargs
        assert outbound['system_prompt'] == text(role)
        assert '电影A没有僵尸' in outbound['user_prompt']


@pytest.mark.parametrize('role', sorted(ROLES))
def test_all_roles_resolve_current_not_cumulative_conflicting_rulings(role):
    content = text(role)
    assert '最新明确' in content
    assert '旧要求' in content
    assert '逐轮**核对用户历轮 checkpoint 反馈是否**全部仍被满足' not in content
    assert content.count('```') % 2 == 0


@pytest.mark.parametrize('role', [
    'novel_outliner', 'novel_writer', 'novel_outline_reviewer', 'novel_chapter_reviewer',
])
def test_makers_and_reviewers_cover_negative_editorial_echo(role):
    content = text(role)
    assert '电影A没有僵尸' in content
    assert '因果依赖' in content
    assert any(term in content for term in ('误判', '排查'))


@pytest.mark.parametrize('role', ['novel_humanizer', 'novel_humanize_reviewer'])
def test_polish_contract_covers_both_leakage_removal_and_information_retention(role):
    content = text(role)
    for item in ('编辑', '门后没有脚步声', '失望', '愤怒', '没看见', '不存在',
                 '暂时逼退', '消灭', '初稿', '信息', '证据', '字数', '钩子'):
        assert item in content
    # These previously granted unsafe, unconditional semantic substitutions.
    for obsolete in ('字数守恒', '修订收敛极快', '把杯子推开',
                     '一个都不许动', '这是去AI味的核心手法，放行',
                     '除了"语言表达"，还有别的东西被改了吗？'):
        assert obsolete not in content


def test_writer_review_blocks_leakage_before_polish():
    content = text('novel_chapter_reviewer')
    assert '编辑指令泄漏（硬门槛）' in content
    assert '有效反馈守护与编辑指令泄漏检查' in content
    graph = PipelineGraph.from_yaml(ROOT / 'configs/novel_chapter.yaml')
    steps = {s.id: s for s in graph.steps}
    feedback_sources = steps['draft_review'].context
    assert any(s.get('feedback_of') == 'draft' for s in feedback_sources)
    assert steps['outline_gate'].checkpoint
    assert steps['final_gate'].checkpoint
    assert steps['final_gate'].checkpoint_reject_to == 'draft'
    assert any(t.to == 'humanize' for t in steps['humanize_review'].transitions)


@pytest.mark.parametrize('role', ['novel_finalizer', 'novel_finalize_reviewer'])
def test_ledger_contract_keeps_rulings_scoped_without_fictional_events(role):
    content = text(role)
    for item in ('电影A没有僵尸', '来源', '范围', '计划', 'summary', '世界', '规则'):
        assert item in content
    assert '全书' in content
    assert '最新明确裁定' in content
    assert 'changes' in content and 'reason' in content


def test_every_novel_graph_role_uses_a_tested_template():
    used = set()
    for name in ('novel_init', 'novel_chapter'):
        graph = PipelineGraph.from_yaml(ROOT / 'configs' / f'{name}.yaml')
        used.update(s.agent_config for s in graph.steps if s.step_type == 'agent')
    assert used == set(ROLES)
    assert len(used) == 11


def test_semantic_fixture_format_and_balanced_coverage_not_model_behavior():
    data = json.loads(FIXTURES.read_text(encoding='utf-8'))
    assert data['execution_status'] == 'not_run_against_llm'
    cases = data['cases']
    assert len({c['id'] for c in cases}) == len(cases)
    required = {'negative_editorial_echo', 'meaningful_negation', 'uncertainty',
                'unique_information', 'pure_editorial_leak', 'emotion',
                'invented_props', 'damage_semantics', 'hook', 'number',
                'superseded_feedback', 'ledger_text_edit', 'ledger_rule_scope'}
    assert required <= {tag for c in cases for tag in c['tags']}
    for case in cases:
        assert case['role'] in ROLES
        assert case['context'] and case['input'] and case['rubric']
        assert case['acceptable_example'] != case['rejected_example']
