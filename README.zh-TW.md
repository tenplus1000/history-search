# history-search

[English](README.md) | 繁體中文

本機唯讀的 MCP server，讓 Claude Code、Codex 等 MCP 客戶端搜尋你過去的對話：Claude Code 與 Codex 的對話、Gemini CLI 的對話、ChatGPT 匯出檔，以及你自己的 markdown 筆記，全部放在同一個 SQLite 資料庫。每筆結果都指回原始出處：JSONL 是檔案和行號，ChatGPT 是 JSON pointer 和對話網址。

不需要模型、embedding、API key，也不開網路 port。原始檔案不會被改動。

## 安裝

需要 Python 3.11 以上和 [uv](https://docs.astral.sh/uv/)。

```sh
git clone https://github.com/tenplus1000/history-search.git
cd history-search
uv sync
cp sources.example.json sources.json   # 再改成你的路徑
.venv/bin/python import_history.py     # 第一次匯入會比較久
```

## 接上

Claude Code：

```sh
claude mcp add --scope user history-search -- /path/to/history-search/.venv/bin/python /path/to/history-search/server.py
```

Codex（`~/.codex/config.toml`）：

```toml
[mcp_servers.history-search]
command = "/path/to/history-search/.venv/bin/python"
args = ["/path/to/history-search/server.py"]
```

接著在對話裡說，例如：「用 history-search 找我們決定快取格式的那次討論，讀前後文，附上出處。」

## 工具

| 工具 | 做什麼 |
|---|---|
| `search_history` | 字面關鍵字搜尋。空格分開的詞要同時出現在同一段約 6000 字的內容裡；詞組用雙引號。可篩 `provider`、`role`、`project`、`title`、`after`／`before`（ISO、UTC）、`include_tools`。 |
| `read_context` | 讀命中訊息的前後文。ChatGPT 沿著命中所在的分支走，遇到分岔就停。 |
| `read_message` | 分頁讀一則訊息的全文。 |
| `get_source` | 平台、標題、日期、原始 id、檔案與行號／JSON pointer，有網址時附網址。 |
| `archive_status` | 匯入了哪些、什麼時候匯入的。 |
| `refresh_history` | 匯入有變動的來源檔，只寫資料庫。 |

除非設了 `include_tools`，工具呼叫與推理內容不列入搜尋。

## 來源

`sources.json` 列出要讀哪裡，每一筆有 `provider`、`path`、`glob`：

| provider | 讀什麼 |
|---|---|
| `claude` | Claude Code 的 `~/.claude/projects/**/*.jsonl`；subagent 的紀錄另算一個對話 |
| `codex` | Codex 的 `rollout-*.jsonl` 與壓縮過的 `rollout-*.jsonl.zst`（行號是解壓後的行號） |
| `gemini` | Gemini CLI 的 `session-*.jsonl` |
| `chatgpt` | ChatGPT 資料匯出裡的 `conversations-*.json`，包含所有分支 |
| 其他名字 | Markdown 檔，依 `## ` 標題與 `**說話者**` 行切段 |

markdown 來源可另加：`exclude`（要略過的資料夾名稱）、`project`、`timezone`（從 `20260413_101500.md` 這類檔名取日期時用的時區，預設 UTC）、`speakers`（額外的 `**名字**` 標記與對應角色）、`provider_from_subfolder`（把 `<path>/claude/...` 當成 provider `claude`）。範例見 `sources.example.json`。

匯入以檔案為單位做增量。來源檔被刪掉時，已經匯入的內容會留在資料庫裡。資料庫隨時可以從來源重建。

## 搜尋說明

- 三個字以上的詞走 FTS5 trigram 索引。
- 兩個字的中文、日文、韓文詞走另一個兩字一組的索引，所以搜 `記憶` 跟搜 `記憶整理` 一樣快。其他一到兩個字的詞會逐筆掃描。
- 這是字面搜尋，不是語意搜尋：沒找到就換個說法。

## 測試

```sh
.venv/bin/python -m unittest -v test_history
.venv/bin/python smoke_mcp.py --query <你紀錄裡有的詞>
```

## 授權

MIT
