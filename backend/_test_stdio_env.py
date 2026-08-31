"""验证 stdio 子进程能否读到 PROMETHEUS_URL（模拟 Agent 的 MCP 加载流程）"""
import asyncio
import os


async def main():
    from langchain_mcp_adapters.client import MultiServerMCPClient

    print("=== 父进程环境 ===")
    print("PROMETHEUS_URL:", os.environ.get("PROMETHEUS_URL", "NOT SET"))

    # 模拟 agent.py 的 _build_default_mcp_config（含 env 传递）
    config = {
        "prometheus": {
            "command": "python",
            "args": ["/app/mcp_servers/prometheus_monitoring_server.py"],
            "transport": "stdio",
            "env": os.environ.copy(),  # 显式传递环境变量
        }
    }

    print("\n=== 启动 stdio 子进程 ===")
    client = MultiServerMCPClient(config)
    tools = await client.get_tools()
    print(f"工具数: {len(tools)}")
    for t in tools:
        print(f"  - {t.name}")

    # 调 query_system_overview 测试 Prometheus 连通性
    print("\n=== 调用 query_system_overview ===")
    for t in tools:
        if t.name == "query_system_overview":
            result = await t.ainvoke({})
            import json
            data = json.loads(result)
            print(f"prometheus_url: {data.get('prometheus_url')}")
            metrics = data.get("metrics", {})
            for name in ["cpu_usage_percent", "memory_usage_percent", "cpu_load1"]:
                m = metrics.get(name, {})
                if "error" in m:
                    print(f"  {name}: ERROR - {m['error']}")
                else:
                    print(f"  {name}: {m.get('value')}")
            break

    await client.close()
    print("\n✅ stdio 子进程测试完成")

if __name__ == "__main__":
    asyncio.run(main())
