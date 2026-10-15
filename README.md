<p align="center">
  <img src="assets/ethgent-cover-coral.svg" alt="Ethgent - AI-powered multi-agent automation orchestrator" width="800" />
</p>

<p align="center">
  <strong>A customisable LLM agent: an orchestrator, sub-agents and tools that you shape to a domain.</strong>
</p>

<p align="center">
  <a href="https://www.python.org/"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue" /></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green" /></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#make-it-your-own">Make it your own</a> ·
  <a href="#the-agents">The agents</a> ·
  <a href="#terminal-commands">Commands</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#testing">Testing</a>
</p>

---

## What it is

- **Standalone.** Clone it, run `./run.sh`, and you have an orchestrator LLM (`BaseModel`) that delegates to specialised sub-agents, a terminal to manage it, and its own logs, run history and cost tracking. It is a product you operate, not a framework you assemble.
- **Configured, not coded.** What the agent *is for* is decided by its configuration: which sub-agents exist, which tools they own, and what their prompts say. Agents and tools live in YAML and in packs, and can be created, edited, pulled from another repository and shared from the terminal.
- **Social media by default, any domain by design.** It ships tuned for **social media and content** (X, Instagram, YouTube, image and video content, browser automation, research), but that is a configuration, not a limit. See [Make it your own](#make-it-your-own).
- **Runs inside your backend too.** The same agent runs headless as a queue worker, an MCP endpoint or a library in your Python code ([Running inside your backend](#running-inside-your-backend)).

## Features

| | |
|---|---|
| **Agents** | Content creator (text, images, video), social media (X, Instagram, YouTube), browser, research, system and VLM agents. Two are on by default, see [The agents](#the-agents). |
| **Build and share** | **Agent Studio** adds, toggles and reconfigures agents and tools from YAML, no code changes. **Agent packs** are plug-and-play bundles of agents, tools and prompts. |
| **Run it** | **Heartbeat scheduler** (cron or interval jobs, APScheduler) and an **interactive terminal** to create, edit, copy, test and delete agents and tools, schedule jobs and review logs, all without a restart. |
| **Observe it** | **Operations store**: persistent logs, per-run history (what a job did, how long it took, what it cost) and LLM token usage, queryable from the terminal. |
| **Keep it safe** | Risky actions ask for approval. **Remote management** over Telegram or Discord is limited to allow-listed admins, with remote code upload deliberately blocked. |

## Quick Start

**You need:** Python 3.11+, and Chrome plus an active X (Twitter) session in a Chrome profile if you want the social features.

```bash
git clone https://github.com/ethosoftai/Ethgent.git
cd Ethgent
chmod +x run.sh
./run.sh
```

`run.sh` creates a `.venv` if there is none, installs the packages from `requirements.txt`, and launches the interactive terminal (`python -m MarketingApp.main`).

For PNG and video rendering, install the Playwright browser once:

```bash
source .venv/bin/activate
playwright install chromium
```

Then copy `.env.example` to `.env`, add your keys ([Configuration](#configuration)), and type a message to chat, or `/help` for the commands.

## Make it your own

The shipped agents are social-media ones, but the machinery is general: **an agent is a prompt, a model and a set of tools, and a tool is a Python function.** To point Ethgent at another domain you write tools, give an agent those tools and a prompt, try it, and, if you like, package it for others. Everything below happens in the terminal (or in `config/*.yaml`), without touching Ethgent's code.

> [!NOTE]
> The names below are illustrations. None of these tools ship with Ethgent.

### 1. Write a tool

A tool is a plain function in a Python file. Its docstring (or the `--desc` you pass) is how the model decides when to call it, so say what it does and what it returns.

```python
# ~/tools/prices.py
def get_price(symbol: str) -> dict:
    """Return the latest price of a symbol, e.g. get_price("AAPL"). Read-only."""
    ...
    return {"symbol": symbol, "price": 123.4}
```

```
/tool create --file ~/tools/prices.py --all          # every public function becomes a tool
/tool create get_price --file ~/tools/prices.py      # or just one, named after its function
/tool show get_price                                 # check what was registered
```

The code is compiled and imported before it is accepted, so a broken tool is rejected immediately (details in [Custom tools](#custom-tools)). Secrets it needs go in with `--env NAME=value`, which writes to the gitignored `.env.model`.

### 2. Give it to an agent

Create a sub-agent that owns the tool, with its own prompt and, if you want, its own model.

```
/agent create trading_agent --tools get_price,bellek_yaz,bellek_oku \
    --prompt "You analyse positions and write a short journal entry. Never place orders." \
    --desc "Market data and journaling"
```

- Change it later with `/agent edit trading_agent --add-tools get_candles --prompt "..." --model <name>`, copy it with `/agent copy`, inspect it with `/agent show trading_agent`.
- The same fields live in `config/agents.yaml`: `name`, `description`, `model`, `system_prompt`, `tools`, `enabled`.
- To change how the orchestrator itself behaves, use `/prompt set "..."` (and `/prompt reset` to go back).

> [!TIP]
> The orchestrator reaches your tool only through this sub-agent, so giving the agent read-only tools is how you keep it read-only. A tool marked as risky asks for approval before it runs.

### 3. Try it

Run the one agent directly, before involving the orchestrator, and look at the run it recorded:

```
/agent test trading_agent "What is the latest price of AAPL?"
/runs 5
/logs 20 --grep trading_agent
```

When it behaves, ask the same thing in the normal chat and the orchestrator will delegate to it.

### 4. Package and share it

Export the agent with its tools and prompt as a pack, put the folder in a GitHub repository, and anyone can install it:

```
/agent pack export trading_pack --agents trading_agent --out ~/trading_pack   # you
# push ~/trading_pack to github.com/you/trading-pack                          # you

/agent pack install github:you/trading-pack                                   # anyone else
```

The installer shallow-clones the repository, shows a preview of the agents and tools it would add, and asks before installing. The export lists environment variable **names** only, never values. More in [Sharing a setup](#sharing-a-setup).

> [!IMPORTANT]
> A pack carries code. Its tools run in a **sandbox**: a throwaway container per call that sees none of the agent's keys, only the variables the pack declared, with memory, CPU and time limits. Add `--trust` only for packs you wrote or reviewed; then they run in the agent's own process. See [Sandbox for installed packs](#sandbox-for-installed-packs).

### Other domains work the same way

An HR agent would have tools such as `list_applicants` and `read_cv` and a prompt about screening against a role description; a support agent would have tools over a ticket system. Only the tools and the prompt change.

Ready-made connectors and workers that join Ethgent to an application or to platform APIs live in the separate open pool [Marketing Agent Assets](https://github.com/Ahmet2001/MarketingPool/tree/main/marketing-agent-assets). Ethgent does not require it.

## The agents

The orchestrator (`BaseModel`) does not call tools directly: it hands a task to a **sub-agent**, and each sub-agent owns its own model and its own set of tools. Nine sub-agents ship in `config/agents.yaml`; two are on by default. Two of the others connect the agent to an application's backend (shown dashed). You can add more with `/agent create` or a pack.

```mermaid
flowchart TB
    U([You: terminal, Telegram, Discord, heartbeat job]) --> O
    O[BaseModel<br/>orchestrator] --> SM
    O --> CC
    subgraph on[On by default]
        SM[sosyal_medya_agent<br/>X, Instagram, YouTube]
        CC[content_creator_agent<br/>images, video, captions]
    end
    O -.-> BR
    O -.-> CO
    O -.-> AR
    O -.-> SI
    O -.-> VL
    subgraph off[Shipped, off by default: /agent NAME on]
        BR[browser_agent<br/>Selenium browsing]
        CO[computer<br/>empty template]
        AR[arastirma_agent<br/>deep web research]
        SI[sistem_agent<br/>files, system tasks]
        VL[vlm_agent<br/>screen, keyboard, mouse]
    end
    O -.-> AC
    O -.-> PD
    subgraph added[Connection to a backend, off by default]
        AC[asset_collector_agent<br/>an app's approved media]
        PD[platform_data_agent<br/>read-only platform data]
    end
    style added stroke-dasharray: 5 5
```

| Sub-agent | What it does | Default | Model |
|---|---|---|---|
| `sosyal_medya_agent` | Publishing, engagement and notification scanning on X, Instagram and YouTube, driven through a signed-in browser session | on | `default` (follows `SUBMODEL_MODEL_NAME`) |
| `content_creator_agent` | Captions, stock photo and video search, website-to-post extraction, HTML/CSS to PNG posts, MP4 reels | on | `default` |
| `browser_agent` | Selenium-based navigation, DOM reading and form interaction | off | `browser_default` (follows `BROWSER_AGENT_MODEL`) |
| `computer` | A blank Agent Studio template for computer-related tasks: no tools until you attach some | off | `default` |
| `arastirma_agent` | Multi-query web research and report writing | off | Gemini Live (needs `GEMINI_API_KEY`) |
| `sistem_agent` | File and workspace management, system status, terminal commands | off | Gemini Live |
| `vlm_agent` | Screen capture and mouse/keyboard control with a self-verifying vision loop | off | Gemini Live |
| `asset_collector_agent` | Lists, validates and prepares the approved media an application exposes; asks the application to generate a video | off | `default` |
| `platform_data_agent` | Asks a separate worker for read-only platform data; never holds the platform's credentials | off | `default` |

Turn one on with `/agent <name> on`, inspect it with `/agent show <name>`, and write your own with `/agent create` ([Terminal Commands](#terminal-commands)). Tools marked as risky ask for approval in the terminal before they run.

**The two that connect to a backend.** `asset_collector_agent` reads an application's approved media through the `asset-pool` contract (`APP_INTERNAL_URL`, `APP_INTERNAL_TOKEN`). `platform_data_agent` queues read-only data requests for a separate worker and never holds the platform's credentials (`TOOLBOXES_DIR`, the queue settings). The contract, the workers and the toolboxes are in [Marketing Agent Assets](https://github.com/Ahmet2001/MarketingPool/tree/main/marketing-agent-assets); [MarketingPool](https://github.com/Ahmet2001/MarketingPool) runs them all together with Docker. Without those settings the two return a clear configuration error, which is why they are off by default. They show that connecting to a system is a sub-agent with its own tools, not a change to the orchestrator.

## Architecture

```
main.py
 ├─ background tasks: heartbeat_loop, telegram bot (optional), discord bot (optional)
 └─ TerminalManager (foreground)
       └─ AutomationCoordinator (mutex: only one caller touches BaseModel/browser at a time)
             └─ BaseModel.text_query()
                   └─ tool-calling loop (≤12 turns, repeat-call guard)
                         ├─ base tools (memory, workspace, search, …)
                         └─ SubModel agents (each runs its own inner LLM + tool loop)
```

- **`BaseModel`** (`MarketingApp/llms/BaseModel.py`) is the orchestrator: an OpenAI-compatible chat-completions loop that calls tools and sub-agents until it has a final answer.
- **`SubModel`** agents (`MarketingApp/llms/SubModels/`) are self-contained mini-agents, each with their own model and tool subset, exposed to `BaseModel` as a single callable tool.
- **`AutomationCoordinator`** (`MarketingApp/environments/automation_runtime.py`) is a lock ensuring the terminal, heartbeat, and Telegram/Discord triggers never touch the shared browser session concurrently.
- **`telemetry`** (`MarketingApp/telemetry.py`) is the operations store: a `run` is opened per unit of work (a chat turn, a heartbeat job) and carried in a `ContextVar`, so every log line and LLM call made inside it is attached to it automatically.
- **`Agent Studio`** (`MarketingApp/llms/agent_studio.py`) reads `config/agents.yaml`, `config/custom_tools.yaml`, and `config/agent_packs.yaml` to assemble the runtime — agents and tools can be added, toggled, or reconfigured without touching code.

## Configuration

Copy `.env.example` to `.env` and fill in your keys. Settings are loaded in this order (later files override earlier ones): `.env` → `.env.local` → `.env.model` → `.env.secrets`.

| File | Purpose |
|---|---|
| `.env` | Base model/provider settings, Telegram token |
| `.env.local` *(optional)* | Local overrides, kept out of version control |
| `.env.model` *(optional)* | Model-specific overrides |
| `.env.secrets` *(optional)* | Additional API keys (Pexels, etc.) |

**Key variables**

| Variable | Description |
|---|---|
| `MODEL_PROVIDER` | `gemini` or an OpenAI-compatible provider |
| `OPENAI_COMPAT_BASE_URL` | Base URL for the OpenAI-compatible endpoint |
| `BASE_MODEL_NAME` / `SUBMODEL_MODEL_NAME` / `BROWSER_AGENT_MODEL` | Model IDs per role |
| `GEMINI_API_KEY` / `GEMINI_API_KEY_SECONDARY` | Gemini API keys (with failover) |
| `TELEGRAM_TOKEN` / `DISCORD_TOKEN` | Optional chat platform integrations |
| `PEXELS_API_KEY` | Stock photo/video search for the Content Creator agent |
| `TELEGRAM_ALLOWED_USER_IDS` / `DISCORD_ALLOWED_USER_IDS` | Who may chat with the bot (see [Telegram and Discord](#telegram-and-discord)) |
| `TELEGRAM_ADMIN_IDS` / `DISCORD_ADMIN_IDS` | Who may run management commands remotely |
| `ETHGENT_TELEMETRY_RETENTION_DAYS` | How long logs/runs/usage are kept (default `30`, `0` = forever) |
| `ETHGENT_SANDBOX` | Where installed pack tools run: `auto` (default), `container` or `subprocess`; see [Sandbox for installed packs](#sandbox-for-installed-packs) |

> [!CAUTION]
> All API keys and tokens live only in the gitignored `.env*` files (`.env`, `.env.local`, `.env.model`, `.env.secrets`). Never commit real credentials.

## Terminal Commands

Once running, type a message to chat with Ethgent, or use a command. `/help` prints everything below. Destructive commands ask for confirmation unless you pass `--yes`; `--dry-run` on `agent create/edit` and `heartbeat add` previews the result without saving.

### Agents

| Command | Description |
|---|---|
| `/agents`, `/agent list` | List agents |
| `/agent <name> on\|off\|toggle` | Enable/disable an agent |
| `/agent show <name>` | Type, model, tool list, prompt and any config errors for one agent |
| `/agent create <name> [flags]` | Create an agent (`--model`, `--tools`, `--tool-group`, `--tool-category`, `--prompt`, `--desc`, `--dry-run`) |
| `/agent edit <name> [flags]` | Change one (`--add-tools`, `--remove-tools`, `--remove-tool-group`, `--enable`/`--disable`, …) |
| `/agent copy <from> <to>` | Clone an agent with its resolved tool list |
| `/agent test <name> "task"` | Run a single agent directly and record it as a run |
| `/agent delete <name> [--yes]` | Delete a config agent |
| `/agent pack list\|preview\|install\|export` | Manage agent packs, see [Sharing a setup](#sharing-a-setup) |
| `/prompt`, `/prompt default`, `/prompt set "..."`, `/prompt reset` | Show, set or reset the orchestrator's own system prompt |

### Tools

| Command | Description |
|---|---|
| `/tools [query] [--group G] [--category C] [--risk high]` | List and filter tools; `--list-groups` shows the groups, categories and risk split |
| `/tool <name> on\|off\|toggle` | Enable/disable a tool |
| `/tool create\|edit\|show\|delete\|list` | Manage custom tools, see [Custom tools](#custom-tools) |

### Scheduling

| Command | Description |
|---|---|
| `/heartbeat` | Scheduler and job status |
| `/heartbeat add --cron X --gorev "..."` | Add a scheduled task (`startup`, `*/N` or `HH:MM`) |
| `/heartbeat remove <id>`, `show <id>`, `on`, `off` | Remove/inspect a task, enable/disable the scheduler |
| `/heartbeat run\|pause\|resume <id>`, `reload` | Control jobs, reload the config |
| `/heartbeat log [id] [n]` | Past runs of a job: when, how long, what it produced |

### Observing

| Command | Description |
|---|---|
| `/status` | Model, provider, uptime, channels, config errors, store health and today's token usage |
| `/logs [n] [--since 24h] [--type T] [--grep text] [--run id]` | Persistent logs (`--memory` = this process only) |
| `/runs [n] [--source S] [--status S] [--since 24h]` | Run history across the terminal, heartbeat, Telegram and Discord |
| `/run <id>` | One run in detail: duration, error, token breakdown by agent/model, its log lines |
| `/usage [--since 24h] [--by agent\|model\|source\|day\|run]` | LLM token usage, plus a cost estimate if `config/pricing.yaml` exists |
| `/errors [text]` | Config problems that were previously collected but never shown |

### Settings and memory

| Command | Description |
|---|---|
| `/reload` | Reload agent/custom tool config |
| `/provider` | Show the active provider/model and which agents have a literal (non-default) model pinned in `agents.yaml` |
| `/provider set <name> [--base-model M] [--submodel-model M] [--browser-model M] [--base-url URL] [--api-key K] [--reset-pins] [--dry-run]` | Switch provider/model, writing to `.env.model`; `--reset-pins` un-pins agents back to the `default` sentinel so they follow the new provider |
| `/memory [category] [key]`, `/memory search <text>`, `/memory delete <category> <key> --yes` | Inspect, search and remove entries from the agent's long-term memory (`bellek_yaz`/`bellek_oku`) without going through chat |
| `/history`, `/clear`, `/exit` | Chat history / shut down |

> [!NOTE]
> **Switching providers.** Each agent's `model:` field in `agents.yaml` is normally the sentinel `default` (or `browser_default`), which is re-resolved from `SUBMODEL_MODEL_NAME`/`BROWSER_AGENT_MODEL` on every boot. An agent that was ever given an explicit `--model` (via `/agent create`, `edit` or `copy`) is pinned to that literal name and does **not** follow a later `/provider set`; use `--reset-pins` to un-pin it. A model *name* change takes effect immediately via `/reload`, but a provider swap (`--base-url`/`--api-key`) fully applies only after a restart, since the HTTP clients are built once at boot.

### Custom tools

```
/tool create fiyat_getir --file ~/fiyat_getir.py --desc "Returns a price"
/tool create --file ~/kripto.py --all            # every public function in the file becomes a tool
/tool create --file ~/kripto.py --names a,b      # only these, sharing one copy of the file
/tool create x --code "def x(): return 1" --desc "inline"
```

The code is compiled, checked for a function named after the tool, and actually imported before it is accepted, so a broken tool is rejected at write time. The file is **copied** into `workspace/custom_tools/`; re-run `/tool edit <name> --file …` to pick up later changes. `--env NAME=value` writes to the gitignored `.env.model`; a bare `--env NAME` only records the requirement.

### Sharing a setup

```
/agent pack export my_pack --agents my_agent --out ~/my_pack   # or --all
/agent pack install ~/my_pack
/agent pack install github:user/repo[@branch][#sub/dir]
```

`export` writes `plugin.yaml`, `agents/`, `prompts/`, `tools/`, a README and an `env.example` that contains variable **names only**, never values. `install` from GitHub shallow-clones the repository, shows the preview, and asks before installing.

- **Builtin tools** cannot be packaged: they already exist in every install.
- **Builtin agents** can. One you scaffolded yourself (`/agent create --builtin`) travels with its own `submodels/<name>.py` source and installs even where it doesn't exist yet. Because that source runs in the agent's own process, a pack carrying it installs only with `--trust`. One of the seven agents the app ships with (`sosyal_medya_agent`, `content_creator_agent`, …) travels as config only (model, tools, prompt), since the target install already has its code.

### Sandbox for installed packs

Isolation follows trust, not the number of agents. Your own tools (`/tool create`) and the built-in ones run in the agent's process. Tools that arrive in a pack (`/agent pack install`) run in a sandbox; the sub-agent that owns them stays in the agent's process and calls into the sandbox for each tool call. The tool's code is never executed in the agent's process: its name, docstring and parameters are read from the source.

| Mode | When | What the tool gets |
|---|---|---|
| `container` | `ETHGENT_SANDBOX=container`, or `auto` when Docker is reachable | A throwaway container per call: read-only filesystem except `/tmp`, an unprivileged user, all capabilities dropped, memory, CPU, process and time limits, and optionally no network. The tool file is mounted read-only; the agent's installed packages are shared read-only so a tool can `import requests`. |
| `subprocess` | `ETHGENT_SANDBOX=subprocess`, or `auto` without Docker | A separate Python process with the same memory, CPU and time limits. It shares the host's filesystem and network, so it is weaker than a container. |

In both modes the tool sees **only the environment variables it declared**, never the agent's model keys, chat tokens or database keys. A pack cannot get those by declaring their names: variables the agent itself uses (model keys, `TELEGRAM_*`, `SUPABASE_*`, …) are withheld even if declared. A tool that runs past its time limit is stopped, and one that ignores that is killed.

| Variable | Default | Meaning |
|---|---|---|
| `ETHGENT_SANDBOX_TIMEOUT` | `60` | Seconds per tool call |
| `ETHGENT_SANDBOX_MEMORY_MB` | `512` | Memory limit |
| `ETHGENT_SANDBOX_CPUS` | `1` | CPU share (container mode) |
| `ETHGENT_SANDBOX_NETWORK` | `bridge` | `none` cuts the network (container mode) |
| `ETHGENT_SANDBOX_IMAGE` | `python:<your version>-slim` | Image for container mode |

A tool the pack marks `requires_approval: true` is not sent to the sandbox until the host approves it; see [Approval](#approval-for-tools-that-change-something-outside).

`/agent pack install <src> --trust` installs a pack's tools in the agent's process instead, with access to everything the agent can reach; use it only for packs you wrote or reviewed. `/tool show <name>` tells you where a tool runs.

> [!NOTE]
> If the agent itself runs inside a container (as in the MarketingPool Docker setup), Docker is normally not reachable from inside it, so `auto` falls back to `subprocess`. Running a separate sandbox service next to the agent for that case is not built yet.

## Operations: logs, run history, usage

<details>
<summary>Where it is stored, what it records, how cost is estimated</summary>

Everything is stored in `workspace/runtime/telemetry.sqlite` (gitignored) and kept for `ETHGENT_TELEMETRY_RETENTION_DAYS` days.

- **Logs** survive restarts (`/logs`).
- **Runs** record every chat turn and heartbeat job, including skipped ones and retry attempts, with status, duration, error and a summary of the output (`/runs`, `/run`, `/heartbeat log`). The heartbeat's own `job_runtime` table only keeps the *last* run per job; this keeps them all.
- **Usage** records the tokens of every LLM call, attributed to the run, agent, model and channel it happened in (`/usage`).

Cost is estimated only for models you price in `config/pricing.yaml` (USD per 1M tokens); Ethgent ships no prices because they change and model names are install-specific:

```yaml
models:
  gemini-2.5-flash: {input: 0.30, output: 2.50}
```

Known limits: the Gemini Live agents (`arastirma_agent`, `sistem_agent`, `vlm_agent`) produce one **approximate** usage row per session, because the SDK does not document whether Live `usage_metadata` is cumulative; providers that return no usage data are counted as calls with unknown tokens.

</details>

## Telegram and Discord

<details>
<summary>Run the management commands from a chat, with an allow-list</summary>

The bots can run the management commands above, but **who may talk to them is configured by you**:

| Variable | Meaning |
|---|---|
| `<CHANNEL>_ALLOWED_USER_IDS` | Who may chat. **If unset, chat is open to everyone** (the old behaviour) and startup prints a warning. |
| `<CHANNEL>_ADMIN_IDS` | Who may run management commands. If unset they are **disabled**. |

> [!WARNING]
> Set `<CHANNEL>_ALLOWED_USER_IDS`. Without it anyone who finds the bot can chat with your agent.

Send `/id` (Telegram) or `!id` (Discord) to the bot to learn your numeric ID. Unauthorised users get no reply; each attempt is written to the log (`/logs --type remote`).

Management commands are `/agent`, `/tool`, `/heartbeat`, `/usage`, … on Telegram and `!agent`, `!tool`, … on Discord. Even for an admin, remote sessions cannot run `/tool create|edit|delete` or `/tool show --code` (tool code runs on the server), `/agent pack …` (filesystem paths) or `/agent create --builtin`, and `agent delete` / `heartbeat remove` require `--yes`.

</details>

## Running inside your backend

The terminal is one way to run Ethgent. The other is headless, next to your own services, with no one at a keyboard. There are three entry points, and all of them use the same orchestrator, sub-agents and tools:

| Entry point | How a task arrives | Approval comes from |
|---|---|---|
| **Queue worker** (`python -m MarketingApp.worker`) | Your backend inserts a row into the `agent_jobs` table (`migrations/`) with `payload = {"task": ..., "context": ..., "approved_tools": [...]}`; the worker claims it, runs it and writes `results` back | the job's `approved_tools`, set by whoever queued it |
| **MCP** (served by the same worker on `AGENT_WORKER_MCP_PORT`, `Authorization: Bearer AGENT_WORKER_MCP_TOKEN`) | An MCP client calls `run_marketing_task`; a long task returns a job id to poll with `check_marketing_task` | nobody: every gated tool is refused |
| **In-process** (`EthgentAgent` in `MarketingApp/agent_api.py`) | Your Python code calls `await agent.run(task)` | your `approval_handler=`; refused by default |

The worker reads the queue through Supabase's REST API (`SUPABASE_URL` plus a key; a narrowed key is preferred, see `migrations/002_*.sql`). Without those settings it does not poll; without an MCP token the MCP endpoint refuses every request. All settings are in `.env.example`.

```python
from MarketingApp.agent_api import EthgentAgent

agent = EthgentAgent(workspace_dir="/path/to/brandX/workspace")
result = await agent.run("Draft a post about today's topic for X")
print(result.text)
```

Limits to know before you deploy:

- **One process per workspace.** The worker and `EthgentAgent` serialize their own tasks, but two processes on the same workspace (for example the worker and the terminal) do not coordinate. Run more workers with separate workspaces.
- **The pack sandbox needs Docker on the host.** A worker that itself runs in a container usually cannot start containers, so installed packs fall back to the weaker process sandbox (see [Sandbox](#sandbox-for-installed-packs)).
- **Logs and run history are local** (a SQLite file in the workspace); there is no exporter to your monitoring yet.

[MarketingPool](https://github.com/Ahmet2001/MarketingPool) is a complete example: this worker, the queue, the connection workers and an application wired together with Docker Compose.

## Approval for tools that change something outside

A tool that does something irreversible (publishing, sending) asks the approval gate before it acts, with `await request_tool_approval(action_id, description)` from `MarketingApp.environments.approval_runtime`. The answer never comes from the model:

- **At the terminal** the person is asked (`Onayliyor musun? [e/H/o]`); `e`, `evet`, `y` or `yes` approves this call, `o` approves that tool for the rest of the session, anything else refuses.
- **Embedded (`EthgentAgent`)** the gate refuses everything unless you pass your own `approval_handler=` (an `async` function taking `(action_id, description)` and returning a bool).
- **With no handler registered at all** it refuses.
- A host that queues jobs can approve per job: wrap the run in `with job_approvals([...tool names...])` and register `approve_if_granted_by_job`. Only the tools the job names are approved, for the length of that job.

### The built-in tools that post, reply, like or follow

The agent's own social tools pass the same gate: the agent asks before they run, with the tool name and its arguments. These are `publish_x_post`, `publish_x_thread`, `submit_current_x_composer`, `reply_to_x_post`, `send_x_reply`, `quote_x_post`, `repost_x_post`, `like_x_post`, `follow_x_account`, `engage_with_x_post`, `like_instagram_post`, `follow_instagram_account`, `comment_instagram_post`, `like_youtube_video` and `subscribe_youtube_channel` (the list is `BUILTIN_EXTERNAL_WRITE_TOOLS` in `approval_runtime.py`). Tools that only read, only prepare a draft (`publish_x_post_with_media`) or stay private (`bookmark_x_post`) do not ask.

The general browser and terminal tools (`browser_click_*`, `browser_type_*`, `terminal_komut_calistir`) are not gated: they can do anything, so a sub-agent that holds them can also post without asking. Give them only to agents you trust with that.

### Tools from packs declare it; the agent asks

A pack marks a tool that changes something outside with `requires_approval: true` in its `plugin.yaml`:

```yaml
tools:
  - name: send_payment
    file: tools/payments.py
    requires_approval: true
```

For such a tool the agent itself asks the gate above **before** the tool runs, showing the tool name and its arguments. Without an approval the call is refused and nothing is sent to the [sandbox](#sandbox-for-installed-packs); a tool installed with `--trust` is gated the same way in the agent's process. The approval does not depend on the tool's own code, which may not ask at all. In a queued job the action id is the tool name, so `job_approvals(["send_payment"])` approves exactly that tool. `/agent pack preview` and `/tool show` say which tools ask, and `/agent pack export` keeps the declaration.

Workflows exported by the workflow factory with a step that writes outside the machine set `requires_approval: true` and also ask the gate from their own code. Inside the sandbox that inner question gets the agent's answer for that call, so they run only if one of the above approves them.

## Project Structure

<details>
<summary>Folder layout</summary>

```
MarketingApp/
├── agent_api.py         # Embeddable agent wrapper (EthgentAgent)
├── worker.py             # Headless worker: polls the agent_jobs queue and serves MCP (python -m MarketingApp.worker)
├── paths.py              # Central, overridable workspace/config path resolution
├── main.py               # Entry point (python -m MarketingApp.main)
├── telemetry.py           # Persistent logs, run history and token usage
├── araclar/               # Tools: browser, search, memory, content creation, workspace, skills
├── config/                # agents.yaml, custom_tools.yaml, agent_packs.yaml, heartbeat_config.yaml
├── environments/          # terminal.py, heartbeat.py, telegram.py, discord_bot.py, automation_runtime.py,
│                          # access.py (bot allowlists), remote_commands.py (terminal commands over chat)
├── llms/                  # BaseModel orchestrator, Agent Studio, SubModels/
├── legacy/panel/          # Archived FastAPI web panel (superseded by the terminal interface)
└── workspace/              # Runtime data: memory, drafts, assets, custom tools, agent packs
migrations/                  # SQL for the agent_jobs queue table the worker reads
tests/                       # unittest suite (isolated temp workspace)
```

</details>

## Testing

```bash
python -m unittest discover -s tests
```

The suite runs against a temporary workspace and config directory (`tests/_env.py`), so it never touches your real agents, tools or telemetry. Use `discover -s tests` rather than `tests.<module>`: a dependency installs a top-level `tests` package that shadows it.

## Contributing

Issues and pull requests are welcome. Please keep changes scoped and include a short description of what changed and why.

## License

MIT © 2026 Ahmet Rıfat Öztürk. See [LICENSE](LICENSE).
