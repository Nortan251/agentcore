"""Finite subprocess fault worker: real native publication, synthetic UI/model."""

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1]))
from test_task_50b_workflow import Human, Model, run
from agentcore.config.schema import AgentConfig
from agentcore.permissions.config import PermissionConfig
from agentcore.permissions.levels import PermissionLevel
from agentcore.tools.builtin.write_file import WriteFileTool
from agentcore.workspace_host.workflow import SupervisedWorkspaceWorkflow
from test_fs_write import _publication_hook


root, private, stage = sys.argv[1:]
config = AgentConfig(workspace_dir=root, permissions=PermissionConfig(level=PermissionLevel.CONFIRM))
with SupervisedWorkspaceWorkflow(config=config, human=Human(), model=Model(), enable_write=True,
                                  _host_directory=Path(private)) as host:
    save = host._storage.save

    def interrupted_save():
        save()
        if host._record is not None and ((stage == "before_dispatch" and host._record["dispatched"])
                or (stage == "after_receipt" and host._record["phase"] == "verified")):
            os._exit(51)

    host._storage.save = interrupted_save
    if stage == "authorized":
        create = host._services.actions.create

        def authorized(record):
            create(record)
            os._exit(51)

        host._services.actions.create = authorized
    elif stage == "claimed":
        WriteFileTool.run = lambda *args: os._exit(51)
    elif stage == "published":
        class Patch:
            def setattr(self, obj, name, value):
                setattr(obj, name, value)

        def published(publish):
            publish()
            os._exit(51)

        _publication_hook(Patch(), published)
    elif stage in {"after_success", "after_read"}:
        invoke = host.executor.invoke

        def lost_return(name, *args, **kwargs):
            result = invoke(name, *args, **kwargs)
            if name == "write_file" and stage == "after_success" or (
                    name == "read_file" and stage == "after_read" and host._record["dispatched"]):
                os._exit(51)
            return result

        host.executor.invoke = lost_return
    run(host)
raise SystemExit("fault boundary not reached")
