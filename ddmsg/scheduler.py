"""Native per-user launchd / Windows Task Scheduler, no credentials or elevation."""
import datetime as dt
import csv
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from .dws import executable


def label(config):
    return "local.ddmsg." + hashlib.sha256(str(Path(config).resolve()).encode()).hexdigest()[:12]


def command(config, mode, interval, python=None):
    script = Path(__file__).resolve().parent.parent / "ddmsg_cli.py"
    selected = python or sys.executable
    pythonw = Path(selected).with_name("pythonw.exe")
    if python is None and os.name == "nt" and pythonw.is_file():
        selected = str(pythonw)
    args = [selected, str(script), "--config", str(Path(config).resolve())]
    return args + (["listen", "--interval", str(interval)] if mode == "hybrid" else ["collect", "--max-seconds", str(min(180, interval-10))])


def launchd_definition(config, mode, interval, dws_binary, python=None):
    result = {"Label": label(config), "ProgramArguments": command(config, mode, interval, python),
        "WorkingDirectory": str(Path(config).resolve().parent), "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "DDMSG_DWS": dws_binary},
        "RunAtLoad": True, "ThrottleInterval": 30, "ExitTimeOut": 90}
    if mode == "hybrid":
        result["KeepAlive"] = True
    else:
        result["StartInterval"] = interval
    # Our rotating Python log is authoritative; no unbounded launchd stdout files.
    return result


def windows_definition(config, mode, interval, python=None, user=None, start=None):
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)
    def child(parent, tag, text=None):
        node = ET.SubElement(parent, "{" + ns + "}" + tag)
        node.text = text
        return node
    root = ET.Element("{" + ns + "}Task", {"version": "1.2"})
    triggers = child(root, "Triggers")
    # Standard Windows users can register their own recurring time trigger, but
    # importing an XML task with a LogonTrigger can require elevated rights.
    # StartWhenAvailable below resumes a missed recurring run after login.
    timed = child(triggers, "TimeTrigger")
    repeat = child(timed, "Repetition")
    child(repeat, "Interval", f"PT{interval}S")
    child(repeat, "StopAtDurationEnd", "false")
    child(timed, "StartBoundary", start or (dt.datetime.now()+dt.timedelta(minutes=1)).isoformat(timespec="seconds"))
    child(timed, "Enabled", "true")
    principals = child(root, "Principals")
    principal = child(principals, "Principal")
    principal.set("id", "Author")
    username = user
    if username is None and os.name == "nt":
        # schtasks XML imports for an InteractiveToken principal expect the
        # current account SID. A DOMAIN\user name can be rejected with
        # "Access is denied" even though that user can create normal tasks.
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            for field in next(csv.reader(result.stdout.splitlines()), []):
                if re.fullmatch(r"S-\d(?:-\d+)+", field.strip(), re.IGNORECASE):
                    username = field.strip()
                    break
    if username is None:
        username = ((os.environ.get("USERDOMAIN", "") + "\\" if os.environ.get("USERDOMAIN") else "") + getpass.getuser())
    child(principal, "UserId", username)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(root, "Settings")
    for key, value in {"MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
                       "StopIfGoingOnBatteries": "false", "StartWhenAvailable": "true", "Enabled": "true",
                       "ExecutionTimeLimit": "PT0S" if mode == "hybrid" else "PT240S"}.items():
        child(settings, key, value)
    actions = child(root, "Actions")
    actions.set("Context", "Author")
    action = child(actions, "Exec")
    args = command(config, mode, interval, python)
    child(action, "Command", args[0])
    child(action, "Arguments", subprocess.list2cmdline(args[1:]))
    child(action, "WorkingDirectory", str(Path(config).resolve().parent))
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


def run(args, allow_missing=False):
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if p.returncode and not allow_missing:
        raise RuntimeError((p.stdout+p.stderr)[-2000:])
    return p


def operate(app, action, mode="hybrid", interval=600, dry_run=False, platform=None):
    platform = platform or sys.platform
    name = label(app.config_path)
    state = app.directory / "scheduler.json"
    if action == "run-now":
        return app.collect()
    if platform not in ("darwin", "win32"):
        raise ValueError("Native scheduler supports macOS and Windows; use collect manually on this OS")
    path = Path.home()/"Library"/"LaunchAgents"/(name+".plist") if platform == "darwin" else app.directory/(name+".xml")
    saved = json.loads(state.read_text()) if state.exists() else {}
    if action == "status":
        p = run(["launchctl", "print", f"gui/{os.getuid()}/{name}"], True) if platform == "darwin" else run(["schtasks", "/Query", "/TN", name, "/XML"], True)
        native = {"registered": p.returncode == 0}
        if platform == "darwin" and p.returncode == 0:
            for key, pattern in {"state": r"\n\s*state = ([^\n]+)", "pid": r"\n\s*pid = (\d+)",
                                 "last_exit": r"\n\s*last exit code = ([^\n]+)"}.items():
                match = re.search(pattern, p.stdout)
                if match:
                    native[key] = match.group(1)
        return {"installed": p.returncode == 0, "name": name, "settings": saved,
                "native": native, "listener": app.store.get("listener")}
    if action == "install":
        binary = executable()
        definition = launchd_definition(app.config_path, mode, interval, binary) if platform == "darwin" else windows_definition(app.config_path, mode, interval)
        if dry_run:
            return {"name": name, "path": str(path), "mode": mode,
                    "definition": definition if isinstance(definition, dict) else definition.decode("utf-16")}
        if path.exists() and not state.exists():
            raise ValueError("Existing scheduler definition is not owned by this installation")
        # Use persisted native DWS path on Windows, where Task Scheduler has a reduced PATH.
        with app.db:
            app.store.set("dws_binary", binary)
        (app.directory / "listener.stop").unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = plistlib.dumps(definition) if isinstance(definition, dict) else definition
        if platform == "darwin":
            run(["launchctl", "bootout", f"gui/{os.getuid()}/{name}"], True)
        path.write_bytes(content)
        if platform == "darwin":
            run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
        else:
            run(["schtasks", "/Create", "/TN", name, "/XML", str(path), "/F"])
            run(["schtasks", "/Run", "/TN", name])
        saved = {"mode": mode, "interval_seconds": interval, "name": name, "definition_path": str(path), "config": str(app.config_path)}
        state.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        return {"installed": True, **saved}
    if action == "uninstall":
        if not state.exists():
            return {"installed": False, "note": "No managed scheduler installation"}
        if dry_run:
            return {"would_remove": saved}
        if platform == "darwin":
            run(["launchctl", "bootout", f"gui/{os.getuid()}/{name}"], True)
        else:
            # Signal the cooperative listener before asking Task Scheduler to remove future starts.
            (app.directory / "listener.stop").touch()
            run(["schtasks", "/Delete", "/TN", name, "/F"])
        path.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
        return {"installed": False, "name": name}
    raise ValueError("Unknown scheduler action")
