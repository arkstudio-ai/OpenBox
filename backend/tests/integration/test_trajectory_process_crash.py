"""The hard-kill fixture always owns the exact processes and database it uses."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_sigkill_preserves_committed_tool_output_and_never_reexecutes(tmp_path):
    output=tmp_path/'crash-evidence.json'
    environment=dict(os.environ)
    # The ordinary suite always uses its own fresh SQLite file. PostgreSQL
    # verification runs the same standalone module with an explicit test DB.
    environment.pop('TRAJECTORY_CRASH_DATABASE_URL',None)
    completed=subprocess.run([sys.executable,'-m','trajectory.benchmark_process_crash','--output',str(output)],
        cwd=Path(__file__).parents[2],env=environment,capture_output=True,text=True,timeout=45)
    assert completed.returncode==0,completed.stdout+completed.stderr
    result=json.loads(output.read_text())
    assert result['kill_signal']=='SIGKILL' and result['exitcode']==-9
    assert result['killed_child_pid']!=result['recovery_pid']
    assert result['prefix_preserved'] and result['automatic_reexecution_count']==0
    assert result['old_tool_status']==result['old_run_status']=='unknown'
    assert result['new_input_tool_status']==result['new_input_run_status']=='completed'
