"""Verify the actual stdio transport using the official MCP client."""
import asyncio
import json
import sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name("server.py"))])
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            tools=await session.list_tools()
            names=[t.name for t in tools.tools]
            assert {"search_history","read_context","read_message","get_source","archive_status","refresh_history"} <= set(names)
            # A word that exists in your own archive: python smoke_mcp.py --query <word>
            query=sys.argv[sys.argv.index("--query")+1] if "--query" in sys.argv else "the"
            result=await session.call_tool("search_history",{"query":query,"limit":1})
            assert not result.isError,result
            data=json.loads(result.content[0].text)
            assert data["results"]
            mid=data["results"][0]["message_id"]
            for name in ("read_context","read_message","get_source"):
                result=await session.call_tool(name,{"message_id":mid})
                assert not result.isError,(name,result)
            result=await session.call_tool("archive_status",{})
            assert not result.isError,result
            bad=await session.call_tool("get_source",{"message_id":"nonexistent"})
            assert bad.isError
            if "--refresh" in sys.argv:
                result=await session.call_tool("refresh_history",{})
                assert not result.isError,result
                report=json.loads(result.content[0].text)
                assert report["success"],report
                print(json.dumps({"refresh":report}))
            print(json.dumps({"transport":"stdio","tools":names,"real_search_and_context":"passed","missing_id_error":"passed"}))


if __name__=="__main__": asyncio.run(main())
