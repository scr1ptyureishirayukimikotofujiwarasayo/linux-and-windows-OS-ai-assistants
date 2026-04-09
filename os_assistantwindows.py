"""
Local Windows OS Assistant
Supports: Ollama, LM Studio, OpenRouter, Groq, Google Gemini, Custom API
"""

import os
import re
import json
import time
import shutil
import base64
import getpass
import logging
import platform
import threading
import traceback
import subprocess
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, List, Any
from datetime import datetime

import requests
try:
    import win32crypt
except ImportError:
    print("  [Warning] pywin32 not installed - API keys will be stored insecurely")
    win32crypt = None


# ============================================================
# CONFIG
# ============================================================

OLLAMA_URL      = "http://localhost:11434/api"
LM_STUDIO_URL   = "http://localhost:1234/v1"

class AppState:
    """Centralized application state management"""
    def __init__(self):
        self.current_model = ""
        self.provider = "ollama"   # ollama | lmstudio | openrouter | groq | gemini | custom
        self.api_keys = {}

# Global state instance
state = AppState()

# Backward compatibility (to be gradually removed)
CURRENT_MODEL = ""
CURRENT_PROVIDER = "ollama"
API_KEY         = ""
API_BASE_URL    = ""
TIMEOUT         = 120
AUTO_APPROVE    = False

SAFE_DIR = os.path.expanduser("~/OllamaAssistant")
KEYS_FILE = os.path.join(SAFE_DIR, "keys.json")
LOG_FILE  = os.path.join(SAFE_DIR, "assistant.log")
MEMORY_FILE = os.path.join(SAFE_DIR, "memory.json")

ALLOWED_ROOTS = [
    os.path.expanduser("~"),
    os.path.join(os.environ.get("TEMP", ""), "OllamaAssistant"),
    "C:\\Users",
]

MAX_HISTORY    = 30
MAX_TOOL_CHARS = 3000

os.makedirs(SAFE_DIR, exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

def audit(event: str, detail: str = ""):
    logging.info(f"{event} | {detail}")


# ============================================================
# PROVIDER INFO
# ============================================================

PROVIDERS = {
    "openrouter": {
        "name": "OpenRouter",
        "url": "https://openrouter.ai/api/v1",
        "models_url": "https://openrouter.ai/api/v1/models",
        "key_url": "https://openrouter.ai/keys",
        "hint": "Hundreds of models. Free tier available.",
    },
    "groq": {
        "name": "Groq",
        "url": "https://api.groq.com/openai/v1",
        "models_url": "https://api.groq.com/openai/v1/models",
        "key_url": "https://console.groq.com/keys",
        "hint": "Very fast inference. Free tier.",
    },
    "gemini": {
        "name": "Google Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "key_url": "https://aistudio.google.com/apikey",
        "hint": "Gemini 2.5 Flash/Pro. Free tier available.",
    },
    "custom": {
        "name": "Custom API",
        "url": "",
        "models_url": "",
        "key_url": "",
        "hint": "Any OpenAI-compatible endpoint.",
    },
}


# ============================================================
# ADMIN
# ============================================================

def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    import ctypes
    script = os.path.abspath(__file__)
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "python", f'"{script}"', None, 1)
    if ret <= 32:
        print("\n  ⚠️  UAC elevation was cancelled or failed.")
        return False
    return True


def choose_admin_mode() -> bool:
    if is_admin():
        print("  ✅ Running as Administrator.\n")
        return True
    print("\n  Run as Administrator?")
    print("  (Required for: service control, UAC-protected paths, system tasks)")
    print("  [1] Yes — relaunch with admin privileges")
    print("  [2] No  — continue without admin\n")
    while True:
        try:
            choice = input("  Choose [1/2]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if choice == "1":
            launched = relaunch_as_admin()
            if launched:
                raise SystemExit(0)
            return True
        elif choice == "2":
            print("  Continuing without admin.\n")
            return True
        print("  Please enter 1 or 2.")


# ============================================================
# API KEY MANAGEMENT
# ============================================================

def _encode(key: str) -> str:
    if not key:
        return ""
    if win32crypt:
        try:
            encrypted = win32crypt.CryptProtectData(key.encode(), None, None, None, None, 0)
            return base64.b64encode(encrypted).decode()
        except Exception:
            pass
    # Fallback to base64 if DPAPI fails
    return base64.b64encode(key.encode()).decode()

def _decode(enc: str) -> str:
    if not enc:
        return ""
    if win32crypt:
        try:
            data = base64.b64decode(enc)
            decrypted = win32crypt.CryptUnprotectData(data, None, None, None, 0)
            return decrypted[1].decode()
        except Exception:
            pass
    # Fallback to base64 if DPAPI fails
    try:
        return base64.b64decode(enc).decode()
    except Exception:
        return enc

def load_keys() -> Dict[str, str]:
    try:
        if os.path.isfile(KEYS_FILE):
            with open(KEYS_FILE, "r", encoding="utf-8") as f:
                return {k: _decode(v) for k, v in json.load(f).items()}
    except Exception:
        pass
    return {}

def save_keys(keys: Dict[str, str]):
    try:
        os.makedirs(os.path.dirname(KEYS_FILE), exist_ok=True)
        with open(KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump({k: _encode(v) for k, v in keys.items() if v}, f, indent=2)
        try:
            os.chmod(KEYS_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"  [Error] Could not save keys: {e}")

def get_key(provider: str) -> str:
    return load_keys().get(provider, "")

def set_key(provider: str, key: str):
    keys = load_keys()
    if key:
        keys[provider] = key
    else:
        keys.pop(provider, None)
    save_keys(keys)


# ============================================================
# MEMORY MANAGEMENT
# ============================================================

def load_memory() -> Dict:
    try:
        if os.path.isfile(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_memory(mem: Dict):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, indent=2)
    except Exception:
        pass

def remember(key: str, value: Any):
    mem = load_memory()
    mem[key] = value
    save_memory(mem)




# ============================================================
# TASK PLANNER
# ============================================================

def plan_task(user_input: str, messages: List[Dict]) -> tuple:
    planning_prompt = {
        "role": "system",
        "content": """You are a task planner for a Windows OS assistant.

Break the task into steps using ONLY these exact tool names:
  create_folder(path)
  create_file(path, content)
  delete_file(path)
  list_directory(path)
  read_file(path)
  search_files(root, pattern)
  run_powershell(command)
  get_system_info(query)
  web_search(query)
  create_shortcut(target, shortcut)
  run_as_admin(path)
  uninstall_program(name)

Return JSON ONLY in this exact format:
{"steps":[{"tool":"create_folder","args":{"path":"C:\\Users\\user\\Desktop\\myfolder"}}]}

If no tools needed, return:
{"steps":[]}

Rules:
- Use ONLY the tool names listed above
- Always use full absolute Windows paths
- No text outside the JSON
"""
    }

    msgs = [planning_prompt, {"role": "user", "content": user_input}]
    response = call_ai(msgs)

    # Parse planner response directly - it returns {"steps":[...]} not a tool call
    response = response.strip()

    # Guard: if AI call failed entirely, report clearly
    if response.startswith("[Error]") or response == "[Cancelled]":
        print(f"  [Planner Error] AI call failed: {response}")
        return [], False

    # Strip markdown code fences if present
    if "```" in response:
        response = re.sub(r"```(?:json)?", "", response).replace("```", "").strip()

    # Find the JSON block
    try:
        start = response.index("{")
        end = response.rindex("}") + 1
        raw = response[start:end]
        raw = re.sub(r",(\s*[}\]])", r"\1", raw)  # fix trailing commas
        tool_data = json.loads(raw)
        if isinstance(tool_data, dict) and "steps" in tool_data and isinstance(tool_data["steps"], list):
            return tool_data["steps"], True
        # Model returned valid JSON but wrong format - print it to help debug
        print(f"  [Planner Error] Unexpected JSON structure: {list(tool_data.keys())}")
    except json.JSONDecodeError as e:
        print(f"  [Planner Error] JSON decode failed: {e}")
    except ValueError:
        print(f"  [Planner Error] No JSON found in response: {response[:100]}")

    return [], False


# ============================================================
# TOOL COOLDOWN SYSTEM
# ============================================================

# Global tracking for tool cooldowns
_tool_history = []
_tool_cooldown_time = 30  # seconds
_max_history_size = 20

def check_tool_cooldown(tool_name: str, args: Dict) -> bool:
    """Check if tool should be blocked due to recent opposite operations"""
    global _tool_history
    current_time = time.time()
    
    # Clean old entries
    _tool_history = [(t, a, ts) for t, a, ts in _tool_history 
                     if current_time - ts < _tool_cooldown_time]
    
    # Check for opposite operations
    opposite_pairs = {
        "create_file": "delete_file",
        "create_folder": "delete_folder", 
        "delete_file": "create_file",
        "delete_folder": "create_folder"
    }
    
    opposite_tool = opposite_pairs.get(tool_name)
    if opposite_tool:
        for hist_tool, hist_args, hist_time in _tool_history:
            if (hist_tool == opposite_tool and 
                current_time - hist_time < _tool_cooldown_time):
                # Check if they're operating on the same path
                path = args.get("path", "")
                hist_path = hist_args.get("path", "")
                if path and hist_path and (path == hist_path or 
                                         os.path.dirname(path) == os.path.dirname(hist_path)):
                    return True  # Block this operation
    
    return False

def record_tool_execution(tool_name: str, args: Dict):
    """Record tool execution for cooldown tracking"""
    global _tool_history
    _tool_history.append((tool_name, args.copy(), time.time()))
    
    # Limit history size
    if len(_tool_history) > _max_history_size:
        _tool_history = _tool_history[-_max_history_size:]


# ============================================================
# TOOL SCHEMAS
# ============================================================

TOOL_SCHEMAS = {
    "list_directory": {"path": str},
    "read_file": {"path": str},
    "create_folder": {"path": str},          # name is optional
    "create_file": {"path": str},            # content is optional
    "delete_file": {"path": str},
    "search_files": {"root": str, "pattern": str},  # max_results is optional
    "run_powershell": {"command": str},
    "get_system_info": {"query": str},
    "run_as_admin": {"path": str},           # args is optional
    "create_shortcut": {"target": str, "shortcut": str},  # description is optional
    "uninstall_program": {"name": str},
    "web_search": {"query": str},            # max_results is optional
    "list_models": {},
    "set_model": {"name": str},
    "revert": {}
}

# Tool-specific timeouts (seconds)
TOOL_TIMEOUTS = {
    "list_directory": 10,
    "read_file": 15,
    "create_folder": 10,
    "create_file": 10,
    "delete_file": 10,
    "search_files": 30,
    "run_powershell": 60,
    "get_system_info": 20,
    "run_as_admin": 120,
    "create_shortcut": 10,
    "uninstall_program": 180,
    "web_search": 30,
    "list_models": 15,
    "set_model": 5,
    "revert": 5
}

def validate_args(tool: str, args: Dict) -> bool:
    """Validate tool arguments against schema"""
    schema = TOOL_SCHEMAS.get(tool, {})
    for k, expected_type in schema.items():
        if k not in args:
            return False
        if not isinstance(args[k], expected_type):
            return False
    return True

# ============================================================
# STEP VALIDATION
# ============================================================

def validate_step(step: Dict) -> bool:
    """Validate tool step before execution"""
    if not isinstance(step, dict):
        return False
    if "tool" not in step or "args" not in step:
        return False
    if not isinstance(step["tool"], str):
        return False
    if step["tool"] not in TOOL_RISK:
        return False
    if not isinstance(step["args"], dict):
        return False
    if not validate_args(step["tool"], step["args"]):
        return False
    return True


# ============================================================
# SELF-HEALING EXECUTION
# ============================================================

def execute_with_retry(tool: str, args: Dict, messages: List[Dict], max_attempts=2) -> str:
    result = "[Error] Execution failed."
    for _ in range(max_attempts):
        result = safe_execute(tool, args)

        if not result.startswith("[Error]"):
            return result

        messages.append({
            "role": "user",
            "content": f"""
Tool failed.
Tool: {tool}
Args: {json.dumps(args)}
Error: {result}

Fix and return corrected tool call JSON.
"""
        })

        messages[:] = trim_history(messages)
        response = call_ai(messages)
        fix = extract_tool_call(response)

        if not fix:
            break

        tool = fix.get("tool", tool)
        args = fix.get("args", args)

    return result


# ============================================================
# ARGUMENT ENRICHMENT
# ============================================================

def enrich_args(tool: str, args: Dict, ctx: Dict) -> Dict:
    if tool == "create_folder":
        if not args.get("path"):
            args["path"] = ctx.get("desktop", str(Path.home()))
    if tool == "create_file":
        if not args.get("path"):
            args["path"] = os.path.join(ctx.get("desktop", ""), "new_file.txt")
        if not args.get("content"):
            args["content"] = "# Auto-generated file\n"
    return args


# ============================================================
# PROVIDER SELECTION
# ============================================================

def is_ollama_up() -> bool:
    try:
        return requests.get(f"{OLLAMA_URL}/tags", timeout=3).status_code == 200
    except Exception:
        return False

def is_lmstudio_up() -> bool:
    try:
        return requests.get(f"{LM_STUDIO_URL}/models", timeout=3).status_code == 200
    except Exception:
        return False

def find_ollama_exe() -> Optional[str]:
    found = shutil.which("ollama")
    if found:
        return found
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Ollama", "ollama.exe"),
    ]
    return next((p for p in candidates if p and os.path.isfile(p)), None)

def ensure_ollama() -> bool:
    if is_ollama_up():
        return True
    print("  Ollama not running. Searching...", end=" ", flush=True)
    exe = find_ollama_exe()
    if not exe:
        print("not found.\n  Install from: https://ollama.com/download\n")
        return False
    print("found. Starting...", end=" ", flush=True)
    try:
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        print(f"failed: {e}")
        return False
    deadline = time.time() + 25
    while time.time() < deadline:
        if is_ollama_up():
            print("ready.")
            return True
        time.sleep(0.5)
    print("timed out. Run 'ollama serve' manually.")
    return False

def list_cloud_models(provider: str, key: str, base_url: str = "") -> List[str]:
    info = PROVIDERS.get(provider, {})
    if provider == "gemini":
        try:
            r = requests.get(f"{info['models_url']}?key={key}", timeout=10)
            r.raise_for_status()
            return sorted(
                m["name"].split("/", 1)[1]
                for m in r.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
                and "gemini" in m.get("name", "")
            )
        except Exception:
            return ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
    url = base_url or info.get("models_url", "")
    if not url:
        return []
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "data" in data:
            return sorted(m["id"] for m in data["data"] if "id" in m)
    except Exception:
        pass
    return []

def choose_provider() -> bool:
    global CURRENT_PROVIDER, API_KEY, API_BASE_URL, CURRENT_MODEL

    print("\n  Choose AI provider:\n")
    ollama_up = is_ollama_up()
    lmstudio_up = is_lmstudio_up()
    print(f"  [1] Ollama      {'✅ running' if ollama_up else '(not detected)'}")
    print(f"  [2] LM Studio   {'✅ running' if lmstudio_up else '(not detected)'}")
    for i, (pid, info) in enumerate(PROVIDERS.items(), 3):
        saved = get_key(pid)
        note = f"key saved (…{saved[-6:]})" if saved else "no key saved"
        print(f"  [{i}] {info['name']:<14} {info['hint']}")
        print(f"       {'':<14} {note}")
    print()

    max_choice = 2 + len(PROVIDERS)
    while True:
        try:
            choice = input(f"  Choose [1-{max_choice}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return False

        if choice == "1":
            CURRENT_PROVIDER = "ollama"
            if not ensure_ollama():
                return False
            print("\n  ✅ Provider: Ollama\n")
            return True

        elif choice == "2":
            CURRENT_PROVIDER = "lmstudio"
            if not lmstudio_up:
                print("\n  Start LM Studio server first then press Enter.")
                try:
                    input("  Press Enter when ready...")
                except Exception:
                    return False
                if not is_lmstudio_up():
                    print("  Still not reachable.\n")
                    return False
            print("\n  ✅ Provider: LM Studio\n")
            return True

        elif choice.isdigit() and 3 <= int(choice) <= max_choice:
            pid, info = list(PROVIDERS.items())[int(choice) - 3]
            CURRENT_PROVIDER = pid

            if pid == "custom":
                try:
                    API_BASE_URL = input("  Base URL (e.g. https://api.together.xyz/v1): ").strip()
                except (EOFError, KeyboardInterrupt):
                    return False
                if not API_BASE_URL:
                    continue

            saved = get_key(pid)
            if saved:
                print(f"  Saved key found (…{saved[-6:]}). Press Enter to keep or paste new one.")
            try:
                raw = input("  API key: ").strip()
            except (EOFError, KeyboardInterrupt):
                return False
            key = raw if raw else saved
            if not key:
                print("  No key entered.")
                continue

            print("  Verifying...", end=" ", flush=True)
            models = list_cloud_models(pid, key, base_url=API_BASE_URL if pid == "custom" else "")
            print(f"OK — {len(models)} models." if models else "could not fetch models.")

            API_KEY = key
            set_key(pid, key)
            if models and not CURRENT_MODEL:
                CURRENT_MODEL = models[0]
            print(f"\n  ✅ Provider: {info['name']}\n")
            return True

        print(f"  Enter 1-{max_choice}.")


# ============================================================
# AI CALL
# ============================================================

_cancel = threading.Event()

def call_ai(messages: List[Dict], model: str = "") -> str:
    global CURRENT_MODEL
    use_model = model or CURRENT_MODEL
    _cancel.clear()
    result_box = [None]

    def _req():
        try:
            payload = {"model": use_model, "messages": messages, "stream": False}
            if CURRENT_PROVIDER == "lmstudio":
                r = requests.post(f"{LM_STUDIO_URL}/chat/completions", json=payload, timeout=TIMEOUT)
                r.raise_for_status()
                result_box[0] = r.json()["choices"][0]["message"]["content"]

            elif CURRENT_PROVIDER in PROVIDERS:
                info = PROVIDERS.get(CURRENT_PROVIDER, {})
                base = API_BASE_URL if CURRENT_PROVIDER == "custom" else info.get("url", "")
                url  = f"{base}/chat/completions"
                hdrs = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
                if CURRENT_PROVIDER == "openrouter":
                    hdrs["HTTP-Referer"] = "https://github.com/local-os-assistant"
                    hdrs["X-Title"] = "Local OS Assistant"
                # Gemini doesn't accept stream:false
                if CURRENT_PROVIDER == "gemini":
                    payload = {k: v for k, v in payload.items() if k != "stream"}
                for attempt in range(3):
                    r = requests.post(url, json=payload, headers=hdrs, timeout=TIMEOUT)
                    if r.status_code == 429:
                        time.sleep(5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    result_box[0] = r.json()["choices"][0]["message"]["content"]
                    break
                else:
                    result_box[0] = "[Error] Rate limited — wait a moment and retry."

            else:  # ollama
                for attempt in range(3):
                    r = requests.post(f"{OLLAMA_URL}/chat", json=payload, timeout=TIMEOUT)
                    if r.status_code == 429:
                        time.sleep(5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    result_box[0] = r.json()["message"]["content"]
                    break
                else:
                    result_box[0] = "[Error] Rate limited — wait a moment and retry."

        except Exception as e:
            result_box[0] = f"[Error] {e}"

    t = threading.Thread(target=_req, daemon=True)
    t.start()
    print("  [Thinking... Ctrl+C to cancel]", end="\r", flush=True)
    try:
        while t.is_alive():
            t.join(timeout=0.25)
            if _cancel.is_set():
                return "[Cancelled]"
    except KeyboardInterrupt:
        _cancel.set()
        print("\n  [Cancelled]")
        return "[Cancelled]"
    print(" " * 40, end="\r")  # clear the thinking line
    return result_box[0] or "[Error] No response."


# ============================================================
# MODEL LISTING & SWITCHING
# ============================================================

def list_models() -> List[str]:
    try:
        if CURRENT_PROVIDER == "lmstudio":
            r = requests.get(f"{LM_STUDIO_URL}/models", timeout=5)
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        elif CURRENT_PROVIDER in PROVIDERS:
            return list_cloud_models(CURRENT_PROVIDER, API_KEY, API_BASE_URL)
        else:
            r = requests.get(f"{OLLAMA_URL}/tags", timeout=10)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
    except Exception as e:
        print(f"  [Error listing models] {e}")
        return []

def switch_model():
    global CURRENT_MODEL
    models = list_models()
    if not models:
        print("  No models found.\n")
        return
    print("\n  Available models:")
    for i, m in enumerate(models, 1):
        mark = " ← current" if m == CURRENT_MODEL else ""
        print(f"  [{i:2d}] {m}{mark}")
    print("  [ 0] Cancel\n")
    try:
        choice = input("  Select model: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if choice == "0" or not choice:
        return
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(models):
            selected_model = models[idx]
            # Validate model exists by checking list_models again
            current_models = list_models()
            if selected_model in current_models:
                CURRENT_MODEL = selected_model
                audit("set_model", CURRENT_MODEL)
                print(f"\n  ✅ Switched to: {CURRENT_MODEL}\n")
            else:
                print(f"\n  Error: Model '{selected_model}' is no longer available.\n")
        else:
            print("  Invalid selection.\n")
    except ValueError:
        print("  Invalid selection.\n")


# ============================================================
# PATH SAFETY
# ============================================================

def is_path_allowed(path: str) -> bool:
    try:
        p = os.path.normpath(os.path.abspath(path))
        if os.name == "nt":
            p = p.lower()
            roots = [os.path.normpath(os.path.abspath(r)).lower() for r in ALLOWED_ROOTS if r]
        else:
            roots = [os.path.normpath(os.path.abspath(r)) for r in ALLOWED_ROOTS if r]
        return any(p.startswith(root) for root in roots)
    except Exception:
        return False

def path_guard(path: str) -> Optional[str]:
    """Return error string if path is not allowed, else None."""
    if not is_path_allowed(path):
        return f"[Error] Path '{path}' is outside allowed directories."
    return None


# ============================================================
# POWERSHELL RUNNER
# ============================================================

def run_ps(command: str, timeout: int = 60) -> str:
    try:
        encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace"
        )
    except Exception as e:
        return f"[Error] Failed to start PowerShell: {e}"

    deadline = time.time() + timeout
    try:
        while proc.poll() is None:
            if _cancel.is_set():
                proc.kill()
                return "[Cancelled]"
            if time.time() > deadline:
                proc.kill()
                return "[Error] Command timed out."
            time.sleep(0.2)
    except KeyboardInterrupt:
        _cancel.set()
        proc.kill()
        return "[Cancelled]"

    out, err = proc.communicate()
    result = (out + err).strip()
    if len(result) > MAX_TOOL_CHARS:
        result = result[:MAX_TOOL_CHARS] + "\n... [truncated]"
    return result


# ============================================================
# SYSTEM INFO QUERIES
# ============================================================

SYSINFO_QUERIES = {
    "cpu":       "Get-CimInstance Win32_Processor | Select Name,LoadPercentage | Format-List | Out-String",
    "memory":    "$o=Get-CimInstance Win32_OperatingSystem; 'Total: {0:N0} MB  Free: {1:N0} MB' -f ($o.TotalVisibleMemorySize/1KB),($o.FreePhysicalMemory/1KB)",
    "disk":      "Get-PSDrive -PSProvider FileSystem | Select Name,@{N='Free(GB)';E={[math]::Round($_.Free/1GB,2)}},@{N='Total(GB)';E={[math]::Round(($_.Free+$_.Used)/1GB,2)}} | Format-Table -AutoSize | Out-String",
    "processes": "Get-Process | Sort-Object CPU -Descending | Select-Object -First 20 Name,Id,@{N='CPU(s)';E={[math]::Round($_.CPU,1)}},@{N='Mem(MB)';E={[math]::Round($_.WorkingSet/1MB,1)}} | Format-Table -AutoSize | Out-String",
    "network":   "Get-NetAdapter | Where Status -eq Up | Select Name,LinkSpeed,MacAddress | Format-Table -AutoSize | Out-String",
    "startup":   "Get-CimInstance Win32_StartupCommand | Select Name,Command,Location | Format-Table -AutoSize | Out-String",
    "services":  "Get-Service | Where Status -eq Running | Select Name,DisplayName | Format-Table -AutoSize | Out-String",
    "updates":   "Get-HotFix | Sort InstalledOn -Descending | Select-Object -First 10 HotFixID,InstalledOn,Description | Format-Table -AutoSize | Out-String",
    "info":      "Get-CimInstance Win32_OperatingSystem | Select Caption,Version,BuildNumber,OSArchitecture,LastBootUpTime | Format-List | Out-String",
}


# ============================================================
# TOOLS
# ============================================================

# Track actions for undo
_action_history: List[Dict] = []

def _record(action_type: str, path: str, backup: str = None):
    _action_history.append({"type": action_type, "path": path, "backup": backup})
    if len(_action_history) > 20:
        _action_history.pop(0)


def tool_list_directory(path: str) -> str:
    path = os.path.abspath(path)
    err = path_guard(path)
    if err:
        return err
    try:
        entries = sorted(os.listdir(path))
        if not entries:
            return "(empty directory)"
        lines = []
        for e in entries:
            full = os.path.join(path, e)
            try:
                tag = "[DIR] " if os.path.isdir(full) else "[FILE]"
                size = "" if os.path.isdir(full) else f"  ({os.path.getsize(full):,} bytes)"
                lines.append(f"{tag} {e}{size}")
            except Exception:
                lines.append(f"      {e}")
        audit("list_directory", path)
        return "\n".join(lines)
    except Exception as e:
        return f"[Error] {e}"


def tool_read_file(path: str) -> str:
    path = os.path.abspath(path)
    err = path_guard(path)
    if err:
        return err
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > MAX_TOOL_CHARS:
            content = content[:MAX_TOOL_CHARS] + f"\n...[truncated at {MAX_TOOL_CHARS} chars]"
        audit("read_file", path)
        return content
    except Exception as e:
        return f"[Error] {e}"


def tool_create_folder(path: str, name: str = None) -> str:
    # If only a name (no slashes), assume Desktop
    p = path.strip()
    if name:
        name = name.strip()
    # Expand ~ and env vars
    p = os.path.expanduser(os.path.expandvars(p))
    
    # Determine final path
    if name:
        # Check if path already ends with the name to avoid duplication
        if os.path.basename(p) == name:
            final = os.path.normpath(p)
        else:
            final = os.path.normpath(os.path.join(p, name))
    else:
        final = os.path.normpath(p)
    
    err = path_guard(final)
    if err:
        return err
    try:
        os.makedirs(final, exist_ok=True)
        _record("create_folder", final)
        audit("create_folder", final)
        return f"✓ Folder created: {final}"
    except PermissionError:
        return f"[Error] Permission denied: {final}"
    except Exception as e:
        return f"[Error] {e}"


def tool_create_file(path: str, content: str = "") -> str:
    path = os.path.abspath(os.path.expanduser(path))
    err = path_guard(path)
    if err:
        return err
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        _record("create_file", path)
        audit("create_file", path)
        return f"✓ File created: {path}"
    except PermissionError:
        return f"[Error] Permission denied: {path}"
    except Exception as e:
        return f"[Error] {e}"


def tool_delete_file(path: str) -> str:
    path = os.path.abspath(path)
    err = path_guard(path)
    if err:
        return err
    if not os.path.exists(path):
        return f"[Error] Path does not exist: {path}"
    try:
        if os.path.isfile(path):
            os.remove(path)
            _record("delete_file", path)
            audit("delete_file", path)
            return f"✓ File deleted: {path}"
        else:
            shutil.rmtree(path)
            _record("delete_folder", path)
            audit("delete_folder", path)
            return f"✓ Folder deleted: {path}"
    except PermissionError:
        return f"[Error] Permission denied: {path}"
    except Exception as e:
        return f"[Error] {e}"


def tool_search_files(root: str, pattern: str, max_results: int = 20) -> str:
    root = os.path.abspath(root)
    err = path_guard(root)
    if err:
        return err
    matches = []
    skip_dirs = {"System Volume Information", "$Recycle.Bin", "WindowsApps"}
    start_time = time.time()
    
    for dirpath, dirnames, filenames in os.walk(root):
        if _cancel.is_set():
            return "[Cancelled]"

        if time.time() - start_time > 10:
            return "[Error] Search timed out."
        
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in skip_dirs]
        for fname in filenames:
            if pattern.lower() in fname.lower():
                full = os.path.join(dirpath, fname)
                try:
                    matches.append(f"{full}  ({os.path.getsize(full):,} bytes)")
                except Exception:
                    matches.append(full)
            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break
    audit("search_files", f"root={root} pattern={pattern} found={len(matches)}")
    if not matches:
        return f"No files matching '{pattern}' under {root}"
    suffix = f"\n(showing first {max_results})" if len(matches) == max_results else ""
    return "\n".join(matches) + suffix


def tool_run_powershell(command: str) -> str:
    audit("run_powershell", command[:200])
    return run_ps(command, timeout=60)


def tool_get_system_info(query: str) -> str:
    query = query.lower().strip()
    if query not in SYSINFO_QUERIES:
        valid = ", ".join(SYSINFO_QUERIES)
        return f"[Error] Unknown query. Valid options: {valid}"
    result = run_ps(SYSINFO_QUERIES[query], timeout=30)
    audit("get_system_info", query)
    return result or "(no results)"


def tool_revert() -> str:
    if not _action_history:
        return "Nothing to revert."
    last = _action_history[-1]
    t, path = last["type"], last["path"]
    try:
        if t in ("create_file", "create_folder"):
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            _action_history.pop()
            audit("revert", path)
            return f"✓ Reverted: removed '{path}'"
        return f"[Info] Revert not supported for action type '{t}'."
    except Exception as e:
        return f"[Error] Revert failed: {e}"


def tool_run_as_admin(path: str, args_str: str = "") -> str:
    if not os.path.exists(path):
        return f"[Error] Path does not exist: {path}"
    ext = os.path.splitext(path)[1].lower()
    allowed_exts = {".exe", ".msi", ".bat", ".cmd", ".ps1"}
    if ext not in allowed_exts:
        return f"[Error] '{ext}' is not an executable type."
    if ext == ".ps1":
        cmd = f'Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File \\"{path}\\"" -Verb RunAs'
    elif args_str:
        cmd = f'Start-Process -FilePath "{path}" -ArgumentList "{args_str}" -Verb RunAs'
    else:
        cmd = f'Start-Process -FilePath "{path}" -Verb RunAs'
    out = run_ps(cmd, timeout=15)
    audit("run_as_admin", f"path={path}")
    return out if out.strip() else f"'{os.path.basename(path)}' launched as admin."


def tool_create_shortcut(target: str, shortcut: str, description: str = "") -> str:
    if not shortcut.lower().endswith(".lnk"):
        shortcut += ".lnk"
    
    # Use absolute path for consistency
    shortcut_abs = os.path.abspath(shortcut)
    err = path_guard(shortcut_abs)
    if err:
        return err
    
    def esc(s):
        return s.replace("'", "''")
    ps = (
        f"$ws=New-Object -ComObject WScript.Shell\n"
        f"$sc=$ws.CreateShortcut('{esc(shortcut_abs)}')\n"
        f"$sc.TargetPath='{esc(target)}'\n"
        f"$sc.WorkingDirectory='{esc(os.path.dirname(target))}'\n"
    )
    if description:
        ps += f"$sc.Description='{esc(description)}'\n"
    ps += "$sc.Save()"
    run_ps(ps, timeout=10)
    if os.path.isfile(shortcut_abs):
        audit("create_shortcut", shortcut_abs)
        _record("create_file", shortcut_abs)
        return f"✓ Shortcut created: {shortcut_abs}"
    return "[Error] Shortcut not created."


def tool_uninstall_program(name: str) -> str:
    # Check if running as administrator
    if not is_admin():
        return "[Error] Uninstall requires administrator privileges. Please run the assistant as admin."
    
    ps_find = (
        f"Get-ItemProperty "
        f"HKLM:\\Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*,"
        f"HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*,"
        f"HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* "
        f"-ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.DisplayName -like '*{name}*' }} | "
        f"Select-Object DisplayName,UninstallString,QuietUninstallString | ConvertTo-Json -Depth 2"
    )
    result = run_ps(ps_find, timeout=20)
    if not result.strip() or result.strip() == "null":
        out = run_ps(f'winget uninstall --name "{name}" --silent --accept-source-agreements', timeout=120)
        return "Registry match not found. Tried winget:\n" + out
    try:
        data = json.loads(result)
    except Exception:
        return f"[Error] Could not parse registry data:\n{result}"
    entries = data if isinstance(data, list) else [data]
    if not entries:
        return f"No installed program matching '{name}' found."
    entry = entries[0]
    disp = entry.get("DisplayName", name)
    quiet = entry.get("QuietUninstallString", "")
    uninst = entry.get("UninstallString", "")
    audit("uninstall_program", disp)
    msi = re.search(r"\{[0-9A-Fa-f\-]{36}\}", uninst or "")
    if msi:
        # Use direct msiexec call without -Verb RunAs since we're already admin
        out = run_ps(f'msiexec /x {msi.group(0)} /qn /norestart', timeout=180)
        return f"Uninstalled '{disp}' (MSI).\n{out}"
    if quiet:
        # Use direct execution without -Verb RunAs
        out = run_ps(f'cmd /c "{quiet}"', timeout=180)
        return f"Uninstalled '{disp}'.\n{out}"
    if uninst:
        if not any(f in uninst.upper() for f in ["/S", "/SILENT", "/QUIET", "/Q"]):
            uninst += " /S"
        # Use direct execution without -Verb RunAs
        out = run_ps(f'cmd /c "{uninst}"', timeout=180)
        return f"Uninstalled '{disp}'.\n{out}"
    return f"Found '{disp}' but no uninstall method available."


def tool_web_search(query: str, max_results: int = 5) -> str:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.5",
        }
        encoded = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}"
        r = requests.get(url, headers=headers, timeout=15)
        html = r.text
        results = []
        matches = re.findall(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            html, re.DOTALL | re.IGNORECASE
        )
        for href, title, snippet in matches[:max_results]:
            t_clean = re.sub(r"<[^>]+>", "", title).strip()
            s_clean = re.sub(r"<[^>]+>", "", snippet).strip()
            if t_clean:
                results.append(f"• {t_clean}\n  {s_clean[:120]}\n  {href}")
        if not results:
            return f"[Error] No parseable results for '{query}'."
        audit("web_search", f"query={query[:50]} results={len(results)}")
        return f"Search: {query}\n{'='*50}\n\n" + "\n\n".join(results)
    except Exception as e:
        return f"[Error] Search failed: {e}"


def has_internet() -> bool:
    """Check if internet connection is available using multiple endpoints."""
    endpoints = [
        "https://www.microsoft.com",
        "https://www.google.com", 
        "https://httpbin.org/get",
        "https://1.1.1.1"
    ]
    
    for endpoint in endpoints:
        try:
            # Use HEAD request for faster check when possible
            if endpoint.startswith("https://www."):
                response = requests.head(endpoint, timeout=3, allow_redirects=True)
            else:
                response = requests.get(endpoint, timeout=3)
            if response.status_code < 400:
                return True
        except Exception:
            continue
    return False


# ============================================================
# TOOL DISPATCH
# ============================================================

TOOL_RISK = {
    "list_directory":   0,
    "read_file":        0,
    "search_files":     0,
    "get_system_info":  0,
    "list_models":      0,
    "web_search":       0,
    "create_folder":    1,
    "create_file":      1,
    "create_shortcut":  1,
    "run_powershell":   2,
    "delete_file":      2,
    "run_as_admin":     2,
    "uninstall_program": 3,
    "revert":           1,
}

RISK_LABELS = {
    0: "read-only",
    1: "low risk",
    2: "moderate — modifies system",
    3: "⚠️ HIGH RISK",
}


def execute_with_timeout(tool_func, timeout_seconds: int):
    """Execute tool function with timeout"""
    import signal
    import threading
    
    result = [None]
    exception = [None]
    
    def target():
        try:
            result[0] = tool_func()
        except Exception as e:
            exception[0] = e
    
    thread = threading.Thread(target=target)
    thread.daemon = True
    thread.start()
    thread.join(timeout_seconds)
    
    if thread.is_alive():
        return f"[Error] Tool execution timed out after {timeout_seconds} seconds"
    
    if exception[0]:
        return f"[Error] {exception[0]}"
    
    return result[0]

def safe_execute(tool: str, args: Dict) -> str:
    try:
        return execute_tool(tool, args)
    except Exception as e:
        return f"[Error] Tool crashed: {e}"

def execute_tool(tool: str, args: Dict) -> str:
    if not tool:
        return "[Error] Empty tool name"
    audit("tool_call", f"tool={tool} args={json.dumps(args)[:200]}")
    timeout = TOOL_TIMEOUTS.get(tool, 30)  # Default 30 seconds
    
    try:
        if tool == "list_directory":
            return execute_with_timeout(lambda: tool_list_directory(args.get("path", ".")), timeout)
        if tool == "read_file":
            return execute_with_timeout(lambda: tool_read_file(args.get("path", "")), timeout)
        if tool == "create_folder":
            return execute_with_timeout(lambda: tool_create_folder(args.get("path", ""), args.get("name")), timeout)
        if tool == "create_file":
            return execute_with_timeout(lambda: tool_create_file(args.get("path", ""), args.get("content", "")), timeout)
        if tool == "delete_file":
            return execute_with_timeout(lambda: tool_delete_file(args.get("path", "")), timeout)
        if tool == "search_files":
            return execute_with_timeout(lambda: tool_search_files(args.get("root", str(Path.home())), args.get("pattern", ""), int(args.get("max_results", 20))), timeout)
        if tool == "run_powershell":
            return execute_with_timeout(lambda: tool_run_powershell(args.get("command", "")), timeout)
        if tool == "get_system_info":
            return execute_with_timeout(lambda: tool_get_system_info(args.get("query", "info")), timeout)
        if tool == "revert":
            return execute_with_timeout(lambda: tool_revert(), timeout)
        if tool == "run_as_admin":
            return execute_with_timeout(lambda: tool_run_as_admin(args.get("path", ""), args.get("args", "")), timeout)
        if tool == "uninstall_program":
            return execute_with_timeout(lambda: tool_uninstall_program(args.get("name", "")), timeout)
        if tool == "create_shortcut":
            return execute_with_timeout(lambda: tool_create_shortcut(args.get("target", ""), args.get("shortcut", ""), args.get("description", "")), timeout)
        if tool == "web_search":
            return execute_with_timeout(lambda: tool_web_search(args.get("query", ""), int(args.get("max_results", 5))), timeout)
        if tool == "list_models":
            result = execute_with_timeout(lambda: "\n".join(list_models()) or "No models found.", timeout)
            return result if result is not None else "[Error] Could not list models."
        if tool == "set_model":
            name = args.get("name", "")
            def _set_model():
                global CURRENT_MODEL
                current = list_models()
                if name in current:
                    CURRENT_MODEL = name
                    audit("set_model", CURRENT_MODEL)
                    return f"Model set to: {CURRENT_MODEL}"
                return f"[Error] Model '{name}' not found."
            return execute_with_timeout(_set_model, timeout)
        return f"[Error] Unknown tool: {tool}"
    except Exception as e:
        return f"[Error] Tool execution failed: {e}"


# ============================================================
# JSON PARSING
# ============================================================

def _fix_json(text: str) -> str:
    """Basic JSON repair: smart quotes, trailing commas."""
    s = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    # Remove trailing commas before } or ]
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s

def extract_tool_call(text: str) -> Optional[Dict]:
    text = text.strip()

    # FAST PATH: clean JSON
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(_fix_json(text))
            if isinstance(data, dict) and "tool" in data:
                return data
        except Exception:
            pass
    
    # Fallback: Collect all {...} balanced blocks
    depth = start = None
    candidates = []
    for i, ch in enumerate(text):
        if ch == "{":
            if depth is None:
                depth, start = 1, i
            else:
                depth += 1
        elif ch == "}" and depth is not None:
            depth -= 1
            if depth == 0:
                candidates.append(text[start:i+1])
                depth = start = None

    # Try candidates in order, prioritize shorter/top-level blocks
    candidates.sort(key=len)
    
    for raw in candidates:
        try:
            data = json.loads(_fix_json(raw))
            if isinstance(data, dict) and "tool" in data and "args" in data:
                # Additional validation: ensure tool exists
                if data["tool"] in TOOL_RISK:
                    return data
        except Exception:
            pass
    return None

def is_tool_response(text: str) -> bool:
    return extract_tool_call(text) is not None


# ============================================================
# SYSTEM CONTEXT & PROMPT
# ============================================================

def gather_context() -> Dict:
    ctx = {
        "username": getpass.getuser(),
        "hostname": platform.node(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "arch": platform.machine(),
        "home": str(Path.home()),
        "desktop": str(Path.home() / "Desktop"),
        "documents": str(Path.home() / "Documents"),
        "downloads": str(Path.home() / "Downloads"),
        "appdata": os.environ.get("APPDATA", ""),
        "localappdata": os.environ.get("LOCALAPPDATA", ""),
        "temp": os.environ.get("TEMP", ""),
    }
    try:
        r = run_ps("$o=Get-CimInstance Win32_OperatingSystem; 'Total: {0:N0} MB  Free: {1:N0} MB' -f ($o.TotalVisibleMemorySize/1KB),($o.FreePhysicalMemory/1KB)", timeout=8)
        ctx["ram"] = r
    except Exception:
        ctx["ram"] = "unknown"
    try:
        r = run_ps("(Get-PSDrive -PSProvider FileSystem).Root -join ', '", timeout=8)
        ctx["drives"] = r
    except Exception:
        ctx["drives"] = "C:\\"
    return ctx


SYSTEM_PROMPT = """You are a Windows OS assistant. Your ONLY job is to COMPLETE tasks by executing tools.

CRITICAL BEHAVIOR:
- NEVER explain how to do something if it can be done with tools.
- ALWAYS prefer taking action over explaining.
- If a task can be executed, you MUST return a tool call.
- Only explain AFTER all actions are completed.
- If unsure, attempt best-effort execution using available tools.

CRITICAL RULES:
1. For ANY actionable request → output ONLY JSON tool call.
   Format: {"tool": "tool_name", "args": {"key": "value"}}

2. DO NOT ask the user for paths if they can be inferred from system context.

3. MULTI-STEP TASKS:
   If a task requires multiple steps:
   - You MUST return steps via planner
   - DO NOT summarize steps in text

4. FAILURE HANDLING:
   - If a tool fails, retry with corrected arguments
   - NEVER stop at explanation

5. ONLY explain if user explicitly says:
   "explain", "why", "what is", "how does"

AVAILABLE TOOLS:
  list_directory(path)
  read_file(path)
  create_folder(path, name?)
  create_file(path, content?)
  delete_file(path)
  search_files(root, pattern, max_results?)
  run_powershell(command)
  get_system_info(query)
  run_as_admin(path, args?)
  create_shortcut(target, shortcut, description?)
  uninstall_program(name)
  web_search(query, max_results?)
  list_models()
  set_model(name)
  revert()
"""

def build_system_prompt(ctx: Dict) -> str:
    prompt = SYSTEM_PROMPT
    # Inject real paths
    for key, val in ctx.items():
        prompt = prompt.replace(f"{{{key}}}", str(val))
    
    # Load and format memory with intelligent context
    memory = load_memory()
    memory_context = ""
    if memory:
        memory_context = "\nRECENT USER ACTIVITY & PREFERENCES:\n"
        
        # Provide meaningful context from stored memory
        if "last_folder" in memory:
            memory_context += f"  Last created folder: {memory['last_folder']}\n"
        if "last_file" in memory:
            memory_context += f"  Last created file: {memory['last_file']}\n"
        if "last_search" in memory:
            memory_context += f"  Last search pattern: {memory['last_search']}\n"
        if "last_command" in memory:
            memory_context += f"  Last PowerShell command: {memory['last_command']}\n"
        if "last_web_search" in memory:
            memory_context += f"  Last web search: {memory['last_web_search']}\n"
        
        # Add any other memory items
        for key, value in memory.items():
            if key not in ["last_folder", "last_file", "last_search", "last_command", "last_web_search"]:
                memory_context += f"  {key}: {value}\n"
    
    # Add context block
    context_block = f"""
SYSTEM CONTEXT (use these directly):
  Username    : {ctx['username']}
  Hostname    : {ctx['hostname']}
  OS          : Windows {ctx['os_release']}
  RAM         : {ctx.get('ram', 'unknown')}
  Drives      : {ctx.get('drives', 'C:')}  
  Home        : {ctx['home']}
  Desktop     : {ctx['desktop']}
  Documents   : {ctx['documents']}
  Downloads   : {ctx['downloads']}
  AppData     : {ctx['appdata']}
  Temp        : {ctx['temp']}
{memory_context}
"""
    return prompt + context_block


# ============================================================
# CONFIRMATION
# ============================================================

def confirm_tool_call(tool: str, args: Dict) -> bool:
    risk = TOOL_RISK.get(tool, 1)

    # FULL AUTO MODE (except dangerous)
    if AUTO_APPROVE:
        if risk < 3:
            print(f"  [Auto] {tool}")
            return True

    # Always block only HIGH RISK
    if risk == 3:
        print(f"\n  ⚠️ HIGH RISK: {tool}")
        print(f"  Args: {json.dumps(args)[:200]}")
        try:
            ans = input("  Type YES to confirm: ").strip()
            return ans.upper() == "YES"
        except:
            return False

    return True


# ============================================================
# CONVERSATION TRIM
# ============================================================

def trim_history(messages: List[Dict]) -> List[Dict]:
    system = [m for m in messages if m["role"] == "system"]
    rest   = [m for m in messages if m["role"] != "system"]
    if len(rest) > MAX_HISTORY:
        rest = rest[-MAX_HISTORY:]
    return system[:1] + rest


# ============================================================
# MAIN LOOP
# ============================================================

def print_help():
    print("""
  Commands:
    /models          — switch AI model
    /sysinfo         — show system information
    /auto            — toggle auto-approve mode (skips confirmation for low/mid risk)
    /clear           — clear conversation history
    /undo            — undo last action
    /help            — show this help
    /exit            — exit
""")

def main():
    global AUTO_APPROVE

    print("=" * 60)
    print("  Local Windows OS Assistant")
    print("=" * 60)

    if not choose_admin_mode():
        return
    if not choose_provider():
        print("  Cannot continue without an AI provider. Exiting.")
        return

    # Model selection (Ollama/LM Studio only — cloud already set above)
    if CURRENT_PROVIDER in ("ollama", "lmstudio") and not CURRENT_MODEL:
        models = list_models()
        if models:
            switch_model()
        else:
            print("  ⚠️  No models found. Pull one with: ollama pull llama3\n")

    print("\n  Gathering system context...", end=" ", flush=True)
    ctx = gather_context()
    print("done.\n")

    admin_label = "✅ Administrator" if is_admin() else "standard user"
    provider_label = {
        "lmstudio": "LM Studio", "openrouter": "OpenRouter",
        "groq": "Groq", "gemini": "Google Gemini", "custom": "Custom",
    }.get(CURRENT_PROVIDER, "Ollama")

    print(f"  User     : {ctx['username']}  |  OS: Windows {ctx['os_release']}")
    print(f"  Provider : {provider_label}  |  Model: {CURRENT_MODEL or '(none)'}")
    print(f"  Privileges: {admin_label}")
    print("\n  Type /help for commands. Type your request and press Enter.\n")

    AUTO_APPROVE = False
    system_prompt = build_system_prompt(ctx)
    messages: List[Dict] = [{"role": "system", "content": system_prompt}]

    while True:
        try:
            user = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Exiting.")
            break

        if not user:
            continue

        # Commands
        cmd = user.lower()
        if cmd in ("/exit", "/quit", "exit", "quit"):
            print("  Goodbye.")
            break
        if cmd == "/help":
            print_help()
            continue
        if cmd == "/models":
            switch_model()
            # Update system prompt in case model changed
            messages[0]["content"] = build_system_prompt(ctx)
            continue
        if cmd == "/sysinfo":
            print(f"\n{system_prompt.split('SYSTEM CONTEXT')[1]}" if "SYSTEM CONTEXT" in system_prompt else "")
            continue
        if cmd == "/auto":
            AUTO_APPROVE = not AUTO_APPROVE
            auto_label = "ON" if AUTO_APPROVE else "OFF"
            print(f"\n  Auto-approve: {auto_label}  (low/mid-risk tools skip confirmation)\n")
            continue
        if cmd == "/clear":
            messages = [{"role": "system", "content": system_prompt}]
            print("  Conversation cleared.\n")
            continue
        if cmd == "/undo":
            print(f"\n  {tool_revert()}\n")
            continue

        if not CURRENT_MODEL:
            print("  ⚠️  No model selected. Use /models to choose one.\n")
            continue

        # Simple heuristic instead of LLM classifier (more reliable)
        lower = user.lower()
        should_search_first = (
            "latest" in lower
            or "news" in lower
            or "today" in lower
            or "current" in lower
            or "?" in user
        )
        intent = "mixed"  # fallback (planner handles actual behavior)
        
        # Trace execution decisions
        audit("intent_classification", intent)
        audit("search_first", should_search_first)

        # WORKFLOW: Search first, then deepthink (if internet available)
        if should_search_first and has_internet():
            print(f"\n  🔍 Searching web for current information...")
            search_result = tool_web_search(user, max_results=5)

            if not search_result.startswith("[Error]"):
                print(f"  📊 Results found. Analyzing...")

                # Add search results to context and get AI to analyze them
                search_messages = messages.copy()
                search_messages.append({
                    "role": "user",
                    "content": f"{user}\n\n[WEB SEARCH RESULTS - analyze and synthesize these to answer the question]:\n\n{search_result}\n\nProvide a comprehensive answer based on the search results above."
                })

                response = call_ai(search_messages)

                if response and not response.startswith("[Error]") and response != "[Cancelled]":
                    # Store in actual conversation history
                    messages.append({"role": "user", "content": user})
                    messages.append({"role": "assistant", "content": f"[Based on web search] {response}"})
                    print(f"\n  Assistant: {response}\n")
                else:
                    # Fallback to regular deepthink if AI call failed
                    messages.append({"role": "user", "content": user})
                    response = call_ai(messages)
                    if response and not response.startswith("[Error]"):
                        messages.append({"role": "assistant", "content": response})
                        print(f"\n  Assistant: {response}\n")
                    else:
                        messages.pop()
            else:
                # No internet or search failed - proceed with deepthink only
                print(f"  ⚠️  No internet. Using local knowledge...")
                messages.append({"role": "user", "content": user})
                response = call_ai(messages)

                if response in ("[Cancelled]",) or response.startswith("[Error]"):
                    print(f"\n  {response}\n")
                    messages.pop()
                    continue

                tool_data = extract_tool_call(response)
                if tool_data:
                    # Handle tool call normally
                    tool_name = tool_data.get("tool", "")
                    tool_args = tool_data.get("args", {})
                    if not isinstance(tool_args, dict):
                        tool_args = {}

                    if confirm_tool_call(tool_name, tool_args):
                        print(f"\n  ⚙️  Running: {tool_name}...")
                        result = execute_tool(tool_name, tool_args)
                        print(f"\n  📊 Result:\n{result}\n")

                        messages.append({"role": "assistant", "content": response})
                        messages.append({"role": "user", "content": f"[Tool result] {result}"})
                        followup = call_ai(messages)
                        if followup and not followup.startswith("[Error]") and followup != "[Cancelled]":
                            if not extract_tool_call(followup):
                                print(f"  Assistant: {followup}\n")
                            messages.append({"role": "assistant", "content": followup})
                        else:
                            messages.append({"role": "assistant", "content": response})
                    else:
                        print("  Cancelled.\n")
                        messages.pop()
                else:
                    messages.append({"role": "assistant", "content": response})
                    print(f"\n  Assistant: {response}\n")

            messages = trim_history(messages)
            continue

        messages.append({"role": "user", "content": user})

        # STEP 1: PLAN
        steps, ok = [], False
        for attempt in range(2):
            steps, ok = plan_task(f"{user}\n\n[Context] Desktop: {ctx['desktop']}, Home: {ctx['home']}, Documents: {ctx['documents']}, Downloads: {ctx['downloads']}", messages)
            if ok:
                break
            if attempt == 0:
                user = user + "\n\nBreak this into tool steps."

        # FORCE planner usage if user intent is local or mixed
        if not ok and intent in ("local", "mixed"):
            steps, _ = plan_task(f"{user}\n\nBreak this into tool steps.\n\n[Context] Desktop: {ctx['desktop']}, Home: {ctx['home']}", messages)

        if steps:
            print(f"\n  🧠 Planned {len(steps)} step(s)\n")

            for step in steps:
                if not validate_step(step):
                    print(f"  [Error] Invalid step: {step}")
                    break
                    
                tool_name = step.get("tool", "")
                tool_args = step.get("args", {})

                tool_args = enrich_args(tool_name, tool_args, ctx)

                # Check cooldown to prevent create-delete loops
                if check_tool_cooldown(tool_name, tool_args):
                    print(f"  [Cooldown] {tool_name} blocked - recent opposite operation detected")
                    break

                if not confirm_tool_call(tool_name, tool_args):
                    print("  Cancelled.\n")
                    break

                print(f"\n  ⚙️ Running: {tool_name}...")
                result = execute_with_retry(tool_name, tool_args, messages)
                
                # Record execution for cooldown tracking
                record_tool_execution(tool_name, tool_args)

                print(f"\n  📊 Result:\n{result}\n")

                # store useful memory automatically
                if tool_name == "create_folder":
                    remember("last_folder", tool_args.get("path"))
                elif tool_name == "create_file":
                    remember("last_file", tool_args.get("path"))
                elif tool_name == "search_files":
                    remember("last_search", tool_args.get("pattern"))
                elif tool_name == "run_powershell":
                    remember("last_command", tool_args.get("command"))
                elif tool_name == "web_search":
                    remember("last_web_search", tool_args.get("query"))

                messages.append({"role": "assistant", "content": json.dumps(step)})
                messages.append({"role": "user", "content": f"[Tool result] {result}"})

            # final AI response after steps
            followup = call_ai(messages)
            if followup and not is_tool_response(followup):
                print(f"  Assistant: {followup}\n")
                messages.append({"role": "assistant", "content": followup})

        else:
            # fallback to normal single-step behavior
            response = call_ai(messages)

            if response in ("[Cancelled]",) or response.startswith("[Error]"):
                print(f"\n  {response}\n")
                messages.pop()
                continue

            tool_data = extract_tool_call(response)
            if tool_data:
                # Handle tool call normally
                tool_name = tool_data.get("tool", "")
                tool_args = tool_data.get("args", {})
                if not isinstance(tool_args, dict):
                    tool_args = {}

                if confirm_tool_call(tool_name, tool_args):
                    print(f"\n  \u2699\ufe0f  Running: {tool_name}...")
                    result = safe_execute(tool_name, tool_args)
                    print(f"\n  \ud83d\udcca Result:\n{result}\n")

                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "user", "content": f"[Tool result] {result}"})
                    followup = call_ai(messages)
                    if followup and not followup.startswith("[Error]") and followup != "[Cancelled]":
                        if not is_tool_response(followup):
                            print(f"  Assistant: {followup}\n")
                        messages.append({"role": "assistant", "content": followup})
                    else:
                        messages.append({"role": "assistant", "content": response})
                else:
                    print("  Cancelled.\n")
                    messages.pop()
            else:
                messages.append({"role": "assistant", "content": response})
                print(f"\n  Assistant: {response}\n")

        messages = trim_history(messages)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Exiting.")
    except Exception:
        print("\n" + "=" * 60)
        print("  UNEXPECTED ERROR")
        print("=" * 60)
        traceback.print_exc()
    finally:
        print("\n  Press Enter to close...")
        try:
            input()
        except Exception:
            pass
