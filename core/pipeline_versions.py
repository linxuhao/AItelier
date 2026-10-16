"""Normal authorized edits over the native immutable version catalog.

The catalog is the existing seals, and adoption/recovery uses the native
before-change descriptor and RegistrationTransaction. Validation has no effects;
preparation can leave immutable seals when publication fails. Such seals are
reported as prepared, never as successful edits. No run is repinned.
"""
from __future__ import annotations

import copy
import json

import yaml

from contracts.rsi.pipeline_versioning import contract as c
from contracts.rsi.pipeline_versioning.native_provider import (
    NativeVersions, logical_role, project_capture, project_sealed, project_unit)
from core import pipeline_frozen_bundle as fb, pipeline_registry as pr



def active_roles(target, graph):
    found = {}
    for _, physical in fb.agent_refs(graph):
        if not physical.startswith(target + pr._ROLE_SEP):
            raise c.UnsupportedContent(f"role {physical!r} is not owned by {target!r}")
        logical = logical_role(target, physical)
        if found.setdefault(logical, physical) != physical:
            raise c.UnsupportedContent(f"ambiguous active logical role {logical!r}")
    return found


def resolve_active(target, graph, role):
    names = active_roles(target, graph)
    if role in names.values():
        return role
    if role in names:
        return names[role]
    raise c.UnsupportedContent(f"role {role!r} is not active; active roles: {sorted(names)}")


class PipelineVersions(NativeVersions):
    """Application-owned coordinator; optimizer derive_candidate is unchanged."""

    def _healthy(self, target):
        if fb.is_sealed_name(target):
            raise c.UnsupportedContent("sealed bundle is immutable")
        self._target(target)
        block = fb.adoption_block(self._dir, target)
        if block:
            raise fb.BundleRefused(block)
        try:
            unit = fb.recorded_unit(self._dir, target) or fb.read_unit(self._dir, target)
        except fb.BundleRefused as exc:
            raise fb.BundleRefused(f'recorded whole bundle refused: {exc}') from exc
        problems = fb.whole_state(self._sf, self._registry, self._dir, target, unit)
        if problems:
            raise fb.BundleRefused(f"recorded whole bundle target is registered mixed ({', '.join(problems)}); reconcile first")
        return unit

    def restore(self, target, version_id, *, expected_current):
        with fb.exclusion(self._sf):
            self._healthy(target)
            return super().restore(target, version_id, expected_current=expected_current)

    def _recorded_provider(self, vid):
        try:
            sealed = self._sealed(vid)[1]
        except c.UnknownVersion:
            return
        fb.verify_providers(self._sf, sealed)

    def _catalog(self, bundle):
        """Catalog a fully preflighted detached native unit, without a live edit."""
        _, vid = project_capture(bundle)
        try:
            physical, sealed, _ = self._sealed(vid)
            fb.verify_providers(self._sf, sealed)
            return physical
        except c.UnknownVersion:
            pass
        sealed = fb.seal(bundle, c.seal_slug(vid), self._dir)
        return sealed['physical_name']

    def _candidate(self, target, bundle, unit, intent):
        """Separate normal-operator policy; share native compile/seal/adopt mechanics."""
        body = copy.deepcopy(fb.body_of(bundle))
        graph = body['graph']
        roles = json.loads(unit['roles_text']) if unit['roles_text'] else {}
        names = active_roles(target, graph)
        missing = set(names.values()) - set(roles)
        if missing:
            raise c.UnsupportedContent(f'active roles have no persisted owned definition: {sorted(missing)}')
        kind = intent['kind']
        if kind == 'graph':
            proposed = yaml.safe_load(intent['graph_yaml'])
            if not isinstance(proposed, dict) or not proposed.get('steps'):
                raise c.MalformedContent('graph_yaml is not a pipeline with steps')
            proposed['name'] = target
            original = yaml.safe_load(unit['graph_yaml'])
            if proposed.get('x-aitelier') != original.get('x-aitelier'):
                raise c.UnsupportedContent('x-aitelier host policy is outside version content')
            proposed.pop('x-aitelier', None)
            # A bare role addresses the current graph, never retained history.
            for node, role in fb.agent_refs(proposed):
                node['agent_config'] = resolve_active(target, graph, role)
            body['graph'] = proposed
        elif kind in ('role', 'prompt'):
            role = resolve_active(target, graph, intent['role'])
            cfg = body['roles'][role]
            if kind == 'prompt':
                text = intent['template']
                if not isinstance(text, str) or not text.strip():
                    raise c.MalformedContent('template is empty')
                key = 'inline:' + role
                cfg['prompt'] = key
                body['prompts'][key] = text
                body['sources'][key] = {'path': None, 'sha256': fb.sha(text.encode('utf-8'))}
            else:
                for key, value in intent['changes'].items():
                    if key in ('model', 'tools'):
                        cfg[key] = copy.deepcopy(value)
                    else:
                        cfg['config'][key] = copy.deepcopy(value)
        elif kind == 'source':
            which = intent['file']
            field = 'roles_text' if which == 'roles' else 'graph_yaml'
            text = unit[field]
            old = intent['old_str']
            if not isinstance(old, str) or not old:
                raise c.MalformedContent('old_str is required; read config_read first')
            count = (text or '').count(old)
            if count != 1:
                raise c.Conflict(f'old_str appears {count} times; it must identify exactly one place')
            edited = text.replace(old, intent['new_str'])
            if which == 'graph':
                proposed = yaml.safe_load(edited)
                if not isinstance(proposed, dict) or proposed.get('name') != target:
                    raise c.UnsupportedContent('graph name must remain the target name')
                return self._candidate(target, bundle, unit, {'kind': 'graph', 'graph_yaml': edited})
            if which != 'roles':
                raise c.UnsupportedContent('shared/unreferenced template source is not editable')
            after = json.loads(edited)
            if not isinstance(after, dict) or set(after) != set(roles):
                raise c.UnsupportedContent('role keys must remain unchanged; only active role values are editable')
            active = set(names.values())
            for name in roles:
                if fb.canonical(roles[name]) == fb.canonical(after[name]):
                    continue
                if name not in active:
                    raise c.UnsupportedContent(f'historical/unreferenced role {name!r} is immutable')
                prior, new = roles[name], after[name]
                if not isinstance(new, dict):
                    raise c.MalformedContent('role must be a mapping')
                allowed = {'model', 'tools', 'system_prompt', 'temperature', 'thinking',
                           'native_tool_calling', 'fallback_to_json_mode',
                           'max_tool_turns', 'max_output_tokens'}
                changed = {k for k in set(prior) | set(new)
                           if k not in prior or k not in new
                           or fb.canonical(prior[k]) != fb.canonical(new[k])}
                if not changed <= allowed:
                    raise c.UnsupportedContent(f'unsupported role fields: {sorted(changed - allowed)}')
                # Resolve defaults exactly as the actual native registration does.
                resolved = fb._expected_roles(target, {name: new})[name].to_dict()
                cfg = copy.deepcopy(resolved['config'])
                model = cfg.pop('model', resolved['model'])
                tools = cfg.pop('tools', resolved['tools'])
                template = cfg.pop('template', '')
                inline = cfg.pop('system_prompt', '')
                if template:
                    if 'system_prompt' in changed:
                        raise c.UnsupportedContent('file-backed prompt: use edit_template to detach it')
                    inline = fb.safe_file(fb.ROOT / 'templates', template).read_bytes().decode('utf-8')
                key = 'inline:' + name if inline else None
                body['roles'][name] = {'model': model, 'tools': tools, 'config': cfg, 'prompt': key}
                if key:
                    body['prompts'][key] = inline
                    body['sources'][key] = {'path': None, 'sha256': fb.sha(inline.encode('utf-8'))}
        else:
            raise c.UnsupportedContent(f'unsupported edit intent {kind!r}')
        # Validate the entire future graph and providers before catalog effects.
        referenced = {r for _, r in fb.agent_refs(body['graph'])}
        body['roles'] = {r: v for r, v in body['roles'].items() if r in referenced}
        for cfg in body['roles'].values():
            if not isinstance(cfg['model'], str) or not cfg['model']:
                raise c.MalformedContent('role model must be nonempty text')
            if not isinstance(cfg['tools'], list) or not all(isinstance(t, str) and t for t in cfg['tools']):
                raise c.MalformedContent('role tools must be a list of nonempty strings')
            c.check_finite_json(cfg['config'])
        tools, caps, locations = fb.resolve_providers(self._sf, body['graph'], body['roles'])
        body.update(tools=tools, capabilities=caps, tool_loader=fb._loader_identity(self._sf))
        source = body['sources']['graph']
        body['sources']['graph'] = {**source, 'base_sha256': source.get('base_sha256') or source['sha256'],
                                    'sha256': fb.sha(body['graph'])}
        body['derived_from'] = bundle['identity']
        steps = list(fb._steps(body['graph']))
        body['inventory'] = {'steps': [s.get('id') for s in steps],
                             'outputs': {str(s.get('id')): s.get('output') for s in steps},
                             'capabilities': body['graph'].get('capabilities'), 'tools': sorted(tools)}
        candidate = {**body, 'identity': fb.sha(body),
                     'locations': {**bundle['locations'], 'tools': locations}}
        _, vid = project_capture(candidate)  # accepted codec, including finite/UTF-8 checks
        preview = fb.compile_bundle(candidate, c.seal_slug(vid))
        if project_sealed(preview)[1] != vid:
            raise c.UnsupportedContent('compiled candidate would not reproduce logical content')
        # The real registration parser/host policy validates the detached preview.
        pr._validated_registration(preview['physical_name'], preview['graph_yaml'],
                                   roles=preview['roles'], sf=self._sf)
        fb.verify_inputs(candidate)
        fb.verify_providers(self._sf, candidate)
        return candidate

    def edit(self, target, intent, *, expected_current=None):
        prepared = []
        try:
            with fb.exclusion(self._sf):
                unit = self._healthy(target)
                current = fb.unit_identity(unit)
                if expected_current is not None:
                    c.check_unit_identity(expected_current)
                    if current != expected_current:
                        raise c.Conflict('target changed: stale expected_current')
                bundle, _, previous_vid = self._capture_live(target)
                logical = (logical_role(target, resolve_active(target, bundle['graph'], intent['role']))
                           if intent['kind'] in ('role', 'prompt') else None)
                candidate = self._candidate(target, bundle, unit, intent)
                _, vid = project_capture(candidate)
                # Equal content never exempts recorded providers or catalog integrity.
                self.list_versions()
                self._recorded_provider(previous_vid)
                self._recorded_provider(vid)
                result = {'config': target, 'previous_version_id': previous_vid,
                          'version_id': vid, 'changed': vid != previous_vid,
                          'previous_unit_identity': current, 'unit_identity': current}
                if logical is not None:
                    role = active_roles(target, bundle['graph'])[logical]
                    result.update(role=role, value=json.loads(unit['roles_text'])[role])
                if vid == previous_vid:
                    return {**result, 'graph_version': self.current(target)['graph_version'],
                            'reloaded': False}
                compiled = fb.compile_bundle(candidate, c.seal_slug(vid))
                try:
                    compiled = self._sealed(vid)[1]
                except c.UnknownVersion:
                    pass
                preview = fb.preview_adoption(target, unit, compiled)
                if project_unit(target, preview, candidate['tools'])[1] != vid:
                    raise c.UnsupportedContent('adoption would not serve logical content')
                existing = json.loads(unit['roles_text']) if unit['roles_text'] else {}
                proposed = json.loads(preview['roles_text'])
                if any(proposed.get(k) != v for k, v in existing.items()):
                    raise c.UnsupportedContent('adoption would change a retained role')
                pr._validated_registration(target, preview['graph_yaml'],
                                           roles=proposed, sf=self._sf)
                # Preparation starts here. Never catalog before complete preflight.
                for version in (bundle, candidate):
                    native = fb.compile_bundle(version, c.seal_slug(project_capture(version)[1]))
                    prepared.append(native['physical_name'])
                    physical = self._catalog(version)
                    prepared[-1] = physical
                done = fb.adopt_bundle(self._sf, self._registry, target, physical,
                                       expected_current=current, config_dir=self._dir)
                if logical is not None:
                    role = active_roles(target, yaml.safe_load(preview['graph_yaml']))[logical]
                    result.update(role=role, value=proposed[role])
                return {**result, 'unit_identity': done['identity'], 'reloaded': True,
                        'graph_version': self.current(target)['graph_version']}
        except Exception as exc:
            result = {'error': f'{type(exc).__name__}: {exc}',
                      'code': getattr(exc, 'code', 'NATIVE_REFUSED'), 'changed': False}
            if prepared:
                result.update(changed=None, prepared_seals=[{'physical_name': name, 'complete': (self._dir / (name + '.bundle.json')).is_file()} for name in prepared], publication_failed=True,
                              recovery='Native descriptor/whole_state determines admission; reconcile if pending.')
            return result


def editor():
    from api.dependencies import get_skillflow, get_config_registry
    return PipelineVersions(get_skillflow(), get_config_registry(), pr.generated_configs_dir())
