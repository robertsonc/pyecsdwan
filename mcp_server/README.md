# pyedgeconnect MCP Server

An MCP (Model Context Protocol) server that exposes every function from the
[pyedgeconnect](https://github.com/aruba/pyedgeconnect) Python library as
tools for Claude, other LLMs, and AI agents.

## What's included

| Tool count | Source |
|---|---|
| ~634 | Aruba **Orchestrator** API methods |
| ~105 | Aruba **EdgeConnect** appliance API methods |
| 7 | Connection management & discovery helpers |

**Total: ~747 tools** covering the full SD-WAN lifecycle — appliance
management, tunnels, overlays, alarms, statistics, BGP/OSPF, security
policies, user management, licensing, and more.

## Quick start

### 1. Install dependencies

```bash
pip install mcp requests
```

### 2. Run the server (stdio transport — default for Claude Desktop)

```bash
# From the repository root:
python -m mcp_server
```

### 3. Configure Claude Desktop

Copy `mcp_server/claude_desktop_config.json` into your Claude Desktop
config (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS) and edit the `cwd` and optional `env` fields:

```json
{
  "mcpServers": {
    "pyedgeconnect": {
      "command": "python3",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/pyecsdwan",
      "env": {
        "ORCH_URL": "orchestrator.example.com",
        "ORCH_API_KEY": "your-api-key",
        "ORCH_VERIFY_SSL": "false"
      }
    }
  }
}
```

### 4. Configure Claude Code

Add the MCP server to your Claude Code settings:

```bash
claude mcp add pyedgeconnect -- python3 -m mcp_server
```

## Usage

### Connection management

Before calling any API tool, establish a session:

```
orch_connect(url="10.1.1.100", api_key="abc123")
```

Or with username/password:

```
orch_connect(url="10.1.1.100", user="admin", password="pass")
```

For EdgeConnect appliances:

```
ec_connect(url="10.2.30.50", user="admin", password="admin")
```

### Discovering tools

Use the built-in discovery tools:

- **`list_orchestrator_tools`** — Lists all ~634 Orchestrator tools with
  parameter names and brief descriptions.
- **`list_edgeconnect_tools`** — Lists all ~105 EdgeConnect tools.
- **`tool_help(tool_name)`** — Returns full documentation for any tool
  including all parameter types, defaults, and API endpoint info.

### Calling API tools

Every public method from pyedgeconnect is available with a prefix:

- **`orch_*`** — Orchestrator methods (e.g., `orch_get_appliances`)
- **`ec_*`** — EdgeConnect methods (e.g., `ec_get_appliance_system_info`)

Examples:

```
orch_get_appliances()
orch_get_alarms_from_appliances(ne_pk_list=["3.NE","5.NE"], view="active")
orch_get_appliance_bgp_state(ne_id="3.NE")
ec_get_appliance_tunnels_config()
ec_get_appliance_cpu()
```

### Multiple sessions

You can manage multiple connections simultaneously by using the `session`
parameter:

```
orch_connect(url="orch1.example.com", api_key="key1", session="prod")
orch_connect(url="orch2.example.com", api_key="key2", session="staging")
orch_get_appliances(session="prod")
orch_get_appliances(session="staging")
```

### Disconnecting

```
orch_disconnect()
ec_disconnect()
```

## Environment variables

| Variable | Description |
|---|---|
| `ORCH_URL` | Orchestrator hostname/IP |
| `ORCH_API_KEY` | API key (no login required) |
| `ORCH_USER` | Username for login |
| `ORCH_PASSWORD` | Password for login |
| `ORCH_AUTH_MODE` | `local`, `radius`, or `tacacs` (default: `local`) |
| `ORCH_VERIFY_SSL` | `true` or `false` (default: `false`) |
| `EC_URL` | EdgeConnect hostname/IP |
| `EC_USER` | EdgeConnect username |
| `EC_PASSWORD` | EdgeConnect password |
| `EC_VERIFY_SSL` | `true` or `false` (default: `false`) |

## Architecture

```
mcp_server/
├── __init__.py       # Package marker
├── __main__.py       # Entry point (python -m mcp_server)
├── server.py         # FastMCP server + dynamic tool registration
├── introspect.py     # Extracts method metadata via reflection
├── claude_desktop_config.json  # Example Claude Desktop config
└── README.md         # This file
```

The server uses Python's `inspect` module to introspect all public methods
on the `Orchestrator` and `EdgeConnect` classes at startup. Each method is
wrapped as an MCP tool with:

- Proper parameter names, types, and descriptions extracted from docstrings
- Automatic JSON serialization of responses
- Type coercion for incoming arguments
- Session-based connection management supporting multiple simultaneous
  connections
