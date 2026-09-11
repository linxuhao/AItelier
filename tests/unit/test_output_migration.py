import copy
from pathlib import Path
import yaml
import pytest
from core.output_migration import migrate_document, migrate_generated_outputs


def graph():
    return {'name':'gen_test','begin':'implement','steps':[
        {'id':'implement','output':{'mode':'write'},
         'lifecycle':{'on_deliver':[{'tool':'repo_apply','params':{'source_dir':'$STEP_DIR'}},
                                     {'tool':'repo_delete'}]},'transitions':[{'to':None}]}]}


def test_code_migration_removes_copy_and_deferred_delete_but_keeps_other_hooks():
    d=graph()
    lint = {'tool':'lint','files':['*.py'], 'on_failure':'warn'}
    pure = {'tool':'file_exists','files':['main.py']}
    d['steps'][0]['lifecycle']['after_deliver']=[lint, pure]
    changed=migrate_document(d)
    assert changed and d['steps'][0]['output']['target']=='code'
    assert d['steps'][0]['lifecycle']=={'after_deliver':[pure]}
    assert d['steps'][0]['validation']==[lint]
    assert migrate_document(d)==[]


def test_artifact_write_modes_are_not_misclassified():
    d=graph();del d['steps'][0]['lifecycle'];old=copy.deepcopy(d)
    assert migrate_document(d)==[] and d==old


def test_mixed_slot_targets_are_preserved_without_source_copy():
    d=graph();d['steps'][0]['output']={'mode':'content','fixed':{'design':'plan.md','linter_manifest':'linter_manifest.json'}}
    migrate_document(d);s=d['steps'][0]
    assert s['output']['target']=='artifact'
    assert s['output']['fixed']['design']['target']=='artifact'
    assert s['output']['fixed']['linter_manifest']['target']=='code'
    assert 'lifecycle' not in s


def test_ambiguous_copy_contract_is_not_guessed_or_written(tmp_path):
    d=graph();d['steps'][0]['output']={'mode':'content','fixed':{'unknown':'unknown.txt'}}
    path=tmp_path/'gen_unknown.yaml';original=yaml.safe_dump(d).encode();path.write_bytes(original)
    with pytest.raises(ValueError,match='classify'):migrate_generated_outputs(tmp_path)
    assert path.read_bytes()==original


def test_boot_migration_backups_and_idempotency(tmp_path):
    root=tmp_path/'configs';root.mkdir();path=root/'gen_test.yaml'
    original=yaml.safe_dump(graph()).encode();path.write_bytes(original)
    changes=migrate_generated_outputs(root);assert len(changes)==1
    assert Path(changes[0]['backup']).read_bytes()==original
    after=path.read_bytes();assert yaml.safe_load(after)['steps'][0]['output']['target']=='code'
    assert migrate_generated_outputs(root)==[] and path.read_bytes()==after


def test_failed_config_replacement_keeps_original_and_backup(tmp_path, monkeypatch):
    from core.output_migration import write_migrated_config
    import os
    config = tmp_path / "config.yaml"
    config.write_bytes(b"original bytes")
    backup_dir = tmp_path / "backups"
    def fail(*args):
        raise OSError("replace interrupted")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="interrupted"):
        write_migrated_config(config, b"original bytes", b"new bytes", backup_dir)
    assert config.read_bytes() == b"original bytes"
    assert next(backup_dir.iterdir()).read_bytes() == b"original bytes"
    assert not list(tmp_path.glob(".output-migration-*"))


def test_stale_config_read_cannot_overwrite_newer_contents(tmp_path):
    from core.output_migration import write_migrated_config
    config = tmp_path / "config.yaml"
    config.write_bytes(b"newer user edit")
    with pytest.raises(RuntimeError, match="changed"):
        write_migrated_config(config, b"old bytes", b"migration", tmp_path / "backups")
    assert config.read_bytes() == b"newer user edit"
