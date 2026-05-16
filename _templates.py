import os
import re
import subprocess
from fastapi.templating import Jinja2Templates


def _semver(tag: str) -> tuple:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _get_version_info() -> dict:
    env_version = os.environ.get("APP_VERSION", "").strip()
    if env_version and env_version != "dev":
        return {"version": env_version, "latest": env_version, "outdated": False}

    try:
        local = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()

        remote_out = subprocess.check_output(
            ["git", "ls-remote", "--tags", "--refs", "origin"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()

        remote_tags = [
            line.split("\t")[1].replace("refs/tags/", "")
            for line in remote_out.splitlines()
            if "\t" in line and re.search(r"v?\d+\.\d+\.\d+", line.split("\t")[1])
        ]
        latest_remote = max(remote_tags, key=_semver) if remote_tags else local
        outdated = _semver(latest_remote) > _semver(local)

        return {"version": local, "latest": latest_remote, "outdated": outdated}
    except Exception:
        return {"version": "dev", "latest": "dev", "outdated": False}


_info = _get_version_info()

templates = Jinja2Templates(directory="templates", autoescape=True)
templates.env.globals["app_version"] = _info["version"]
templates.env.globals["app_version_latest"] = _info["latest"]
templates.env.globals["app_version_outdated"] = _info["outdated"]
