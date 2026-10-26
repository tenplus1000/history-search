# history-search

English | [繁體中文](README.zh-TW.md)

A local, read-only MCP server that lets Claude Code, Codex and other MCP clients search your past
conversations: Claude Code and Codex sessions, Gemini CLI sessions, a ChatGPT data export and your own
markdown notes, all in one SQLite database. Every result points back to the exact source: file and
line for JSONL, JSON pointer and conversation URL for ChatGPT.

No model, no embeddings, no API key, no network port. Original files are never modified.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/tenplus1000/history-search.git
cd history-search
uv sync
cp sources.example.json sources.json   # then edit the paths
.venv/bin/python import_history.py     # first import can take a while
```

## Connect it

Claude Code:

```sh
claude mcp add --scope user history-search -- /path/to/history-search/.venv/bin/python /path/to/history-search/server.py
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.history-search]
command = "/path/to/history-search/.venv/bin/python"
args = ["/path/to/history-search/server.py"]
```

Then ask in a conversation, for example: "Use history-search to find where we decided the cache layout,
read the context around it and cite the source."

## Tools

| Tool | What it does |
|---|---|
| `search_history` | Literal keyword search. Space-separated terms must all appear in one ~6000-character passage; quote phrases. Filters: `provider`, `role`, `project`, `title`, `after`/`before` (ISO, UTC), `include_tools`. |
| `read_context` | Messages around a hit. ChatGPT follows the branch the hit is on and stops at forks. |
| `read_message` | Full text of one message, paged. |
| `get_source` | Platform, title, date, original id, file and line / JSON pointer, URL when known. |
| `archive_status` | What is imported and when. |
| `refresh_history` | Imports changed source files. Only the database is written. |

Tool calls and reasoning are excluded from search unless `include_tools` is set.

## Sources

`sources.json` lists where to read from. Each entry has a `provider`, a `path` and a `glob`:

| Provider | Reads |
|---|---|
| `claude` | Claude Code `~/.claude/projects/**/*.jsonl`, subagent transcripts kept as separate conversations |
| `codex` | Codex `rollout-*.jsonl` and the compressed `rollout-*.jsonl.zst` (line numbers refer to the decompressed file) |
| `gemini` | Gemini CLI `session-*.jsonl` |
| `chatgpt` | `conversations-*.json` from a ChatGPT data export, with all branches |
| any other name | Markdown files, split at `## ` headings and `**Speaker**` lines |

Optional keys for markdown sources: `exclude` (folder names to skip), `project`, `timezone` (used for
dates taken from file names such as `20260413_101500.md`, default UTC), `speakers` (extra `**Name**`
labels and their role) and `provider_from_subfolder` (treat `<path>/claude/...` as provider `claude`).
See `sources.example.json`.

Imports are incremental per file. When a source file is deleted, what was imported from it stays in the
database. The database can always be rebuilt from the sources.

## Search notes

- Terms of three or more characters use an FTS5 trigram index.
- Two-character Chinese, Japanese or Korean terms use a separate bigram index, so `記憶` is as fast as
  `記憶整理`. Other one- and two-character terms fall back to a scan.
- Search is literal, not semantic: if nothing comes back, try other words.

## Tests

```sh
.venv/bin/python -m unittest -v test_history
.venv/bin/python smoke_mcp.py --query <a word in your archive>
```

## License

MIT
