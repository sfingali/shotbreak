# Shotbreak

AI-powered screenplay breakdown tool with physical description generation for consistent image prompting, MMS export, and multi-LLM support.

## Quick Start

```bash
pip install -e ".[web]"
shotbreak import-script /path/to/script.fountain -n "My Film"
shotbreak run 1 --passes extract --provider deepseek
shotbreak serve
```

Open http://localhost:8190

## Features

- **Import** — Fountain, Final Draft (.fdx), Fade In (.fadein)
- **Breakdown** — AI-powered element extraction (cast, props, wardrobe, locations, VFX, 18 categories)
- **Physical Descriptions** — Two-phase character bible + per-scene rendering with continuity tracking
- **Hierarchical Settings** — Global → location → scene cascading with staleness detection
- **Exports** — MMS (.sex, .MMS10), Final Draft (.fdx), Fade In (.fadein), CSV, PDF
- **Web UI** — SPA with scene navigator, element grid, category filtering, extraction controls
- **Multi-LLM** — DeepSeek, Anthropic, OpenAI — you bring the keys

## Commands

```
shotbreak import-script PATH [-n NAME]     Import a screenplay
shotbreak status                           List projects
shotbreak run PROJECT_ID --passes extract  Run AI breakdown
shotbreak export PROJECT_ID --format fdx   Export to Final Draft
shotbreak bible PROJECT_ID -e ELEMENT_ID   Build character bible
shotbreak describe PROJECT_ID -e ELEMENT_ID  Generate per-scene descriptions
shotbreak setting PROJECT_ID --scope location --key CABIN --value "..."  Set hierarchical settings
shotbreak serve                            Start web UI
```

## Configuration

Create `config.yaml`:

```yaml
providers:
  deepseek:
    kind: openai_compatible
    api_key: "${DEEPSEEK_API_KEY}"
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-v4-pro"

default_provider: "deepseek"
```

API keys via environment variables: `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.

## License

MIT
