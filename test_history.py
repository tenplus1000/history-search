import json
import tempfile
import unittest
from unittest import mock
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

    def sources(self,*specs):
        path=self.root/"sources.json"; path.write_text(json.dumps({"sources":list(specs)})); return path

    def test_appended_lines_are_read_without_rereading_the_file(self):
        folder=self.root/"proj"; folder.mkdir(); p=folder/"session-a.jsonl"
        row=lambda u,t: json.dumps({"type":"user","uuid":u,"sessionId":"s9","cwd":"/w","timestamp":"2026-11-07T01:00:00Z","message":{"role":"user","content":t}},ensure_ascii=False)+"\n"
        p.write_text(row("a","第一句蔡老師"))
        conf=self.sources({"provider":"claude","path":str(folder),"glob":"*.jsonl"}); db=self.root/"tail.sqlite3"
        self.assertEqual(imp.run(conf,db)["updated"],1)
        real=imp.json.loads; seen=[]
        with p.open("a") as f: f.write(row("b","第二句相褒歌")); f.write('{"type":"user","uuid":"half')
        with mock.patch.object(imp.json,"loads",side_effect=lambda t:(seen.append(t),real(t))[1]):
            imp.run(conf,db,only=imp.TAILABLE)
        self.assertFalse(any("第一句" in str(t) for t in seen))       # did not reread
        hits=store.search("第二句",path=db)["results"]
        self.assertEqual(len(hits),1)
        self.assertEqual(hits[0]["title"],"第一句蔡老師")
        with p.open("a") as f: f.write('","sessionId":"s9","message":{"role":"user","content":"第三句寫完了"}}\n')
        imp.run(conf,db,only=imp.TAILABLE)
        self.assertEqual(len(store.search("第三句",path=db)["results"]),1)
        self.assertEqual(len(store.search("第一句",path=db)["results"]),1)     # nothing lost or doubled

    def test_codex_session_id_survives_tail_import(self):
        folder=self.root/"cx"; folder.mkdir(); p=folder/"rollout-1.jsonl"
        meta=json.dumps({"type":"session_meta","payload":{"id":"real-id","cwd":"/w"}})+"\n"
        msg=lambda t: json.dumps({"type":"response_item","timestamp":"2026-11-07T01:00:00Z","payload":{"type":"message","id":t,"role":"user","content":[{"type":"input_text","text":t}]}})+"\n"
        p.write_text(meta+msg("早先那句"))
        conf=self.sources({"provider":"codex","path":str(folder),"glob":"rollout-*.jsonl*"}); db=self.root/"cx.sqlite3"
        imp.run(conf,db)
        with p.open("a") as f: f.write(msg("後來那句"))
        imp.run(conf,db,only=imp.TAILABLE)
        with store.connect(db) as c:
            self.assertEqual([r[0] for r in c.execute("SELECT native_id FROM conversations")],["real-id"])

    def test_shrunk_file_is_imported_again_from_the_start(self):
        folder=self.root/"shr"; folder.mkdir(); p=folder/"x.jsonl"
        row=lambda u,t: json.dumps({"uuid":u,"sessionId":"s1","timestamp":"2026-11-07T01:00:00Z","message":{"role":"user","content":t}},ensure_ascii=False)+"\n"
        p.write_text(row("a","很長很長的舊內容")+row("b","另一句舊內容"))
        conf=self.sources({"provider":"claude","path":str(folder),"glob":"*.jsonl"}); db=self.root/"shr.sqlite3"
        imp.run(conf,db)
        p.write_text(row("c","新寫"))
        imp.run(conf,db,only=imp.TAILABLE)
        self.assertEqual(len(store.search("新寫",path=db)["results"]),1)
        self.assertEqual(store.search("舊內容",path=db)["results"],[])

    def test_room_log_finds_what_the_user_said_on_the_room_page(self):
        folder=self.root/"rooms"; folder.mkdir(); p=folder/"R37.jsonl"
        post=lambda seq,who,name,t: json.dumps({"seq":seq,"at":"2026-11-07T01:00:00+00:00","room_id":"R37","type":"post","from":who,"from_name":name,"text":t},ensure_ascii=False)+"\n"
        p.write_text(post(1,"owner","本人","音樂頁改綠色")+post(2,"s1","結案","本人交代音樂頁")+json.dumps({"seq":3,"room_id":"R37","type":"joined"})+"\n")
        conf=self.sources({"provider":"rooms","path":str(folder),"glob":"*.jsonl","users":["owner"]}); db=self.root/"room.sqlite3"
        imp.run(conf,db)
        got=store.search("音樂頁",role="user",path=db)["results"]
        self.assertEqual(len(got),1)
        self.assertIn("音樂頁改綠色",got[0]["excerpt"] if "excerpt" in got[0] else json.dumps(got[0],ensure_ascii=False))
        with p.open("a") as f: f.write(post(4,"owner","本人","再改標題"))
        imp.run(conf,db,only=imp.TAILABLE)
        self.assertEqual(len(store.search("再改標題",provider="rooms",path=db)["results"]),1)

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
