"""Project inventory at real subprocess seams; identities/providers are fixtures only."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_config import ROOT, write_config
from test_execution import wait_until
from test_quota import qh as qh
from test_controller import REPO, issue
from aru_project_driver import execution, permissions, quota_admission, reviewers, scheduler
from aru_project_driver.config import Config, DriverError
from aru_project_driver.controller import Controller
from aru_project_driver.state import State


@pytest.fixture
def configured(tmp_path):
    path = write_config(tmp_path)
    raw = json.loads(path.read_text())
    repo = 'gillella/repo'
    raw['projects'][repo]['lanes'] = ['m1', 'n1']
    raw['projects'][repo]['coding_reviewers'] = ['n1', 'm1']
    lane = raw['lanes'].pop('agent-one')
    for identity, family, native in [('m1', 'claude-code', 'claude-sub'), ('n1', 'openai-codex', 'codex')]:
        executable = tmp_path / native
        executable.write_text(f'#!{sys.executable}\nimport os, pathlib\npathlib.Path("child-env").write_text(os.environ["ARU_CODING_REVIEWERS"])\n')
        executable.chmod(0o700)
        prefix = [str(executable), '1'] if identity == 'm1' else [str(executable), 'exec']
        raw['lanes'][identity] = {**lane, 'family': family, 'capacity_key': identity,
            'command': [*prefix, '{prompt}'], 'probe_command': [*prefix, 'OK']}
    raw['projects']['gillella/other'] = {'repo_dir': raw['projects'][repo]['repo_dir'], 'lanes': ['n1']}
    raw['lanes']['n1']['projects'] = [repo, 'gillella/other']
    path.write_text(json.dumps(raw))
    return Config(path), repo


@pytest.mark.parametrize('change', ['null', 'empty', 'duplicate', 'missing', 'unauthorized', 'family', 'native',
                                   'subscription', 'alias', 'ambiguous', 'injection'])
def test_invalid_inventory_refused_before_binding(configured, change):
    config, repo = configured
    raw = deepcopy(config.raw)
    project, lanes = raw['projects'][repo], raw['lanes']
    values = {'null': None, 'empty': [], 'duplicate': ['m1', 'm1'], 'missing': ['absent'], 'injection': {'ENV': 'value'}}
    if change in values:
        project['coding_reviewers'] = values[change]
    elif change == 'unauthorized':
        project['lanes'] = ['m1']
    elif change == 'family':
        lanes['n1']['family'] = 'invented'
    elif change == 'native':
        lanes['n1']['probe_command'][0] = '/different/codex'
    elif change == 'subscription':
        lanes['m1']['probe_command'][1] = '2'
    elif change == 'alias':
        lanes['n1']['capacity_key'] = 'm1'
    else:
        lanes['n2'] = {**lanes['n1'], 'capacity_key': 'n2'}
        project['lanes'].append('n2')
        project['coding_reviewers'].append('n2')
    config.path.write_text(json.dumps(raw))
    with pytest.raises(DriverError):
        Config(config.path, bind=False)


def test_real_bridge_scheduler_and_supervised_child(configured, tmp_path, monkeypatch):
    config, repo = configured
    expected = 'claude-code:m1@1,openai-codex:n1'
    monkeypatch.setenv(reviewers.ENV, 'openai-codex:inherited')
    before = dict(os.environ)
    scripts = config.kernel_root / 'scripts'
    shutil.copytree(ROOT / 'scripts', scripts)
    # Replace only GitHub/Git reads in this private kernel copy. Canonical parsing/status still execute.
    with (scripts / 'common.py').open('a') as stream:
        stream.write('\nrepo_root = lambda: Path.cwd()\ncheckout_repository = repo_slug = lambda: "gillella/repo"\n'
                     'def gh_json(args):\n    assert args[0:2] == ["label", "list"]\n'
                     '    return [{"name": "reviewer-binding:" + i + "=" + a} for i, a in '
                     '[("m1", "author-bot"), ("n1", "independent"), ("inherited", "legacy")]]\n')
    tree = Path(config.project(repo)['repo_dir']) / '.worktrees' / 'fixture'
    tree.mkdir(parents=True)
    direct = Controller(config).adapter(repo).reviewer_status()
    assert direct['valid'] and [r['identity'] for r in direct['coding_reviewers']] == ['m1', 'n1']
    assert config.kernel_adapter('gillella/other').coding_reviewers is None
    assert reviewers.environment(config, 'gillella/other', before)[reviewers.ENV] == 'openai-codex:inherited'
    state = State(config.state_dir)
    state.save(repo, {**state.project(repo), 'enabled': True})
    # Fixture task planning prevents all claims; the actual reconcile/tick and bridge still run.
    planning = ("Controller._plan = lambda c, r: dict(actions=[], resumes=[], free_lanes=[], blocked_lanes={}, "
                "reasons=[c.adapter(r).reviewer_status()], fingerprint='fixture', actionable=True)\n")
    original_plan = Controller._plan
    exec(planning, {'Controller': Controller})
    fixture_plan, Controller._plan = Controller._plan, original_plan
    monkeypatch.setattr(Controller, '_plan', fixture_plan)
    reconciled = Controller(config, sync_reviews=lambda *a: {}).reconcile(repo)
    assert reconciled['reasons'] == [direct] and not reconciled['launched']
    # Generated heartbeat runs fresh config/controller/bridge without caller inventory.
    entry = tmp_path / 'offline-driver.py'
    entry.write_text(f'import sys, json\nsys.path.insert(0, {str(ROOT / "integrations/hermes")!r})\n'
                     'from aru_project_driver.config import Config\nfrom aru_project_driver.controller import Controller\n'
                     + planning + f'print(json.dumps(Controller(Config(sys.argv[2]), sync_reviews=lambda *a: {{}}).tick({repo!r})))\n')
    wrapper = scheduler._wrapper(config.hermes_home, repo, config.path, entry)
    result = subprocess.run([sys.executable, str(config.hermes_home / 'scripts' / wrapper)],
                            env={k: v for k, v in before.items() if k != reviewers.ENV}, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['plan']['reasons'] == reconciled['reasons']
    state.save(repo, {**state.project(repo), 'wake_pending_until': 0})
    receipt = execution.launch(config, repo, 'n1', 1, str(tree))
    wait_until(lambda: state.worker(receipt['id']).get('state') == 'exited')
    assert (tree / 'child-env').read_text() == expected
    for kind in ('implementation', 'review'):
        env = execution.scoped_environment(config, repo, kind, {'reviewer_actor': 'independent'}, None)
        assert env[reviewers.ENV] == expected
        with (tmp_path / 'capacity-fixture').open('w') as descriptor:
            process = execution._start_agent(state, {'repo': repo, 'worktree': str(tree)},
                config.lane(repo, 'n1')['command'], descriptor.fileno(), environment=env)
            assert process.wait(timeout=5) == 0
        assert (tree / 'child-env').read_text() == expected
        assert (execution.APP_RUNNER_ENV in env) == (kind != 'review' and execution.APP_RUNNER_ENV in before)
    fingerprint = permissions.fingerprint(config, repo, 'n1')
    config.project(repo)['coding_reviewers'] = ['n1']
    assert permissions.fingerprint(config, repo, 'n1') != fingerprint
    assert dict(os.environ) == before


def test_inventory_never_supplies_budget_or_assignment(qh, monkeypatch):
    lane = qh.config.lanes['claude-one']
    lane['command'] = ['/fixture/claude-sub', '1', '{prompt}']
    lane['probe_command'] = ['/fixture/claude-sub', '1', 'OK']
    qh.config.project(REPO)['coding_reviewers'] = ['claude-one']
    assert reviewers.inventory(qh.config, REPO) == 'claude-code:claude-one@1'
    task = issue(1)
    decision = quota_admission.evaluate(qh.config, qh.state, REPO, 'codex-one', task, 'implementation', qh.kernel)
    assert decision['review_budget']['authority'] == 'budget-only-canonical-helper-selects-reviewer'
    monkeypatch.setattr(qh.kernel, 'reviewer_status', lambda: {'schema': 'aru.reviewer-status/v3', 'valid': True, 'coding_reviewers': []})
    with pytest.raises(DriverError, match='no eligible independent reviewer'):
        quota_admission.evaluate(qh.config, qh.state, REPO, 'codex-one', task, 'implementation', qh.kernel)
