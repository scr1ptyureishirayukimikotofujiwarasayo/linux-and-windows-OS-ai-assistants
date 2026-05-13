# ai_assistant.py – Part 1
import os
import sys
import json
import requests
import socket
import subprocess
import re
from datetime import datetime
from duckduckgo_search import DDGS

def check_internet():
    """Return True if internet is reachable (Google DNS)."""
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except OSError:
        return False

def main():
    print("=== Local OS AI Assistant ===")
    # Admin mode selection (informational – no actual elevation)
    admin_choice = input("Run as admin? (y/n): ").lower().strip()
    is_admin = admin_choice == 'y'
    if is_admin:
        print("Admin mode selected (logical only – actual elevation not performed)")
    else:
        print("Non-admin mode selected")

    # Provider selection
    print("\nSelect AI Provider:")
    print("1. Ollama (local)")
    print("2. LM Studio (local)")
    print("3. API Keys (OpenAI, Anthropic, custom)")
    provider_choice = input("Enter 1, 2, or 3: ").strip()

    # ---------- Part 2 ----------
    # Helper functions to get model lists
    def get_ollama_models():
        try:
            resp = requests.get("http://localhost:11434/api/tags", timeout=5)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                return [m["name"] for m in models]
            else:
                print("Failed to connect to Ollama. Is it running?")
                return []
        except Exception as e:
            print(f"Error connecting to Ollama: {e}")
            return []

    def get_lmstudio_models():
        try:
            resp = requests.get("http://localhost:1234/v1/models", timeout=5)
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                return [m["id"] for m in models]
            else:
                print("Failed to connect to LM Studio. Is it running?")
                return []
        except Exception as e:
            print(f"Error connecting to LM Studio: {e}")
            return []

    def get_api_models(base_url, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            resp = requests.get(f"{base_url}/v1/models", headers=headers, timeout=10)
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                return [m["id"] for m in models]
            else:
                print(f"Failed to get models: {resp.text}")
                return []
        except Exception as e:
            print(f"Error: {e}")
            return []

    # Provider-specific logic
    if provider_choice == "1":
        print("\nConnecting to Ollama...")
        models = get_ollama_models()
        if not models:
            print("No Ollama models found. Please ensure Ollama is running and has models installed.")
            sys.exit(1)
        print("Available models:")
        for idx, m in enumerate(models):
            print(f"{idx+1}. {m}")
        model_idx = int(input("Select model number: ")) - 1
        selected_model = models[model_idx]
        provider_type = "ollama"
        api_key = None
        base_url = None

    elif provider_choice == "2":
        print("\nConnecting to LM Studio...")
        models = get_lmstudio_models()
        if not models:
            print("No LM Studio models found. Please ensure LM Studio server is running (default port 1234).")
            sys.exit(1)
        print("Available models:")
        for idx, m in enumerate(models):
            print(f"{idx+1}. {m}")
        model_idx = int(input("Select model number: ")) - 1
        selected_model = models[model_idx]
        provider_type = "lmstudio"
        api_key = None
        base_url = "http://localhost:1234"

    elif provider_choice == "3":
        print("\nAPI Key Provider Setup")
        provider_name = input("Provider name (e.g., OpenAI, Anthropic, custom): ").strip()
        api_key = input("Enter API key: ").strip()
        base_url = input("Enter base URL (e.g., https://api.openai.com or http://localhost:1234): ").strip()
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
        base_url = base_url.rstrip("/")
        print(f"Fetching models from {base_url}...")
        models = get_api_models(base_url, api_key)
        if not models:
            print("No models found or failed to fetch. You may still proceed with a manual model name.")
            manual_model = input("Enter model name manually (or press Enter to exit): ").strip()
            if not manual_model:
                sys.exit(1)
            selected_model = manual_model
        else:
            print("Available models:")
            for idx, m in enumerate(models):
                print(f"{idx+1}. {m}")
            model_idx = int(input("Select model number: ")) - 1
            selected_model = models[model_idx]
        provider_type = "api"
    else:
        print("Invalid choice")
        sys.exit(1)

    print(f"\nSelected model: {selected_model}")
    # Store configuration for the chat loop
    config = {
        "provider_type": provider_type,
        "model": selected_model,
        "api_key": api_key,
        "base_url": base_url,
        "is_admin": is_admin
    }

    # ---------- Part 3 – Chat Loop ----------
    def web_search(query, max_results=3):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
                return results
        except Exception as e:
            print(f"Search error: {e}")
            return []

    def execute_powershell(command, is_admin=False, cwd=None):
        """Execute a PowerShell command and return result object."""
        try:
            # Use -Command parameter; if admin flag is True, we note it but actual elevation requires separate logic
            # For simplicity, we run with current privileges
            result = subprocess.run(
                ["powershell.exe", "-Command", command],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=cwd
            )
            return result
        except subprocess.TimeoutExpired:
            # Return a mock result object with error information
            class MockResult:
                def __init__(self):
                    self.returncode = 1
                    self.stdout = ""
                    self.stderr = "Command timed out after 30 seconds."
            return MockResult()
        except Exception as e:
            class MockResult:
                def __init__(self):
                    self.returncode = 1
                    self.stdout = ""
                    self.stderr = f"Error: {e}"
            return MockResult()

    def execute_cmd(command, is_admin=False, cwd=None):
        """Execute a CMD command and return result object."""
        try:
            result = subprocess.run(
                ["cmd.exe", "/c", command],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=cwd
            )
            return result
        except subprocess.TimeoutExpired:
            class MockResult:
                def __init__(self):
                    self.returncode = 1
                    self.stdout = ""
                    self.stderr = "Command timed out after 30 seconds."
            return MockResult()
        except Exception as e:
            class MockResult:
                def __init__(self):
                    self.returncode = 1
                    self.stdout = ""
                    self.stderr = f"Error: {e}"
            return MockResult()

    def run_os_command(command, shell_type="powershell", is_admin=False, cwd=None):
        """
        Execute a command in PowerShell or CMD.
        Returns (output, success_flag)
        """
        if shell_type.lower() == "powershell":
            result = execute_powershell(command, is_admin, cwd)
        elif shell_type.lower() == "cmd":
            result = execute_cmd(command, is_admin, cwd)
        else:
            return "Invalid shell type. Use 'powershell' or 'cmd'.", False
        
        # Use returncode for accurate success detection
        success = result.returncode == 0
        output = result.stdout + result.stderr
        
        # Log command execution
        log_command(shell_type, command, output, success)
        
        return output, success

    def confirm_action(command, shell_type):
        """Ask user for confirmation before executing a command."""
        print(f"\n⚠️  Command to execute ({shell_type}):")
        print(f"    {command}")
        confirm = input("Execute? (y/n): ").strip().lower()
        return confirm == 'y'

    def log_command(shell_type, command, output, success):
        """Log command execution details to file."""
        with open("command_log.txt", "a", encoding="utf-8") as f:
            f.write(f"\n{'='*50}\n")
            f.write(f"Time   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Shell  : {shell_type}\n")
            f.write(f"Success: {success}\n")
            f.write(f"Command: {command}\n")
            f.write(f"Output :\n{output}\n")

    def trim_output(output, max_chars=300):
        output = output.strip()
        if len(output) > max_chars:
            return output[:max_chars] + f"…[+{len(output)-max_chars}]"
        return output

    def generate_response(messages, config):
        provider = config["provider_type"]
        model = config["model"]

        if provider == "ollama":
            url = "http://localhost:11434/api/chat"
            payload = {"model": model, "messages": messages, "stream": False, "options": {"num_predict": 512}}
            try:
                resp = requests.post(url, json=payload, timeout=60)
                if resp.status_code == 200:
                    return resp.json()["message"]["content"]
                else:
                    return f"Error: {resp.text}"
            except Exception as e:
                return f"Error: {e}"

        elif provider == "lmstudio":
            url = f"{config['base_url']}/v1/chat/completions"
            headers = {"Content-Type": "application/json"}
            payload = {"model": model, "messages": messages, "stream": False, "max_tokens": 512}
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=60)
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"]
                else:
                    return f"Error: {resp.text}"
            except Exception as e:
                return f"Error: {e}"

        elif provider == "api":
            url = f"{config['base_url']}/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config['api_key']}"
            }
            payload = {"model": model, "messages": messages, "stream": False, "max_tokens": 512}
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=60)
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"]
                else:
                    return f"Error: {resp.text}"
            except Exception as e:
                return f"Error: {e}"
        else:
            return "Unknown provider"

    def chat_loop(config):
        print("\n=== Chat Started ===")
        print("Type 'exit' to quit, '/search <query>' to force a web search, '/toggle' to enable/disable auto-search")
        print("Type '!ps <command>' for direct PowerShell execution or '!cmd <command>' for direct CMD execution")
        messages = []
        system_prompt = (
            "You are an autonomous OS assistant. You EXECUTE actions directly — never instruct the user how to do things themselves.\n"
            "When asked to perform ANY system task, you MUST respond with the exact shell command in a code block.\n"
            "ALWAYS use ```powershell or ```cmd blocks. Never describe steps in plain text instead of a command.\n"
            "After a command runs, analyse the output and take the next action automatically if needed.\n"
            "Only ask the user a question if genuinely ambiguous (e.g. which drive, which file). Otherwise act.\n\n"
            "CONTEXT RULES:\n"
            "- Summarise completed multi-step task results into one line before continuing.\n"
            "- Do not repeat command output verbatim in your reply; reference it briefly.\n"
            "- If a previous command output is already in history, do not re-describe it.\n\n"
            "STEP-BY-STEP reasoning is internal only — output the command, not your reasoning."
        )
        messages.append({"role": "system", "content": system_prompt})

        auto_search = True
        internet_available = check_internet()
        if not internet_available:
            print("No internet detected. Web search disabled.")
            auto_search = False

        MAX_TURNS = 20
        while True:
            user_input = input("\nYou: ").strip()
            if user_input.lower() == "exit":
                print("Goodbye!")
                break
            if user_input.lower() == "/toggle":
                auto_search = not auto_search
                print(f"Auto-search is now {'ON' if auto_search else 'OFF'}")
                continue
            if user_input.startswith("/search "):
                query = user_input[8:].strip()
                print(f"Searching for: {query}")
                results = web_search(query)
                if results:
                    search_context = "\n".join([f"- {r['body'][:120]} ({r['href']})" for r in results[:2]])
                    user_input = f"Search:'{query}':\n{search_context}\nAnswer concisely."
                else:
                    user_input = f"I tried to search for '{query}' but got no results. Please answer as best you can."
                # Force no additional auto-search for this turn
            elif user_input.startswith("!ps "):
                # Direct PowerShell command execution
                cmd = user_input[4:].strip()
                if confirm_action(cmd, "powershell"):
                    output, success = run_os_command(cmd, "powershell", config.get("is_admin", False))
                    print(f"\nPowerShell Output:\n{output}")
                    messages.append({"role": "user", "content": f"[ps:{cmd[:40]}]\n{trim_output(output)}"})
                    response = generate_response(messages, config)
                    print(f"\nAssistant: {response}")
                    messages.append({"role": "assistant", "content": response})
                else:
                    print("Command execution cancelled.")
                continue
            elif user_input.startswith("!cmd "):
                # Direct CMD command execution
                cmd = user_input[5:].strip()
                if confirm_action(cmd, "cmd"):
                    output, success = run_os_command(cmd, "cmd", config.get("is_admin", False))
                    print(f"\nCMD Output:\n{output}")
                    messages.append({"role": "user", "content": f"[cmd:{cmd[:40]}]\n{trim_output(output)}"})
                    response = generate_response(messages, config)
                    print(f"\nAssistant: {response}")
                    messages.append({"role": "assistant", "content": response})
                else:
                    print("Command execution cancelled.")
                continue
            else:
                if auto_search and internet_available:
                    SEARCH_KEYWORDS = ["what is", "how to", "latest", "news", "who is",
                                       "why", "explain", "when did", "where is", "current",
                                       "update", "version", "download", "install", "error", "fix", "broken"]
                    OS_ACTION_KEYWORDS = ["open", "run", "start", "kill", "delete", "list", "find",
                                          "move", "copy", "rename", "create", "install", "uninstall",
                                          "check", "disable", "enable", "schedule", "read", "write"]
                    is_os_action = any(kw in user_input.lower() for kw in OS_ACTION_KEYWORDS)
                    should_search = not is_os_action and any(kw in user_input.lower() for kw in SEARCH_KEYWORDS)
                    
                    if should_search:
                        print("(Searching web for context...)")
                        results = web_search(user_input)
                        if results:
                            search_context = "\n".join([f"- {r['body'][:100]}" for r in results[:2]])
                            user_input = f"[Search]\n{search_context}\nQ:{user_input}"
                        else:
                            print("No search results found.")

            messages.append({"role": "user", "content": user_input})
            
            if len(messages) % 8 == 0 and len(messages) > 1:
                messages.append({"role": "user", "content": "[Sys] Be autonomous. Always use ```powershell/```cmd blocks. Never instruct user to run commands."})
            
            # Smart trim: keep system prompt + last N turns; summarise dropped middle
            if len(messages) > 1 + (MAX_TURNS * 2):
                dropped = messages[1:-(MAX_TURNS * 2)]
                summary_content = "[System] Context trimmed. Prior actions summary: " + "; ".join(
                    m["content"][:80].replace("\n", " ") for m in dropped if m["role"] == "assistant"
                )
                messages = [messages[0], {"role": "user", "content": summary_content}] + messages[-(MAX_TURNS * 2):]
            
            print("Assistant is thinking...")
            response = generate_response(messages, config)
            print(f"\nAssistant: {response}")
            messages.append({"role": "assistant", "content": response})
            # Strip assistant messages older than MAX_TURNS to just their first 60 chars
            if len(messages) > 10:
                for i in range(1, len(messages) - 6):
                    if messages[i]["role"] == "assistant" and len(messages[i]["content"]) > 60:
                        messages[i]["content"] = messages[i]["content"][:60] + "…"
            
            # After getting AI response, check for embedded commands
            # Look for pattern: ```powershell ... ``` or ```cmd ... ```
            ps_match = re.search(r'```powershell\n(.*?)\n```', response, re.DOTALL | re.IGNORECASE)
            cmd_match = re.search(r'```cmd\n(.*?)\n```', response, re.DOTALL | re.IGNORECASE)
            if ps_match:
                cmd = ps_match.group(1).strip()
                print(f"\nAssistant proposes PowerShell command:\n{cmd}")
                if confirm_action(cmd, "powershell"):
                    output, success = run_os_command(cmd, "powershell", config.get("is_admin", False))
                    # Append result as a system message so AI can continue
                    messages.append({"role": "user", "content": f"[out]\n{trim_output(output)}"})
                    # Generate a follow-up response to incorporate the output
                    response = generate_response(messages, config)
                    print(f"\nAssistant: {response}")
                    messages.append({"role": "assistant", "content": response})
                    # Auto-chain: check if follow-up response contains another command
                    ps_chain = re.search(r'```powershell\n(.*?)\n```', response, re.DOTALL | re.IGNORECASE)
                    if ps_chain:
                        chain_cmd = ps_chain.group(1).strip()
                        print(f"\nAssistant proposes follow-up command:\n{chain_cmd}")
                        if confirm_action(chain_cmd, "powershell"):
                            output, success = run_os_command(chain_cmd, "powershell", config.get("is_admin", False))
                            messages.append({"role": "user", "content": f"[System] Follow-up output:\n{trim_output(output)}"})
                else:
                    messages.append({"role": "user", "content": "[System] User declined to execute the command."})
            if cmd_match:
                cmd = cmd_match.group(1).strip()
                print(f"\nAssistant proposes CMD command:\n{cmd}")
                if confirm_action(cmd, "cmd"):
                    output, success = run_os_command(cmd, "cmd", config.get("is_admin", False))
                    messages.append({"role": "user", "content": f"[out]\n{trim_output(output)}"})
                    response = generate_response(messages, config)
                    print(f"\nAssistant: {response}")
                    messages.append({"role": "assistant", "content": response})
                else:
                    messages.append({"role": "user", "content": "[System] User declined to execute the command."})

    # Start the chat
    chat_loop(config)

if __name__ == "__main__":
    main()
