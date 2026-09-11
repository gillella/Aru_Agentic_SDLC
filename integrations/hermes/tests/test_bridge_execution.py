"""Real bridge, generated heartbeat and supervised child with private fixtures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from test_config import ROOT, write_config
from test_execution import wait_until
from aru_project_driver import execution, scheduler
from aru_project_driver.config import Config
from aru_project_driver.controller import Controller
from aru_project_driver.state import State


def test_real_bridge_scheduler_and_supervised_child(tmp_path, monkeypatch):
    config_path = write_config(tmp_path)
    raw = json.loads(config_path.read_text())
    native = tmp_path / "native.py"
    native.write_text('import os,pathlib\npathlib.Path("child-env").write_text(os.environ["DRIVER_FIXTURE"])\n')
    raw["lanes"]["agent-one"]["command"] = [sys.executable, str(native), "{prompt}"]
    config_path.write_text(json.dumps(raw))
    config, repo = Config(config_path), "gillella/repo"
    scripts = config.kernel_root / "scripts"
    shutil.copytree(ROOT / "scripts", scripts)
    with (scripts / "common.py").open("a") as stream:
        stream.write('\nrepo_root = lambda: Path.cwd()\ncheckout_repository = repo_slug = lambda: "gillella/repo"\n')
    (scripts / "fetch_next_work.py").write_text('def select(agent): return {"type":"idle", "agent":agent}\n')
    tree = Path(config.project(repo)["repo_dir"]) / ".worktrees" / "fixture"
    tree.mkdir(parents=True)
    direct = Controller(config).adapter(repo).next_work("agent-one")
    assert direct == {"type": "idle", "agent": "agent-one"}
    state = State(config.state_dir)
    state.save(repo, {**state.project(repo), "enabled": True})
    planning = ("Controller._plan = lambda c, r: dict(actions=[], resumes=[], free_lanes=[], blocked_lanes={}, "
                "reasons=[c.adapter(r).next_work('agent-one')], fingerprint='fixture', actionable=True)\n")
    namespace = {"Controller": Controller}
    original = Controller._plan
    exec(planning, namespace)
    fixture_plan, Controller._plan = Controller._plan, original
    monkeypatch.setattr(Controller, "_plan", fixture_plan)
    reconciled = Controller(config).reconcile(repo)
    assert reconciled["reasons"] == [direct] and not reconciled["launched"]
    entry = tmp_path / "offline-driver.py"
    entry.write_text(f'import sys,json\nsys.path.insert(0, {str(ROOT / "integrations/hermes")!r})\n'
                     'from aru_project_driver.config import Config\nfrom aru_project_driver.controller import Controller\n'
                     + planning + f'print(json.dumps(Controller(Config(sys.argv[2])).tick({repo!r})))\n')
    wrapper = scheduler._wrapper(config.hermes_home, repo, config.path, entry)
    result = subprocess.run([sys.executable, str(config.hermes_home / "scripts" / wrapper)],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["plan"]["reasons"] == reconciled["reasons"]
    monkeypatch.setenv("DRIVER_FIXTURE", "inherited")
    before = dict(os.environ)
    receipt = execution.launch(config, repo, "agent-one", 1, str(tree))
    wait_until(lambda: state.worker(receipt["id"]).get("state") == "exited")
    assert state.worker(receipt["id"])["exit_code"] == 0
    assert (tree / "child-env").read_text() == "inherited"
    assert dict(os.environ) == before
