# CX Agent

AI-powered shopping agent for SAP Commerce Cloud.

## Installation

```bash
pip install cxagent          # core
pip install cxagent[all]     # all optional dependencies
```

## Quick Start

1. Copy `.env.example` to `.env` and fill in your credentials
2. Run the agent:

```bash
cxagent --mode server    # Start API server
cxagent --mode cli       # Interactive CLI
cxagent --mode check     # Validate config
```

## Optional Dependencies

| Extra | What it adds |
|-------|-------------|
| `redis` | Session persistence with Redis |
| `payments` | Stripe checkout integration |
| `search` | Qdrant vector search |
| `gemini` | Google Gemini LLM support |
| `ollama` | Local LLM via Ollama |
| `all` | Everything above |

## License

Business Source License 1.1 — free for development and testing. Production use requires a commercial license. See [LICENSE](LICENSE) for details.
