"""Real native selection must not make an unsupported JS parser green."""
import json
from pathlib import Path

import pytest
import skillflow
from skillflow.lint_backends import _backends, register_backend
from core.tool_routes import AItelierToolLoader


@pytest.fixture(autouse=True)
def restore_registry():
    saved=dict(_backends)
    yield
    _backends.clear();_backends.update(saved)


def loader():
    return AItelierToolLoader(Path(skillflow.__file__).parent/'tools')


@pytest.mark.parametrize('extension',['js','mjs','cjs'])
@pytest.mark.parametrize('backend',[None,'basic','skip','unknown','ruff','djlint'])
def test_unsupported_javascript_parser_is_explicit_nonpass(tmp_path,extension,backend):
    (tmp_path/('candidate.'+extension)).write_text('const broken = ;')
    manifest=None
    if backend is not None:
        manifest='manifest.json'
        (tmp_path/manifest).write_text(json.dumps({'.'+extension.upper():backend}))
    result=loader().load_fn('lint')(['candidate.'+extension],workspace_root=str(tmp_path),manifest_path=manifest,
         run_id='own-fixture',step_id='lint',project_root=str(tmp_path))
    assert result['all_passed'] is False,result
    assert len(result['results'])==1 and "select owned 'eslint'" in result['results'][0]['error_message'],result


@pytest.mark.parametrize('extension',['js','mjs','cjs'])
def test_arbitrary_trusted_host_extension_is_not_sandboxed_or_made_green(tmp_path,extension):
    calls=[]
    def trap(fp):
        calls.append(str(fp))
        (tmp_path/'host-backend-executed').write_text('fixture only')
        return {'passed':True,'error_message':''}
    register_backend('host-trap',trap)
    (tmp_path/('input.'+extension)).write_text('const value=1;')
    (tmp_path/'manifest.json').write_text(json.dumps({'.'+extension:'host-trap'}))
    result=loader().load_fn('lint')(['input.'+extension],workspace_root=str(tmp_path),manifest_path='manifest.json')
    assert result['all_passed'] is False,result
    # Generic SDK callbacks remain trusted host implementations. Result
    # provenance refuses their unmeasured green; it does not sandbox them.
    assert calls and (tmp_path/'host-backend-executed').exists()
    assert result['results'][0]['unmeasured'] is True


def test_non_javascript_backend_dispatch_is_unchanged(tmp_path):
    calls=[]
    def custom(fp):
        calls.append(fp)
        return {'passed':True,'error_message':'actual other-language backend'}
    register_backend('host-custom',custom)
    (tmp_path/'input.css').write_text('body {}')
    (tmp_path/'manifest.json').write_text('{".css":"host-custom"}')
    result=loader().load_fn('lint')(['input.css'],workspace_root=str(tmp_path),manifest_path='manifest.json')
    assert result['all_passed'] is True and calls==[tmp_path/'input.css']
    assert result['results'][0]['error_message']=='actual other-language backend'


def test_unmeasured_result_rule_survives_native_cache_reload(tmp_path):
    host=loader()
    for _ in range(2):
        (tmp_path/'input.js').write_text('const broken=;')
        result=host.load_fn('lint')(['input.js'],workspace_root=str(tmp_path))
        assert result['all_passed'] is False,result
        host.add_tools_dir(Path(skillflow.__file__).parent/'tools')


@pytest.mark.parametrize('extension',['js','mjs','cjs'])
def test_current_host_ruff_cannot_install_or_measure_javascript(tmp_path,monkeypatch,extension):
    from api.dependencies import _ruff_check_only
    import subprocess
    register_backend('ruff',_ruff_check_only)
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:pytest.fail('wrong-extension host ruff executed a subprocess'))
    (tmp_path/('input.'+extension)).write_text('const broken = ;')
    (tmp_path/'manifest.json').write_text(json.dumps({'.'+extension:'ruff'}))
    result=loader().load_fn('lint')(['input.'+extension],workspace_root=str(tmp_path),manifest_path='manifest.json')
    assert result['all_passed'] is False and result['results'][0]['unmeasured'],result


@pytest.mark.parametrize('extension',['js','mjs','cjs'])
def test_declared_but_unregistered_eslint_cannot_be_green(tmp_path,extension):
    _backends.pop('eslint',None)
    (tmp_path/('input.'+extension)).write_text('const value=1;')
    (tmp_path/'manifest.json').write_text(json.dumps({'.'+extension:'eslint'}))
    result=loader().load_fn('lint')(['input.'+extension],workspace_root=str(tmp_path),manifest_path='manifest.json')
    assert result['all_passed'] is False and "select owned 'eslint'" in result['results'][0]['error_message'],result
