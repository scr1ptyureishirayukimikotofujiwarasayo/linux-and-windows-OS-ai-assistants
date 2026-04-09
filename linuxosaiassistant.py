#!/usr/bin/env python3
import requests
import subprocess
import json
import os
import platform
import getpass
from pathlib import Path
import re
import shlex
import sys
import logging

# ================= SUDO PASSWORD HELPER =================
_sudo_password = None

def get_sudo_password():
    global _sudo_password
    if _sudo_password is None:
        _sudo_password = getpass.getpass("Enter sudo password: ")
    return _sudo_password

def clear_sudo_password():
    global _sudo_password
    _sudo_password = None

# ================= CONFIG =================
class Config:
    def __init__(self):
        self.backend = None
        self.model_name = None
        self.ollama_model = None
        self.max_steps = 5
        self.memory_limit = 100  # Increased memory limit
        self.command_timeout = 30  # Configurable command timeout
        self.auto_execute = False
        self.dry_run = False  # New: dry-run mode
        
        # Paths
        self.lmstudio_url = "http://localhost:1234/v1/chat/completions"
        self.api_url = "https://api.openai.com/v1/chat/completions"
        self.api_key = os.getenv("OPENAI_API_KEY")
        
        # Fixed memory location
        memory_dir = Path.home() / ".local" / "share" / "linux_agent"
        memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = str(memory_dir / "memory.json")
        
        # Log file
        self.log_file = str(memory_dir / "agent.log")

# Global config instance
config = Config()

# Setup logging
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(config.log_file),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# ================= SYSTEM INFO =================

def get_xdg_dir(dir_type):
    """Get XDG user directory with fallback to English names"""
    if platform.system() != "Linux":
        # Non-Linux fallback to English names
        home = Path.home()
        fallbacks = {
            'DESKTOP': home / 'Desktop',
            'DOWNLOAD': home / 'Downloads',
            'DOCUMENTS': home / 'Documents'
        }
        return str(fallbacks.get(dir_type, home / dir_type.lower()))
    
    try:
        result = subprocess.run(['xdg-user-dir', dir_type], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        logger.warning("xdg-user-dir not found, using English names")
    except subprocess.TimeoutExpired:
        logger.warning("xdg-user-dir timed out, using English names")
    except Exception as e:
        logger.warning(f"xdg-user-dir failed: {e}, using English names")
    
    # Fallback to English names
    home = Path.home()
    fallbacks = {
        'DESKTOP': home / 'Desktop',
        'DOWNLOAD': home / 'Downloads',
        'DOCUMENTS': home / 'Documents'
    }
    return str(fallbacks.get(dir_type, home / dir_type.lower()))

def get_system_info():
    return {
        "os": platform.system(),
        "distro": platform.platform(),
        "username": getpass.getuser(),
        "home": str(Path.home()),
        # Remove cwd from system info to avoid stale data
        # "cwd": os.getcwd(),  # Will be updated at runtime
        "shell": os.getenv("SHELL", "unknown"),
        "desktop": get_xdg_dir('DESKTOP'),
        "downloads": get_xdg_dir('DOWNLOAD'),
        "documents": get_xdg_dir('DOCUMENTS')
    }

SYS_INFO = get_system_info()

# ================= MEMORY =================

def load_memory():
    if not os.path.exists(config.memory_file):
        return []
    try:
        with open(config.memory_file, "r") as f:
            memory = json.load(f)
            logger.debug(f"Loaded {len(memory)} memory entries")
            return memory
    except Exception as e:
        logger.error(f"Failed to load memory: {e}")
        return []

def save_memory(memory):
    """Save entire memory list to file efficiently"""
    try:
        with open(config.memory_file, "w") as f:
            json.dump(memory[-config.memory_limit:], f, indent=2)  # Keep only last N entries
        logger.debug(f"Saved {len(memory)} memory entries")
    except Exception as e:
        logger.error(f"Memory save error: {e}")

# ================= JSON FIX =================

def extract_json(text):
    if not text:
        return None

    text = text.strip()
    
    # Find first complete JSON object (brace counting, ignoring quotes)
    brace_count = 0
    start = -1
    in_quotes = False
    escape = False
    
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"' and not escape:
            in_quotes = not in_quotes
            continue
        if not in_quotes:
            if ch == '{':
                brace_count += 1
                if start == -1:
                    start = i
            elif ch == '}':
                brace_count -= 1
                if brace_count == 0 and start != -1:
                    json_str = text[start:i+1]
                    # --- CLEANUP ---
                    # 1. Replace newlines and carriage returns within JSON string values with spaces
                    # This handles newlines inside quoted strings while preserving JSON structure
                    cleaned = []
                    in_string = False
                    escape = False
                    for j, ch in enumerate(json_str):
                        if escape:
                            escape = False
                            cleaned.append(ch)
                            continue
                        if ch == '\\':
                            escape = True
                            cleaned.append(ch)
                            continue
                        if ch == '"':
                            in_string = not in_string
                            cleaned.append(ch)
                            continue
                        if in_string and (ch == '\n' or ch == '\r'):
                            cleaned.append(' ')  # Replace newlines in strings with spaces
                        else:
                            cleaned.append(ch)
                    json_str = ''.join(cleaned)
                    
                    # 2. Remove trailing commas before } or ]
                    json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)
                    # 3. Replace smart quotes with straight quotes
                    json_str = json_str.replace('\u201c', '"').replace('\u201d', '"')
                    # 4. Try to parse
                    try:
                        return json.loads(json_str)
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON parsing error: {e}")
                        logger.debug(f"Cleaned JSON: {json_str[:200]}")
                        return None
    
    # Fallback: regex search for first balanced braces
    brace_re = re.compile(r'(\{.*\})', re.DOTALL)
    match = brace_re.search(text)
    if match:
        try:
            return json.loads(match.group(1))
        except:
            pass
    return None

# ================= BACKENDS =================

def ollama_chat(prompt):
    if platform.system() != "Linux":
        return "ERROR: Ollama backend only supported on Linux"
    
    try:
        r = subprocess.run(
            ["ollama", "run", config.ollama_model, prompt],
            capture_output=True,
            text=True,
            timeout=60
        )
        if r.returncode != 0:
            raise Exception(f"Ollama error: {r.stderr.strip()}")
        return r.stdout.strip()
    except FileNotFoundError:
        return "ERROR: Ollama not found. Please install Ollama."
    except subprocess.TimeoutExpired:
        return "ERROR: Ollama command timed out"
    except Exception as e:
        logger.error(f"Ollama failed: {e}")
        return f"ERROR: Ollama failed - {str(e)}"

def lmstudio_chat(messages):
    try:
        r = requests.post(
            config.lmstudio_url,
            headers={"Content-Type": "application/json"},
            json={"model": config.model_name, "messages": messages},
            timeout=60
        )
        r.raise_for_status()
        response = r.json()
        
        # Validate response structure
        if "choices" not in response or not response["choices"]:
            raise Exception("Invalid response: missing choices")
        
        choice = response["choices"][0]
        if "message" not in choice or "content" not in choice["message"]:
            raise Exception("Invalid response: missing message content")
            
        return choice["message"]["content"]
    except requests.exceptions.ConnectionError:
        return "ERROR: LM Studio not running. Please start LM Studio."
    except requests.exceptions.Timeout:
        return "ERROR: LM Studio request timed out"
    except requests.exceptions.HTTPError as e:
        return f"ERROR: LM Studio HTTP error - {e}"
    except Exception as e:
        logger.error(f"LM Studio failed: {e}")
        return f"ERROR: LM Studio failed - {str(e)}"

def api_chat(messages):
    try:
        if not config.api_key:
            return "ERROR: OpenAI API key not found. Set OPENAI_API_KEY environment variable."
            
        r = requests.post(
            config.api_url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json"
            },
            json={"model": config.model_name, "messages": messages},
            timeout=60
        )
        r.raise_for_status()
        response = r.json()
        
        # Validate response structure
        if "choices" not in response or not response["choices"]:
            raise Exception("Invalid response: missing choices")
        
        choice = response["choices"][0]
        if "message" not in choice or "content" not in choice["message"]:
            raise Exception("Invalid response: missing message content")
            
        return choice["message"]["content"]
    except requests.exceptions.ConnectionError:
        return "ERROR: Network connection failed. Check internet connection."
    except requests.exceptions.Timeout:
        return "ERROR: API request timed out"
    except requests.exceptions.HTTPError as e:
        return f"ERROR: API HTTP error - {e}"
    except Exception as e:
        logger.error(f"API failed: {e}")
        return f"ERROR: API failed - {str(e)}"

def ask_ai(messages):
    if config.backend == "ollama":
        parts = []
        for m in messages:
            role = m.get("role", "user").upper()
            content = m.get("content", "")
            parts.append(f"=== {role} ===\n{content}")
        prompt = "\n\n".join(parts)
        return ollama_chat(prompt)
    elif config.backend == "lmstudio":
        return lmstudio_chat(messages)
    elif config.backend == "api":
        return api_chat(messages)
    return "Invalid backend"

# ================= PLANNER =================

def build_planner_prompt(task, task_memory):
    return [
        {
            "role": "system",
            "content": (
                "You are a Linux task planner. You work iteratively - one step at a time.\n"
                "Output ONLY a JSON object with these fields:\n"
                "{\n"
                "  \"thought\": \"brief reasoning about the current state\",\n"
                "  \"action\": \"run_terminal\" or \"finish\",\n"
                "  \"command\": \"the exact command to run (if action is run_terminal)\"\n"
                "}\n\n"
                "System information:\n"
                f"{json.dumps(SYS_INFO, indent=2)}\n\n"
                "Recent memory (previous steps of this task):\n"
                f"{json.dumps(task_memory, indent=2)}\n\n"
                "STRICT RULES:\n"
                "1. Always use absolute paths - never 'cd'.\n"
                "2. Forbidden characters/operators: | ; && || > < & ` $()\n"
                "3. Forbidden commands: cd, vim, nano, less, top, htop, sudo (the agent handles sudo)\n"
                "4. For `mkdir` always use `-p` flag.\n"
                "5. For `rm` always use `-i` flag or ask for confirmation.\n"
                "6. To check if a package is installed, use `dpkg -l PACKAGE` (Debian/Ubuntu) or `rpm -q PACKAGE` (RHEL).\n"
                "7. To install a package, first check if it exists: `apt-cache show PACKAGE`.\n"
                "8. If a command fails, try a different approach in the next step - do not repeat the same failing command.\n"
                "9. When the original user task is fully completed, set action to 'finish'.\n\n"
                "CORRECT examples:\n"
                "- {\"thought\": \"Chrome not installed, check if package exists\", \"action\": \"run_terminal\", \"command\": \"apt-cache show google-chrome-stable\"}\n"
                "- {\"thought\": \"Package exists, install it\", \"action\": \"run_terminal\", \"command\": \"sudo apt-get install -y google-chrome-stable\"}\n"
                "- {\"thought\": \"All steps done\", \"action\": \"finish\", \"command\": \"\"}\n\n"
                "INCORRECT examples (will be rejected):\n"
                "- {\"command\": \"dpkg -l | grep chrome\"}   # pipe not allowed\n"
                "- {\"command\": \"cd /tmp && ls\"}          # cd and chaining\n"
                "- {\"command\": \"sudo rm -rf /home/user\"} # dangerous pattern\n"
                "- {\"command\": \"vim file.txt\"}           # interactive\n\n"
                "Now produce exactly one JSON object for the next step."
            )
        },
        {
            "role": "user",
            "content": f"Task: {task}\nIf the last memory entry shows an ERROR, you must try a different command."
        }
    ]

# ================= COMMAND PROCESSING =================

def resolve_paths(command):
    """Replace relative paths with absolute paths using word boundaries and XDG"""
    
    # Use os.path.expanduser for proper ~ handling
    expanded = os.path.expanduser(command)
    
    # Use os.path.expandvars for environment variable expansion
    expanded = os.path.expandvars(expanded)
    
    # Use XDG-compliant paths
    replacements = {
        r'\bdesktop\b': SYS_INFO['desktop'],
        r'\bdownloads\b': SYS_INFO['downloads'], 
        r'\bdocuments\b': SYS_INFO['documents']
    }
    
    resolved = expanded
    for pattern, abs_path in replacements.items():
        resolved = re.sub(pattern, abs_path, resolved)
    
    return resolved

def parse_command(command):
    """Parse command into list safely using shlex"""
    try:
        return shlex.split(command)
    except ValueError as e:
        logger.error(f"Command parsing error: {e}")
        return None

def has_pipes_or_chaining(command):
    """Check if command contains pipes, redirections, or chaining operators (outside quotes)"""
    in_quotes = False
    quote_char = None
    i = 0
    while i < len(command):
        ch = command[i]
        if ch in ('"', "'") and not in_quotes:
            in_quotes = True
            quote_char = ch
        elif in_quotes and ch == quote_char:
            in_quotes = False
            quote_char = None
        elif not in_quotes:
            if ch in ('|', ';', '&'):
                return True
            if ch in ('>', '<'):
                return True
        i += 1
    return False

def is_interactive_command(command_parts):
    """Check if command is interactive and will hang"""
    if not command_parts:
        return False
    
    # Only block editors, pagers, and monitoring tools
    interactive_commands = {
        'vim', 'vi', 'nano', 'emacs', 'code',
        'less', 'more', 'top', 'htop', 'iotop', 'nethogs'
    }
    
    base_cmd = command_parts[0].lower()
    return base_cmd in interactive_commands

def is_dangerous_command(command_parts):
    """Check for dangerous commands using regex patterns"""
    if not command_parts:
        return True
    
    dangerous_patterns = [
        r'^rm\s+-rf\s+/$',  # rm -rf /
        r'^rm\s+-rf\s+/\*$',  # rm -rf /*
        r'^sudo\s+rm\s+-rf\s+/$',  # sudo rm -rf /
        r'^sudo\s+rm\s+-rf\s+/\*$',  # sudo rm -rf /*
        r'^mkfs',  # mkfs commands
        r'^dd\s+if=/dev/zero',  # dd with /dev/zero
        r'^chmod\s+-R\s+777\s+/$',  # chmod -R 777 /
        r':\(\)\{\:\|\:&\}\;:',  # fork bomb
        r'^\$\(',  # command substitution start
        r';\s*rm',  # semicolon followed by rm
        r'\|\s*rm',  # pipe followed by rm
        r'&&\s*rm',  # && followed by rm
        r'\|\s*sh',  # pipe to shell
        r';\s*sh',  # semicolon to shell
        r'\b(eval)\b',  # eval command
        r'\$\([^)]*\)',  # command substitution anywhere
        r'\|\s*(bash|zsh|dash|fish|sh)\b',  # pipe to shells
        r';\s*(bash|zsh|dash|fish|sh)\b',  # semicolon to shells
    ]
    
    command_str = ' '.join(command_parts)
    
    for pattern in dangerous_patterns:
        if re.search(pattern, command_str, re.IGNORECASE):
            return True
    
    return False

def confirm_execution(command):
    """Ask user for confirmation before executing command"""
    if config.auto_execute:
        logger.info(f"Auto-executing: {command}")
        return True
    
    response = input(f"Execute: {command}? (y/N): ").strip().lower()
    return response in ['y', 'yes']

def validate_command(command, error=None):
    """Ask AI to suggest a completely different command to achieve the same goal."""
    for attempt in range(3):  # Max 3 attempts
        messages = [
            {"role": "system", "content": "You are a Linux expert. The user's command failed. Suggest a completely different command to accomplish the same goal. Output ONLY JSON: {\"command\": \"new_command\"}"},
            {"role": "user", "content": f"Original command: {command}\nError: {error}\nReturn a different working command as JSON only."}
        ]
        
        response = ask_ai(messages)
        
        # Check if AI returned an error
        if response.startswith("ERROR:"):
            logger.warning(f"Validation failed: {response}")
            if attempt == 2:  # Last attempt
                logger.warning("Could not validate command, using original")
                return command
            continue
            
        data = extract_json(response)
        
        if data and "command" in data:
            fixed_command = data["command"]
            if fixed_command.strip():  # Ensure not empty
                return fixed_command
                
        logger.warning(f"Invalid validation response (attempt {attempt + 1}/3)")
    
    logger.warning("All validation attempts failed, using original command")
    return command

def clean_command(command):
    """Clean and fix commands - ensure mkdir has -p flag"""
    if command.startswith("mkdir"):
        parts = command.split()
        # Ensure -p flag is present
        if "-p" not in parts:
            parts.insert(1, "-p")
        return " ".join(parts)
    return command

# ================= PRE-EXECUTION VALIDATION =================

def command_binary_exists(command_parts):
    """Check if the first word of the command is an executable in PATH."""
    if not command_parts:
        return False
    binary = command_parts[0]
    if binary.startswith(('sudo', 'time', 'nice')) and len(command_parts) > 1:
        binary = command_parts[1]
    from shutil import which
    return which(binary) is not None

def package_available(pkg_name):
    """Check if a package exists in the repositories (Debian/Ubuntu)."""
    try:
        r = subprocess.run(['apt-cache', 'show', pkg_name],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except:
        return False

def is_path_safe(path_str):
    """Prevent deletion of critical system directories."""
    dangerous_paths = ['/', '/usr', '/etc', '/var', '/boot', '/bin', '/sbin', '/lib', '/root']
    expanded = os.path.abspath(os.path.expanduser(path_str))
    for bad in dangerous_paths:
        if expanded == bad or expanded.startswith(bad + '/'):
            return False
    return True

def validate_command_safety(command_parts):
    """Extra safety checks beyond the existing is_dangerous_command."""
    if not command_parts:
        return True, "Empty command"
    # Check for rm with unsafe paths
    if command_parts[0] == 'rm' and '-rf' in command_parts:
        for i, arg in enumerate(command_parts):
            if arg in ('-rf', '-r', '-f') and i+1 < len(command_parts):
                target = command_parts[i+1]
                if not is_path_safe(target):
                    return False, f"Unsafe rm target: {target}"
    # Check for recursive chmod on system directories
    if command_parts[0] == 'chmod' and '-R' in command_parts:
        for i, arg in enumerate(command_parts):
            if arg == '-R' and i+1 < len(command_parts):
                target = command_parts[i+1]
                if not is_path_safe(target):
                    return False, f"Unsafe chmod target: {target}"
    return True, "OK"

# ================= EXECUTOR =================

def execute(command):
    for attempt in range(2):
        # Process command
        processed = resolve_paths(command)
        processed = clean_command(processed)
        
        # Parse command safely
        command_parts = parse_command(processed)
        if not command_parts:
            return "ERROR: Failed to parse command"

        # ---- NEW PRE-CHECKS ----
        # 1. Check if binary exists (for non-sudo commands)
        if command_parts[0] != 'sudo':
            if not command_binary_exists(command_parts):
                return f"ERROR: Command '{command_parts[0]}' not found in PATH. Install it first or use a different command."
        else:
            # for sudo commands, check the real binary
            if len(command_parts) > 1 and not command_binary_exists(command_parts[1:]):
                return f"ERROR: Sudo command '{command_parts[1]}' not found in PATH."

        # 2. Extra safety check for rm/chmod
        safe, msg = validate_command_safety(command_parts)
        if not safe:
            return f"ERROR: {msg}"

        # 3. If the command is 'apt-get install' and we are not in dry-run, pre-check package existence
        if command_parts[0] == 'sudo' and len(command_parts) > 2 and command_parts[1] in ('apt-get', 'apt'):
            if command_parts[2] == 'install':
                pkg_name = command_parts[3] if len(command_parts) > 3 else None
                if pkg_name and not package_available(pkg_name):
                    return f"ERROR: Package '{pkg_name}' not found in repositories. Aborting install."

        # ---- END OF NEW CHECKS ----

        # Check for cd commands
        if command_parts[0] == 'cd':
            return "ERROR: 'cd' command not supported. Use absolute paths instead."
        
        # Check for pipes and chaining
        if has_pipes_or_chaining(processed):
            return "ERROR: Pipes and command chaining are not allowed. Please split into separate steps or use temporary files."
        
        # Check for interactive commands
        if is_interactive_command(command_parts):
            return "ERROR: Interactive commands are not supported. Use non-interactive alternatives."
        
        # Safety check
        if is_dangerous_command(command_parts):
            return "ERROR: Dangerous command blocked"
        
        # Handle sudo commands
        if command_parts[0] == 'sudo':
            # Ask for user confirmation (already have confirm_execution later, but sudo is special)
            print(f"\n\u26a0\ufe0f  Command requires sudo: {' '.join(command_parts)}")
            confirm = input("Execute with sudo? (y/N): ").strip().lower()
            if confirm not in ('y', 'yes'):
                return "ERROR: Sudo command cancelled by user"
            
            # Get password once per session (cached)
            password = get_sudo_password()
            
            # Rebuild command: sudo -S (reads password from stdin)
            new_cmd = ['sudo', '-S'] + command_parts[1:]
            
            # Execute with password input
            try:
                r = subprocess.run(
                    new_cmd,
                    input=password + "\n",
                    capture_output=True,
                    text=True,
                    timeout=config.command_timeout
                )
                if r.returncode == 0:
                    return r.stdout.strip() or "SUCCESS"
                else:
                    # If password was wrong, clear cached password and retry same command
                    if "incorrect password" in r.stderr.lower():
                        clear_sudo_password()
                        # Retry same command with new password (not counted as attempt)
                        continue  # go to next iteration of the outer for attempt loop
                    return f"ERROR: {r.stderr.strip()}"
            except subprocess.TimeoutExpired:
                return "ERROR: Sudo command timed out"
            except Exception as e:
                return f"ERROR: Sudo execution failed: {e}"
        
        # User confirmation and dry-run check
        if config.dry_run:
            logger.info(f"DRY RUN: {processed}")
            print(f" DRY RUN: {processed}")
            return "DRY RUN - Command not executed"
        
        if not confirm_execution(processed):
            return "ERROR: Command cancelled by user"
        
        # Log command for audit trail
        logger.info(f"Executing: {processed}")
        
        # Execute safely without shell with timeout and SIGINT handling
        try:
            r = subprocess.run(command_parts, capture_output=True, text=True, timeout=config.command_timeout)
            
            if r.returncode == 0:
                return r.stdout.strip() or "SUCCESS"
            else:
                # If failed and first attempt, try AI validation
                if attempt == 0:
                    command = validate_command(processed, r.stderr.strip())
                else:
                    return f"ERROR: {r.stderr.strip()}"
        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out ({config.command_timeout}s limit)")
            return f"ERROR: Command timed out after {config.command_timeout}s"
        except FileNotFoundError:
            logger.error(f"Command not found: {command_parts[0]}")
            return f"ERROR: Command not found: {command_parts[0]}"
        except PermissionError:
            logger.error(f"Permission denied: {command_parts[0]}")
            return f"ERROR: Permission denied: {command_parts[0]}"
        except KeyboardInterrupt:
            logger.warning("Command interrupted by user (SIGINT)")
            return "ERROR: Command cancelled by user"
        except Exception as e:
            logger.error(f"Command execution failed: {e}")
            return f"ERROR: {e}"
    
    return "ERROR: max retries exceeded"

# ================= AGENT LOOP =================

def run_agent(task):
    # Per-task memory with sliding window
    all_memory = load_memory()
    # Ensure consistency with saved file
    all_memory = all_memory[-config.memory_limit:]  # Keep only last N entries
    
    # Get relevant memory for this task (last 10 entries across all tasks)
    task_memory = [entry for entry in all_memory if entry.get('task') == task][-10:]
    
    for step in range(config.max_steps):
        messages = build_planner_prompt(task, task_memory)
        response = ask_ai(messages)

        logger.info(f"\n[AI RAW]\n{response}\n")  # debug

        # Retry JSON parsing within same step
        data = None
        for retry in range(3):  # Max 3 retries for JSON parsing
            data = extract_json(response)
            if data:
                break
            logger.error("JSON parsing failed")
            logger.debug(f"Raw AI response: {response}")
            if retry < 2:  # Don't ask AI on last retry
                # Ask AI to fix the JSON
                fix_messages = [
                    {"role": "system", "content": "You returned invalid JSON. Fix it and return ONLY valid JSON."},
                    {"role": "user", "content": f"Your response: {response}\n\nReturn valid JSON only."}
                ]
                response = ask_ai(fix_messages)

        # ONLY return if completely unusable after retries
        if not data:
            logger.warning("Failed to get valid JSON after 3 attempts")
            continue  # Don't consume step, continue to next iteration

        action = data.get("action")

        # FINISH
        if action == "finish":
            return "Done"

        if action not in ("run_terminal", "finish"):
            logger.warning(f"Unknown action '{action}', skipping step...")
            continue

        # MUST execute before anything else
        command = data.get("command")
        if not command:
            logger.warning("No command in response, retrying...")
            continue  # Don't consume step

        result = execute(command)

        # Post-execution verification
        if not result.startswith("ERROR"):
            verify_messages = [
                {"role": "system", "content": "You are a verifier. Determine if the last command made progress toward the original task. Output JSON: {\"progress\": true/false, \"reason\": \"...\", \"next_command\": \"optional command to retry\"}"},
                {"role": "user", "content": f"Task: {task}\nLast command: {command}\nOutput: {result}\nDid it make progress? If not, suggest a single alternative command."}
            ]
            verify_response = ask_ai(verify_messages)
            verify_data = extract_json(verify_response)
            if verify_data and verify_data.get("progress") is False:
                # No progress - retry with the suggested alternative
                alt_cmd = verify_data.get("next_command")
                if alt_cmd:
                    logger.warning(f"No progress, retrying with: {alt_cmd}")
                    result = execute(alt_cmd)
                    command = alt_cmd  # Update command for memory

        logger.info(f"[Step {step}] {command}")
        logger.info(f"Result: {result}")

        # Save memory
        entry = {
            "task": task,
            "step": step,
            "command": command,
            "result": result
        }

        # Update both task_memory and all_memory
        task_memory.append(entry)
        all_memory.append(entry)
        save_memory(all_memory)  # Save entire memory list

    return " Max steps reached"

# ================= SETUP =================

def setup():
    # Parse command line arguments
    if '--help' in sys.argv or '-h' in sys.argv:
        print("""
AI Linux Agent - A secure Linux task automation tool

USAGE:
    python linuxosaiassistant.py [OPTIONS]

OPTIONS:
    --dry-run    Show commands without executing them
    --help, -h   Show this help message

EXAMPLES:
    python linuxosaiassistant.py --dry-run
    python linuxosaiassistant.py

""")
        exit(0)
    
    if '--dry-run' in sys.argv:
        config.dry_run = True
        print(" DRY RUN MODE - No commands will be executed")
    
    print("Select backend:")
    print("1. Ollama")
    print("2. LM Studio")
    print("3. API")

    choice = input("> ")

    if choice == "1":
        config.backend = "ollama"
        config.ollama_model = input("Ollama model (e.g. llama3): ")
    elif choice == "2":
        config.backend = "lmstudio"
        config.model_name = input("Model name: ")
    elif choice == "3":
        config.backend = "api"
        config.api_url = input("API base URL (e.g. https://openrouter.ai/v1/chat/completions): ").strip()
        config.api_key = input("API key: ").strip()
        config.model_name = input("Model name: ")
    else:
        print("Invalid choice")
        exit()

    # Safety settings
    auto_confirm = input("Auto-execute commands? (y/N): ").strip().lower()
    config.auto_execute = auto_confirm in ['y', 'yes']
    # Enforce safe default
    if config.auto_execute:
        print("Auto-execute is risky. Consider using --dry-run first.")
    
    # Step limit
    steps_input = input(f"Max steps (default {config.max_steps}): ").strip()
    if steps_input:
        try:
            config.max_steps = int(steps_input)
            if config.max_steps < 1:
                config.max_steps = 5
        except ValueError:
            print("Invalid number, using default")
    
    # Memory limit
    memory_input = input(f"Memory limit (default {config.memory_limit}): ").strip()
    if memory_input:
        try:
            config.memory_limit = int(memory_input)
            if config.memory_limit < 1:
                config.memory_limit = 20
        except ValueError:
            print("Invalid number, using default")
    
    # Command timeout
    timeout_input = input(f"Command timeout seconds (default {config.command_timeout}): ").strip()
    if timeout_input:
        try:
            config.command_timeout = int(timeout_input)
            if config.command_timeout < 1:
                config.command_timeout = 30
        except ValueError:
            print("Invalid number, using default")

    print("\nDetected system:")
    print(json.dumps(SYS_INFO, indent=2))
    print(f"Auto-execute: {config.auto_execute}")
    print(f"Max steps: {config.max_steps}")
    print(f"Memory limit: {config.memory_limit}")
    print(f"Command timeout: {config.command_timeout}s")
    if config.dry_run:
        print("\n DRY RUN MODE - Commands will be shown but not executed")
    print()

# ================= MAIN =================

def main():
    try:
        setup()

        print("AI Linux Agent Ready (type 'exit')\n")

        while True:
            try:
                task = input("> ")
                if task.lower() in ["exit", "quit"]:
                    break

                result = run_agent(task)
                logger.info(f"Final result: {result}\n")
            except KeyboardInterrupt:
                logger.warning("Task interrupted by user")
                continue
            except EOFError:
                logger.info("Goodbye!")
                break
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                continue
    except KeyboardInterrupt:
        print("\n\n👋 Goodbye!")
    except Exception as e:
        logger.error(f"Fatal error: {e}")

if __name__ == "__main__":
    main()
