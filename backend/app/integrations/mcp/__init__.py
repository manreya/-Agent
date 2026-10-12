"""MCP 集成

从 hello_agents 提取的精简版本, 只保留本项目实际使用的链路:

    MCPTool (同步门面)
      └─ MCPClient      stdio 传输, 连接 uvx amap-mcp-server
      └─ MCPWrappedTool  auto_expand=True 时把每个 MCP 工具包装成独立 Tool

已剥离的内容: A2ATool / ANPTool / MCPServer / 内置演示服务器, 以及原
hello_agents 包 __init__ 里那条会无条件导入 a2a、anp、rag、neo4j、qdrant、
RL 与评估模块的 eager-import 链 —— 那才是 requirements 里一堆无关依赖的来源。

注意: 本包 __init__ 刻意保持"只导入下面三个符号"的克制, 不要再往这里加
兄弟模块的顶层导入, 否则又会把不必要的依赖重新拖进来。
"""

from .base import Tool, ToolParameter
from .client import MCPClient
from .tool import MCPTool, MCPWrappedTool

__all__ = [
    "Tool",
    "ToolParameter",
    "MCPClient",
    "MCPTool",
    "MCPWrappedTool",
]