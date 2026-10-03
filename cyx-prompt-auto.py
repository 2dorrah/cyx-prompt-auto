#!/usr/bin/env python3
"""CYX Prompt Auto - interactive XMRig/RandomX controller.

Interactive controller for a locally installed XMRig executable. It manages a
small JSON config, launches/stops XMRig, reports status, and can generate
RandomX test jobs through the existing cyx-randomx-jobs-nanswap.py module.

It does not download software, alter system startup, or run hidden/background
persistence. Mining starts only after an explicit `start` command.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "cyx-xmrig-config.json"
STATE_PATH = APP_DIR / "cyx-prompt-state.json"
DEFAULT_POOL = "xmrig.nanswap.com:3333"
DEFAULT_ALGO = "rx/0"
DEFAULT_PASS = "x"

DEFAULT_CONFIG: dict[str, Any] = {
    "autosave": True,
    "cpu": True,
    "opencl": False,
    "cuda": False,
    "pools": [{
        "algo": DEFAULT_ALGO,
        "coin": "monero",
        "url": DEFAULT_POOL,
        "user": "",
        "pass": DEFAULT_PASS,
        "keepalive": True,
        "tls": False,
    }],
}


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        if not isinstance(value, dict):
            raise ValueError("JSON root must be an object")
        return value
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Config warning: {exc}; using defaults.")
        return json.loads(json.dumps(default))


def save_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def pool(cfg: dict[str, Any]) -> dict[str, Any]:
    pools = cfg.setdefault("pools", [])
    if not pools:
        pools.append(dict(DEFAULT_CONFIG["pools"][0]))
    return pools[0]


def find_xmrig(explicit: str | None = None) -> str | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend([
        APP_DIR / "xmrig",
        APP_DIR / "xmrig.exe",
        Path.cwd() / "xmrig",
        Path.cwd() / "xmrig.exe",
    ])
    for p in candidates:
        if p.is_file():
            return str(p)
    import shutil
    return shutil.which("xmrig") or shutil.which("xmrig.exe")


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status(cfg: dict[str, Any], proc: subprocess.Popen | None) -> None:
    p = pool(cfg)
    running = bool(proc and proc.poll() is None)
    pid = proc.pid if running and proc else None
    print(f"\nXMRig: {'RUNNING' if running else 'STOPPED'}" + (f" (PID {pid})" if pid else ""))
    print(f"Pool:  {p.get('url', '')}")
    print(f"Algo:  {p.get('algo', '')}")
    print(f"User:  {'set' if p.get('user') else 'NOT SET'}")
    print(f"TLS:   {bool(p.get('tls', False))}")
    print(f"Config: {CONFIG_PATH}")


def validate(cfg: dict[str, Any]) -> list[str]:
    errors = []
    p = pool(cfg)
    for key in ("url", "user", "pass"):
        if not str(p.get(key, "")).strip():
            errors.append(f"pool.{key} is empty")
    if not str(p.get("algo", "")).lower().startswith("rx"):
        errors.append("pool.algo must be a RandomX algorithm such as rx/0")
    return errors


def save_state(pid: int | None = None) -> None:
    save_json(STATE_PATH, {"pid": pid, "updated": int(time.time())})


def start(cfg: dict[str, Any], proc: subprocess.Popen | None, xmrig: str | None) -> subprocess.Popen | None:
    if proc and proc.poll() is None:
        print("Already running.")
        return proc
    errors = validate(cfg)
    if errors:
        print("Cannot start:")
        for e in errors:
            print(f"  - {e}")
        print("Use `wallet`, `pool`, or `set` to configure it first.")
        return None
    xmrig = find_xmrig(xmrig)
    if not xmrig:
        print("XMRig executable not found. Put xmrig/xmrig.exe beside this script or use `xmrig <path>`.")
        return None
    save_json(CONFIG_PATH, cfg)
    print(f"Starting {xmrig} with {CONFIG_PATH} ...")
    try:
        kwargs: dict[str, Any] = {"cwd": str(Path(xmrig).resolve().parent)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([xmrig, "--config", str(CONFIG_PATH)], **kwargs)
        save_state(proc.pid)
        print(f"Started PID {proc.pid}.")
        return proc
    except OSError as exc:
        print(f"Start failed: {exc}")
        return None


def stop(proc: subprocess.Popen | None) -> subprocess.Popen | None:
    if not proc or proc.poll() is not None:
        print("XMRig is not running from this session.")
        save_state(None)
        return None
    print(f"Stopping PID {proc.pid} ...")
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    save_state(None)
    print("Stopped.")
    return None


def set_value(cfg: dict[str, Any], key: str, value: str) -> None:
    p = pool(cfg)
    aliases = {"pool": "url", "wallet": "user", "password": "pass", "algorithm": "algo"}
    key = aliases.get(key.lower(), key.lower())
    if key not in {"url", "user", "pass", "algo", "tls", "keepalive"}:
        print("Settable keys: pool, wallet, password, algorithm, tls, keepalive")
        return
    if key in {"tls", "keepalive"}:
        value = value.lower() in {"1", "true", "yes", "on"}
    p[key] = value
    save_json(CONFIG_PATH, cfg)
    print(f"Set {key} = {p[key]}")


def generate_job() -> None:
    try:
        from importlib.util import spec_from_file_location, module_from_spec
        source = APP_DIR / "cyx-randomx-jobs-nanswap.py"
        if not source.exists():
            print(f"Missing {source}")
            return
        spec = spec_from_file_location("cyx_randomx_jobs_nanswap", source)
        if not spec or not spec.loader:
            raise RuntimeError("unable to load job module")
        mod = module_from_spec(spec)
        spec.loader.exec_module(mod)
        height = int(input("Block height [1]: ").strip() or "1")
        job = mod.RandomXJobGenerator().generate(height=height)
        print(json.dumps(job.notify_params(), indent=2))
    except Exception as exc:
        print(f"Job generation failed: {exc}")


def help_text() -> None:
    print("""
Commands:
  start                         Start XMRig using the managed config
  stop                          Stop XMRig started by this controller
  restart                       Stop then start XMRig
  status                        Show miner/config status
  pool [HOST:PORT]              Set pool endpoint
  wallet [ADDRESS]              Set payout/user address
  password [VALUE]              Set pool password
  algorithm [rx/0]              Set XMRig algorithm
  set KEY VALUE                 Set pool/wallet/password/algo/tls/keepalive
  tls on|off                    Toggle TLS in generated config
  config                        Print the current XMRig JSON config
  save                          Save current config
  job                           Generate a synthetic RandomX test job
  xmrig [PATH]                  Set the XMRig executable path for this session
  debug                         Print validation diagnostics
  help                          Show commands
  quit                          Exit (does not stop a running miner)

Prompts also accept natural aliases such as:
  "start mining", "stop mining", "show status", "configure pool",
  "set wallet", and "generate job".
""")


def main() -> int:
    global CONFIG_PATH
    ap = argparse.ArgumentParser(description="Prompt-driven XMRig/RandomX controller")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--xmrig", default=None, help="path to xmrig executable")
    args = ap.parse_args()

    CONFIG_PATH = Path(args.config).expanduser().resolve()
    cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    proc: subprocess.Popen | None = None
    xmrig = args.xmrig

    print("CYX Prompt Auto — type `help` for commands.")
    status(cfg, proc)
    while True:
        try:
            raw = input("\ncyx> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            print(f"Input error: {exc}")
            continue
        cmd = parts[0].lower()
        args2 = parts[1:]
        normalized = raw.lower()

        if cmd in {"quit", "exit", "q"}:
            break
        if cmd in {"help", "?"}:
            help_text(); continue
        if cmd == "start" or "start mining" in normalized:
            proc = start(cfg, proc, xmrig); continue
        if cmd == "stop" or "stop mining" in normalized:
            proc = stop(proc); continue
        if cmd == "restart":
            proc = stop(proc); proc = start(cfg, proc, xmrig); continue
        if cmd == "status" or "show status" in normalized:
            status(cfg, proc); continue
        if cmd == "config":
            print(json.dumps(cfg, indent=2)); continue
        if cmd == "save":
            save_json(CONFIG_PATH, cfg); print(f"Saved {CONFIG_PATH}"); continue
        if cmd == "job" or "generate job" in normalized:
            generate_job(); continue
        if cmd == "xmrig":
            if args2: xmrig = args2[0]
            print(f"XMRig path: {xmrig or find_xmrig() or 'not found'}"); continue
        if cmd in {"pool", "wallet", "password", "algorithm", "tls", "keepalive"}:
            key = "pool" if cmd == "pool" else cmd
            value = args2[0] if args2 else input(f"{key}: ").strip()
            if cmd in {"tls", "keepalive"} and not args2:
                value = input(f"{key} on/off: ").strip()
            set_value(cfg, key, value); continue
        if cmd == "set" and len(args2) >= 2:
            set_value(cfg, args2[0], " ".join(args2[1:])); continue
        if cmd == "debug":
            print("Validation:")
            errors = validate(cfg)
            print("  OK" if not errors else "  " + "\n  ".join(errors))
            print(f"XMRig discovered: {find_xmrig(xmrig) or 'NO'}")
            print(f"Config path: {CONFIG_PATH}")
            continue
        print("Unknown command. Type `help`.")

    print("Exiting controller. Any XMRig process already started is left running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
