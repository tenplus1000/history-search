import json
import tempfile
import unittest
from pathlib import Path
import store
import import_history as imp


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.db=self.root/"test.sqlite3"
        self.c=store.connect(self.db,write=True); store.init(self.c)

    def tearDown(self):
        self.c.close(); self.tmp.cleanup()

    def test_chinese_filters_paging_and_sources(self):
        cid=store.put_conversation(self.c,"codex","s","記憶設計",project="assistant")
        mid=store.put_message(self.c,cid,"1",1,"user","dialogue","2026-05-01T12:00:00Z","甲"*7000+"記憶 中文索引 專案決定",self.root/"raw.jsonl","line:9")
        store.put_message(self.c,cid,"2",2,"tool","tool",None,"記憶 中文索引",self.root/"raw.jsonl","line:10")
        self.c.commit()
        for q in ["記憶","中文索引","記憶 中文索引"]:
            hits=store.search(q,path=self.db)["results"]
            self.assertEqual(len(hits),1); self.assertEqual(hits[0]["message_id"],mid)
            self.assertEqual(hits[0]["origins"][0]["locator"],"line:9")
            self.assertIn("記憶",hits[0]["excerpt"])
        self.assertEqual(len(store.search("記憶",include_tools=True,path=self.db)["results"]),2)
        self.assertFalse(store.search("記憶",provider="chatgpt",path=self.db)["results"])
        self.assertFalse(store.search("記憶",after="2026-06-01",path=self.db)["results"])
        self.assertEqual(store.read_message(mid,start=7000,path=self.db)["text"],"記憶 中文索引 專案決定")
        self.assertEqual(store.search('" OR 1=1 --',path=self.db)["results"],[])
        self.assertTrue(store.search("",title="記憶設計",path=self.db)["results"])
        with self.assertRaises(ValueError): store.search("記憶",after="yesterday",path=self.db)

    def test_two_character_cjk_uses_bigram_index(self):
        cid=store.put_conversation(self.c,"claude","s2","t")
        mid=store.put_message(self.c,cid,"1",1,"user","dialogue","2026-05-01T12:00:00Z","我們討論記憶整理的做法",self.root/"a.jsonl","line:1")
        store.put_message(self.c,cid,"2",2,"user","dialogue","2026-05-01T12:00:01Z","記 憶 分開寫不算",self.root/"a.jsonl","line:2")
        self.c.commit()
        self.assertTrue(store.cjk_ready(self.c))
        hits=store.search("記憶",path=self.db)["results"]
        self.assertEqual([h["message_id"] for h in hits],[mid])
        self.assertEqual(len(store.search("記憶 做法",path=self.db)["results"]),1)
        self.assertFalse(store.search("憶做",path=self.db)["results"])
        # Replacing a message's text removes its old bigrams.
        store.put_message(self.c,cid,"1",1,"user","dialogue","2026-05-01T12:00:00Z","改成別的內容",self.root/"a.jsonl","line:1"); self.c.commit()
        self.assertFalse(store.search("記憶",path=self.db)["results"])

    def test_cjk_backfill_for_existing_database(self):
        self.c.execute("DROP TRIGGER chunks_cjk_ai"); self.c.execute("DROP TRIGGER chunks_cjk_ad")
        self.c.execute("DROP TABLE cjk_index"); self.c.execute("DELETE FROM meta"); self.c.commit()
        cid=store.put_conversation(self.c,"claude","old","t")
        store.put_message(self.c,cid,"1",1,"user","dialogue","2026-05-01T12:00:00Z","舊資料裡的記憶",self.root/"a.jsonl","line:1"); self.c.commit()
        store.init(self.c)
        self.assertFalse(store.cjk_ready(self.c))
        self.assertTrue(store.search("記憶",path=self.db)["results"])   # still found by the slow path
        imp.backfill_cjk(self.c,batch=1)
        self.assertTrue(store.cjk_ready(self.c))
        self.assertTrue(store.search("記憶",path=self.db)["results"])

    def test_broken_line_in_the_middle_is_skipped(self):
        p=self.root/"resumed.jsonl"
        good={"type":"user","sessionId":"s","message":{"role":"user","content":"斷掉之前"}}
        later={"type":"user","sessionId":"s","uuid":"u2","message":{"role":"user","content":"接著寫的內容"}}
        p.write_text(json.dumps({**good,"uuid":"u1"})+"\n"+'{"type":"user","message":{"cont\n'+json.dumps(later)+"\n")
        imp.import_jsonl(self.c,p,"claude"); self.c.commit()
        self.assertTrue(store.search("斷掉之前",path=self.db)["results"])
        hit=store.search("接著寫的內容",path=self.db)["results"][0]
        self.assertEqual(hit["origins"][0]["locator"],"line:3")

    def test_codex_zst_session(self):
        import zstandard
        rows=[{"type":"session_meta","payload":{"id":"zst-session","cwd":"/project"}},
            {"type":"response_item","timestamp":"2026-09-06T10:00:00Z","payload":{"type":"message","role":"user","content":[{"text":"壓縮過的舊對話"}]}}]
        p=self.root/"rollout-2026-09-06T12-30-15-zst.jsonl.zst"
        p.write_bytes(zstandard.ZstdCompressor().compress(("\n".join(json.dumps(r) for r in rows)+"\n").encode()))
        config=self.root/"sources.json"; config.write_text(json.dumps({"sources":[{"provider":"codex","path":str(self.root),"glob":"rollout-*.jsonl*"}]}))
        self.assertEqual(imp.run(config,self.db)["updated"],1)
        hit=store.search("壓縮過的舊對話",path=self.db)["results"][0]
        self.assertEqual(hit["origins"][0]["locator"],"line:2")
        self.assertTrue(hit["origins"][0]["path"].endswith(".jsonl.zst"))
        self.assertEqual(hit["project"],"/project")

    def test_subagent_and_environment_separation(self):
        p=self.root/"env.jsonl"
        p.write_text("\n".join(json.dumps(o) for o in [
          {"type":"session_meta","payload":{"id":"env-1"}},
          {"type":"response_item","payload":{"type":"message","role":"user","content":[{"text":"<environment_context>"}]}},
          {"type":"response_item","payload":{"type":"message","role":"user","content":[{"text":"真正問題"}]}}])+"\n")
        imp.import_jsonl(self.c,p,"codex"); self.c.commit()
        hit=store.search("真正問題",path=self.db)["results"][0]
        self.assertEqual(hit["title"],"真正問題")
        self.assertFalse(store.search("environment_context",path=self.db)["results"])
        agent=self.root/"subagents"/"agent-a.jsonl"; agent.parent.mkdir()
        row={"sessionId":"s","type":"user","uuid":"u","message":{"role":"user","content":"任務紀錄"}}
        p.write_text(json.dumps(row)+"\n"); agent.write_text(json.dumps(row)+"\n")
        imp.import_jsonl(self.c,p,"claude"); imp.import_jsonl(self.c,agent,"claude"); self.c.commit()
        hits=store.search("任務紀錄",path=self.db)["results"]
        self.assertEqual(len({h["conversation_id"] for h in hits}),2)

    def test_markdown_uuid_not_clock(self):
        p=self.root/"forum"/"claude"/"20260413_718b4f17.md"; p.parent.mkdir(parents=True)
        p.write_text("# Claude conversation\n\n## User\n記憶搜尋\n")
        spec={"provider":"forum","path":str(self.root/"forum"),"provider_from_subfolder":["claude","codex","gemini"],"timezone":"Europe/Berlin"}
        imp.import_markdown(self.c,p,"forum",spec); self.c.commit()
        hit=store.search("記憶搜尋",provider="claude",path=self.db)["results"][0]
        self.assertEqual(hit["timestamp"],"2026-04-12T22:00:00+00:00")
        self.assertIn("filename",hit["timestamp_basis"])

    def test_markdown_tool_code_not_dialogue(self):
        p=self.root/"log.md"
        p.write_text("# Log\n\n**User**\n正常對話\n\n**Tool Result**\n```\n**User**\n工具偽裝\n```\n\n**Claude**\n正常回覆\n")
        imp.import_markdown(self.c,p,"forum"); self.c.commit()
        self.assertFalse(store.search("工具偽裝",path=self.db)["results"])
        self.assertTrue(store.search("工具偽裝",path=self.db,include_tools=True)["results"])
        self.assertTrue(store.search("正常回覆",path=self.db)["results"])

    def test_chatgpt_branches_and_reimport(self):
        p=self.root/"conversations-000.json"
        def node(n,parent,text): return {"parent":parent,"message":{"author":{"role":"user" if n=="a" else "assistant"},"content":{"content_type":"text","parts":[text]},"create_time":1700000000}}
        data=[{"id":"chat-1","title":"Branch test","current_node":"c","mapping":{"a":node("a",None,"原始問題"),"b":node("b","a","舊回答"),"c":node("c","a","新回答")}}]
        p.write_text(json.dumps(data)); imp.import_chatgpt(self.c,p); self.c.commit()
        mid=store.search("原始問題",path=self.db)["results"][0]["message_id"]
        ctx=store.read_context(mid,path=self.db)
        self.assertEqual(len(ctx["messages"]),1); self.assertEqual(len(ctx["target_children"]),2)
        child=store.search("舊回答",path=self.db)["results"][0]
        self.assertEqual(child["active"],0)
        self.assertEqual(len(store.read_context(child["message_id"],path=self.db)["messages"]),2)
        imp.import_chatgpt(self.c,p); self.c.commit()
        self.assertEqual(self.c.execute("SELECT count(*) FROM messages").fetchone()[0],3)

    def test_raw_tool_separation_and_duplicate_origins(self):
        rows=[{"type":"session_meta","payload":{"id":"codex-1","cwd":"/project"}},
            {"type":"event_msg","payload":{"type":"user_message","message":"不要重複"}},
            {"type":"response_item","timestamp":"2026-01-01T00:00:00Z","payload":{"type":"message","role":"user","content":[{"text":"不要重複"}]}},
            {"type":"response_item","payload":{"type":"function_call_output","output":"工具內容"}}]
        for name in ("one.jsonl","two.jsonl"):
            p=self.root/name; p.write_text("\n".join(json.dumps(r) for r in rows)+"\n"); imp.import_jsonl(self.c,p,"codex")
        self.c.commit()
        hits=store.search("不要重複",path=self.db)["results"]
        self.assertEqual(len(hits),1); self.assertEqual(len(hits[0]["origins"]),2)
        self.assertFalse(store.search("工具內容",path=self.db)["results"])
        self.assertTrue(store.search("工具內容",include_tools=True,path=self.db)["results"])

    def test_incremental_change_and_rollback(self):
        p=self.root/"sample.jsonl"
        row={"type":"user","uuid":"u","sessionId":"s","message":{"role":"user","content":"原始紀錄"}}
        p.write_text(json.dumps(row)+"\n")
        config=self.root/"sources.json"; config.write_text(json.dumps({"sources":[{"provider":"claude","path":str(self.root),"glob":"*.jsonl"}]}))
        self.assertEqual(imp.run(config,self.db)["updated"],1)
        self.assertEqual(imp.run(config,self.db)["unchanged"],1)
        row["message"]["content"]="修改內容"; p.write_text(json.dumps(row)+"\n")
        imp.run(config,self.db)
        self.assertFalse(store.search("原始紀錄",path=self.db)["results"])
        self.assertTrue(store.search("修改內容",path=self.db)["results"])
        p.write_text("bad json\n")
        self.assertEqual(imp.run(config,self.db)["failed"],1)
        self.assertTrue(store.search("修改內容",path=self.db)["results"])


if __name__=="__main__": unittest.main()
