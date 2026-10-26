"""Explicit, incremental imports; original records are never modified."""
import argparse
import fcntl
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import zstandard
from store import DB, ROOT, connect, init, put_conversation, put_message, uid

PARSER_VERSION = "2"


def text_content(value):
    if isinstance(value,str): return value
    if isinstance(value,list): return "\n".join(filter(None,(text_content(x) for x in value)))
    if isinstance(value,dict):
        if isinstance(value.get("text"),str): return value["text"]
        if "parts" in value: return text_content(value["parts"])
        if "content" in value: return text_content(value["content"])
        if value.get("asset_pointer"): return "[attachment: "+str(value["asset_pointer"])+"]"
    return ""


def import_chatgpt(c,path):
    data=json.loads(path.read_text())
    for ci,conv in enumerate(data):
        native=conv.get("id") or conv["conversation_id"]
        cid=put_conversation(c,"chatgpt",native,conv.get("title") or "",created=conv.get("create_time"),updated=conv.get("update_time"),url="https://chatgpt.com/c/"+native)
        mapping=conv.get("mapping") or {}; active=set(); cur=conv.get("current_node")
        while cur in mapping and cur not in active:
            active.add(cur); cur=mapping[cur].get("parent")
        for seq,(nodeid,node) in enumerate(mapping.items()):
            msg=node.get("message") or {}; content=msg.get("content") or {}
            body=text_content(content)
            if not body and msg: body=json.dumps(content,ensure_ascii=False)
            role=(msg.get("author") or {}).get("role","system")
            ct=content.get("content_type","")
            kind="dialogue" if role in ("user","assistant") and ct in ("text","multimodal_text") else "reasoning" if ct in ("thoughts","reasoning_recap") else "event"
            put_message(c,cid,nodeid,seq,role,kind,msg.get("create_time"),body,path,f"/{ci}/mapping/{nodeid}",node.get("parent"),int(nodeid in active))


def is_jsonl(path):
    # Codex compresses sessions it has not touched for a while into .jsonl.zst and deletes the original.
    return path.name.endswith((".jsonl",".jsonl.zst"))


def jsonl_rows(path):
    opener=(lambda: zstandard.open(path,"rt",encoding="utf-8")) if path.name.endswith(".zst") else (lambda: path.open(encoding="utf-8"))
    good=0; bad=[]
    with opener() as f:
        for line_no,line in enumerate(f,1):
            if not line.strip(): continue
            try: row=json.loads(line)
            except json.JSONDecodeError:
                # Active writers may leave an incomplete final record. Retry next sync.
                if not line.endswith("\n"): break
                # A writer that crashed mid-record and then resumed leaves one broken line in the middle.
                bad.append(line_no); continue
            good+=1; yield line_no,row
    if bad and not good:
        raise ValueError(f"Invalid JSONL at {path}:{bad[0]}")
    if bad:
        print(f"SKIPPED broken lines in {path}: {','.join(map(str,bad[:20]))}",file=sys.stderr,flush=True)


def import_jsonl(c,path,provider):
    stem=path.name.removesuffix(".zst").removesuffix(".jsonl")
    rows=jsonl_rows(path); cid=None; sid=stem; title=""; project=""
    # Codex rollout filenames contain a full UUID; metadata overrides this fallback.
    match=re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",sid)
    if match: sid=match[0]
    for line_no,o in rows:
        if provider=="codex" and o.get("type")=="session_meta":
            p=o.get("payload") or {}; sid=p.get("id",sid); project=p.get("cwd","")
        elif provider=="gemini" and o.get("sessionId"):
            sid=o["sessionId"]; project=o.get("projectHash","")
        elif provider=="claude":
            sid=o.get("sessionId",sid); project=o.get("cwd",project)
            if "subagents" in path.parts and not sid.endswith("/"+stem):
                sid += "/"+stem
        if cid is None:
            cid=put_conversation(c,provider,sid,project=project,created=o.get("timestamp") or o.get("startTime"))
        timestamp=o.get("timestamp"); entries=[]; parent=None
        if provider=="codex":
            p=o.get("payload") or {}; typ=p.get("type")
            if o.get("type")!="response_item": continue
            native=p.get("id") or f"line:{line_no}"
            if typ=="message":
                role=p.get("role","system"); body=text_content(p.get("content"))
                kind="dialogue" if role in ("user","assistant") else "event"
                if role=="user" and body.lstrip().startswith(("# AGENTS.md instructions", "<environment_context>", "<INSTRUCTIONS>")):
                    kind="event"
                entries=[(native,role,kind,body)]
            elif typ in ("function_call","function_call_output","custom_tool_call","custom_tool_call_output"):
                entries=[(native,"tool","tool",json.dumps(p,ensure_ascii=False))]
            elif typ=="reasoning":
                entries=[(native,"assistant","reasoning",text_content(p.get("summary")))]
        elif provider=="claude":
            msg=o.get("message") or {}
            if not isinstance(msg,dict): continue
            role=msg.get("role"); native=o.get("uuid") or f"line:{line_no}"; parent=o.get("parentUuid")
            content=msg.get("content")
            if role not in ("user","assistant"): continue
            if isinstance(content,str): entries=[(native,role,"dialogue",content)]
            elif isinstance(content,list):
                dialogue=text_content([b for b in content if isinstance(b,dict) and b.get("type")=="text"])
                if dialogue: entries.append((native,role,"dialogue",dialogue))
                for bi,b in enumerate(content):
                    if not isinstance(b,dict): continue
                    typ=b.get("type")
                    if typ in ("tool_use","tool_result"):
                        entries.append((f"{native}:block:{bi}","tool","tool",json.dumps(b,ensure_ascii=False)))
                    elif typ=="thinking" and b.get("thinking"):
                        entries.append((f"{native}:block:{bi}",role,"reasoning",b["thinking"]))
        else:
            typ=o.get("type"); native=o.get("id") or f"line:{line_no}"
            if typ in ("user","gemini"):
                entries=[(native,"user" if typ=="user" else "assistant","dialogue",text_content(o.get("content")))]
                if o.get("toolCalls"): entries.append((native+":tools","tool","tool",json.dumps(o["toolCalls"],ensure_ascii=False)))
        for ei,(native,role,kind,body) in enumerate(entries):
            if not body: continue
            if not title and role=="user" and kind=="dialogue": title=body.strip().split("\n")[0][:150]
            put_message(c,cid,native,line_no*1000+ei,role,kind,timestamp,body,path,f"line:{line_no}",parent)
        if entries: put_conversation(c,provider,sid,title,project,updated=timestamp)


DEFAULT_SPEAKERS={"User":"user","Assistant":"assistant","Claude":"assistant","Codex":"assistant","Gemini":"assistant"}


def import_markdown(c,path,provider,spec=None):
    spec=spec or {}
    speakers={**DEFAULT_SPEAKERS,**spec.get("speakers",{})}
    content=path.read_text(encoding="utf-8",errors="replace"); lines=content.splitlines(keepends=True)
    # A readable forum mirror is a fallback, not a second copy of raw JSONL.
    ref=re.search(r"^Source: `([^`]+)`",content,re.M)
    if ref and Path(ref[1]).is_file(): return
    # A mirror folder may keep each platform in its own subfolder (<root>/claude/...).
    if spec.get("provider_from_subfolder") and spec.get("path"):
        try: section=path.relative_to(Path(spec["path"]).expanduser().resolve()).parts[0]
        except (ValueError,IndexError): section=""
        if section in spec["provider_from_subfolder"]: provider=section
    title=next((s.lstrip("# ").strip() for s in lines if s.strip()),path.stem)[:180]
    date_match=re.search(r"(20\d{2})(\d{2})(\d{2})",path.name)
    ts=""
    if date_match:
        y,mo,d=date_match.groups()
        clock=re.match(r"20\d{6}_(\d{2})(\d{2})(\d{2})(?=[_.-]|$)",path.name)
        h,mi,s=clock.groups() if clock else (0,0,0)
        try: ts=datetime(int(y),int(mo),int(d),int(h),int(mi),int(s),tzinfo=ZoneInfo(spec.get("timezone","UTC"))).isoformat()
        except ValueError: ts=""
    cid=put_conversation(c,provider,str(path),title,project=spec.get("project",""),created=ts,updated=ts)
    # Keep each markdown section separately so debug logs don't swamp dialogue.
    start=1; buf=[]; kind="document"; role="document"; fenced=False
    def flush():
        if buf:
            put_message(c,cid,f"line:{start}",start,role,kind,ts,"".join(buf),path,f"line:{start}")
    for n,line in enumerate(lines,1):
        if line.startswith("```"):
            fenced=not fenced; buf.append(line); continue
        marker=line.strip()
        tool_marker=marker.startswith(("**Tool Use**","**Tool Result", "**Tool Calls**"))
        event_marker=marker.startswith(("**Codex Event**","**Session Meta**","**Reasoning Summary**","**Thoughts**"))
        if not fenced and (line.startswith("## ") or marker in {f"**{name}**" for name in speakers} or tool_marker or event_marker):
            flush(); buf=[]; start=n
            label=line.strip().strip("#* ")
            if tool_marker or any(x in label.lower() for x in ("trace","llm raw","工具","debug")): kind="tool"; role="tool"
            elif event_marker: kind="event"; role="system"
            elif label in speakers:
                kind="dialogue"; role=speakers[label]
        buf.append(line)
    flush()


def backfill_cjk(c,batch=2000):
    """Index chunks imported before the CJK bigram index existed. Resumable: progress is saved per batch."""
    meta=dict(c.execute("SELECT key,value FROM meta WHERE key IN ('cjk_backfill_until','cjk_backfill_done')").fetchall())
    done,until=int(meta.get("cjk_backfill_done",0)),int(meta.get("cjk_backfill_until",0))
    while done<until:
        with c:
            rows=c.execute("SELECT id,body FROM chunks WHERE id>? AND id<=? ORDER BY id LIMIT ?",(done,until,batch)).fetchall()
            c.executemany("INSERT INTO cjk_index(rowid,body) VALUES(?,cjk_bigrams(?))",[(r[0],r[1]) for r in rows])
            done=rows[-1][0] if rows else until
            c.execute("INSERT OR REPLACE INTO meta VALUES('cjk_backfill_done',?)",(str(done),))
        print(f"cjk index {done}/{until}",flush=True)


def discover(config):
    seen=set()
    for spec in config["sources"]:
        root=Path(spec["path"]).expanduser()
        if not root.exists():
            print(f"MISSING {root}",file=sys.stderr); continue
        for p in sorted(root.glob(spec["glob"])):
            p=p.resolve()
            if p in seen or not p.is_file(): continue
            if ".ipynb_checkpoints" in p.parts: continue
            if spec.get("exclude") and any(x in p.parts for x in spec["exclude"]): continue
            seen.add(p); yield spec,p


def run(config_path=ROOT/"sources.json",db=DB):
    if not config_path.exists():
        raise SystemExit(f"{config_path} not found: copy sources.example.json to sources.json and edit the paths")
    config=json.loads(config_path.read_text()); db.parent.mkdir(parents=True,exist_ok=True)
    with (db.parent/"import.lock").open("a") as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError("Another import is running")
        c=connect(db,write=True); init(c)
        c.execute("CREATE TABLE IF NOT EXISTS file_parsers(path TEXT PRIMARY KEY, version TEXT)")
        with c:
            checkpoint_ids=c.execute("SELECT DISTINCT message_id FROM origins WHERE instr(path,'/.ipynb_checkpoints/')>0").fetchall()
            c.execute("DELETE FROM origins WHERE instr(path,'/.ipynb_checkpoints/')>0")
            c.execute("DELETE FROM files WHERE instr(path,'/.ipynb_checkpoints/')>0")
            c.execute("DELETE FROM file_parsers WHERE instr(path,'/.ipynb_checkpoints/')>0")
            c.executemany("DELETE FROM messages WHERE id=? AND NOT EXISTS(SELECT 1 FROM origins o WHERE o.message_id=messages.id)",checkpoint_ids)
        updated=skipped=failed=0
        for spec,p in discover(config):
            provider=spec["provider"]
            st=p.stat(); old=c.execute("SELECT size,mtime FROM files WHERE path=?",(str(p),)).fetchone()
            version=c.execute("SELECT version FROM file_parsers WHERE path=?",(str(p),)).fetchone()
            expected_version="5" if p.suffix==".md" else PARSER_VERSION
            if old and tuple(old)==(st.st_size,st.st_mtime_ns) and version and version[0]==expected_version: skipped+=1; continue
            try:
                with c:
                    old_ids=c.execute("SELECT DISTINCT message_id FROM origins WHERE path=?",(str(p),)).fetchall()
                    c.execute("DELETE FROM origins WHERE path=?",(str(p),))
                    if provider=="chatgpt": import_chatgpt(c,p)
                    elif is_jsonl(p): import_jsonl(c,p,provider)
                    else: import_markdown(c,p,provider,spec)
                    c.executemany("DELETE FROM messages WHERE id=? AND NOT EXISTS(SELECT 1 FROM origins o WHERE o.message_id=messages.id)",old_ids)
                    c.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)",(str(p),provider,st.st_size,st.st_mtime_ns,datetime.now(timezone.utc).isoformat()))
                    c.execute("INSERT OR REPLACE INTO file_parsers VALUES(?,?)",(str(p),expected_version))
                updated+=1
                if updated%20==0 or provider=="chatgpt": print(f"imported={updated} skipped={skipped} latest={p.name}",flush=True)
            except Exception as e:
                failed+=1; print(f"FAILED {p}: {e}",file=sys.stderr,flush=True)
        backfill_cjk(c)
        c.close()
        result={"updated":updated,"unchanged":skipped,"failed":failed}; print(json.dumps(result),flush=True)
        return result


if __name__=="__main__":
    p=argparse.ArgumentParser(); p.add_argument("--config",type=Path,default=ROOT/"sources.json"); p.add_argument("--db",type=Path,default=DB)
    args=p.parse_args(); result=run(args.config,args.db); sys.exit(bool(result["failed"]))
