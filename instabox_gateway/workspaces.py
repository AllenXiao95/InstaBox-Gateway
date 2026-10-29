"""Explicit workspace context and mistake prevention, not a Shell sandbox."""
import json
from pathlib import Path
import posixpath
import secrets

from .errors import GatewayError
from .gateway import fields, require, text
RUNNER = Path(__file__).with_name("remote_runner.py").read_text(encoding="utf-8")


class Workspaces:
    def __init__(self, gateway):
        self.g = gateway

    def get(self, ident):
        value = self.g.store.get("workspace", text(ident, 100))
        require(value is not None, "workspace_not_found", 404)
        self.g._resolve({"bindingId": value["bindingId"]})
        return value

    def probe(self, workspace, **extra):
        if "cwd" in extra:
            text(extra["cwd"])
        result = self.g.execute({"target": {"bindingId": workspace["bindingId"]},
                                 "argv": ["python3", "-c", RUNNER, "probe"],
                                 "stdin": json.dumps(dict(root=workspace["remoteRoot"],
                                                          fileRoot=self.g.settings()["fileRoot"], **extra))})
        try:
            value = json.loads(result["result"]["stdout"])
        except (ValueError, KeyError):
            raise GatewayError("workspace_probe_failed", 502) from None
        require(result["outcome"] == "SUCCEEDED" and not result["result"]["truncated"]
                and "runnerError" not in value, "workspace_probe_failed", 409)
        return value

    def create(self, body):
        fields(body, ("name", "bindingId", "remoteRoot", "localRoot"))
        binding = self.g.store.get("binding", text(body.get("bindingId"), 100))
        require(binding is not None, "binding_not_found", 404)
        root = self.g.file_path(body.get("remoteRoot"))
        require(".igw" not in root.split("/"), "workspace_reserved_path")
        local = str(Path(text(body.get("localRoot"))).expanduser().resolve())
        require(Path(local).is_dir(), "local_workspace_missing")
        value = dict(id="w_" + secrets.token_hex(12), name=text(body.get("name"), 100),
                     bindingId=binding["id"], remoteRoot=root, localRoot=local)
        probe = self.probe(value, create=True)
        value["git"] = probe["git"]
        self.g.store.put("workspace", value["id"], value)
        return value

    def execute(self, ident, body, kind="exec"):
        workspace = self.get(ident)
        require("target" not in body, "workspace_target_conflict")
        if kind == "git":
            fields(body, ("argv",))
            require(body.get("argv") in (["status", "--porcelain=v1"], ["diff", "--stat"], ["rev-parse", "HEAD"]),
                    "git_readonly_arguments_required")
            probe = self.probe(workspace)
            require(probe["git"] is not None, "workspace_not_a_repository", 409)
            argv = ["python3", "-c", "import os,subprocess,sys; e={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}; sys.exit(subprocess.call(sys.argv[1:],env=e))",
                    "git", "-C", workspace["remoteRoot"], *body["argv"]]
            body = {"argv": argv}
        else:
            probe = self.probe(workspace, cwd=body.get("cwd", "."))
            require(not any(k.startswith("GIT_") for k in body.get("envs", {})), "git_environment_denied")
        result = self.g.execute({**body, "target": {"bindingId": workspace["bindingId"]}, "cwd": probe["cwd"]})
        result["workspaceId"] = ident
        result["location"] = "remote"
        result["gitBefore"] = probe["git"]
        result["gitAfter"] = self.probe(workspace)["git"]
        return result

    def file_path(self, ident, value):
        workspace = self.get(ident)
        value = text(value)
        require(not value.startswith("/") and posixpath.normpath(value) == value
                and all(p not in ("", ".", "..") for p in value.split("/")), "workspace_path_denied", 403)
        probe = self.probe(workspace, path=value)
        return {"bindingId": workspace["bindingId"]}, probe["path"]
