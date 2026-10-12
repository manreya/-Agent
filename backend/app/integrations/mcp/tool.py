"""MCP 工具封装

从 hello_agents 的 tools/builtin/protocol_tools.py 与 mcp_wrapper_tool.py 合并提取,
只保留 MCPTool 与 MCPWrappedTool 两个类。

相对原版删掉的内容:
- A2ATool / ANPTool（另两种协议, 本项目不用）
- _create_builtin_server()（内置的四则运算演示服务器, 属于教程示例;
  本项目永远显式传 server_command, 该分支不可达, 改为直接报错）
- 原 `from hello_agents.protocols.mcp.client import MCPClient` 触发的
  protocols 包 __init__ 链（会连带导入 a2a / anp / server）,
  现在改为同包内相对导入, 不再有这层牵连。

待办（属于后续重构, 不在本次提取范围）:
    MCPTool.run() 是同步方法, 且内部自己造事件循环 + 线程池来跑异步客户端
    （见 _run_async）。在 async 节点里调用它会阻塞事件循环, 导致新架构
    "并行 gather 三个高德查询" 退化成串行。后续应直接暴露 async 接口。
"""

from typing import Dict, Any, List, Optional
from .base import Tool, ToolParameter
from .client import MCPClient
import os


# MCP服务器环境变量映射表
# 用于自动检测常见MCP服务器需要的环境变量
MCP_SERVER_ENV_MAP = {
    "server-github": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    "server-slack": ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
    "server-google-drive": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"],
    "server-postgres": ["POSTGRES_CONNECTION_STRING"],
    "server-sqlite": [],  # 不需要环境变量
    "server-filesystem": [],  # 不需要环境变量
}


def _run_async(coro):
    """在当前线程跑一个协程

    若已有运行中的事件循环（例如被 FastAPI/LangGraph 的 async 节点调用）,
    则另起线程 + 新事件循环执行, 以免 "asyncio.run() cannot be called from a
    running event loop"。

    注意: 这条路径会让调用方所在的事件循环阻塞等待, 是已知的并发缺陷,
    详见模块 docstring 的"待办"。
    """
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()

        def run_in_thread():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()

        with concurrent.futures.ThreadPoolExecutor() as executor:
            return executor.submit(run_in_thread).result()
    except RuntimeError:
        # 没有运行中的事件循环
        return asyncio.run(coro)


class MCPTool(Tool):
    """MCP (Model Context Protocol) 工具

    连接到 MCP 服务器并调用其提供的工具、资源和提示词。

    功能：
    - 列出服务器提供的工具
    - 调用服务器工具
    - 读取服务器资源
    - 获取提示词模板

    使用示例:
        >>> tool = MCPTool(
        ...     name="amap",
        ...     server_command=["uvx", "amap-mcp-server"],
        ...     env={"AMAP_MAPS_API_KEY": "..."},
        ...     auto_expand=True,
        ... )
        >>> tool.run({"action": "call_tool", "tool_name": "maps_weather",
        ...           "arguments": {"city": "上海"}})
    """

    def __init__(self,
                 name: str = "mcp",
                 description: Optional[str] = None,
                 server_command: Optional[List[str]] = None,
                 server_args: Optional[List[str]] = None,
                 server: Optional[Any] = None,
                 auto_expand: bool = True,
                 env: Optional[Dict[str, str]] = None,
                 env_keys: Optional[List[str]] = None):
        """
        初始化 MCP 工具

        Args:
            name: 工具名称（默认为"mcp"，建议为不同服务器指定不同名称）
            description: 工具描述（可选，默认为通用描述）
            server_command: 服务器启动命令（如 ["uvx", "amap-mcp-server"]）
            server_args: 服务器参数列表
            server: FastMCP 服务器实例（可选，用于内存传输）
            auto_expand: 是否自动展开为独立工具（默认True）
            env: 环境变量字典（优先级最高，直接传递给MCP服务器）
            env_keys: 要从系统环境变量加载的key列表（优先级中等）

        环境变量优先级（从高到低）：
            1. 直接传递的env参数
            2. env_keys指定的环境变量
            3. 自动检测的环境变量（根据server_command）

        注意: 必须提供 server_command 或 server 之一。
        """
        if not server_command and not server:
            raise ValueError(
                "MCPTool 需要 server_command 或 server 参数。"
                "例如: MCPTool(name='amap', server_command=['uvx', 'amap-mcp-server'])"
            )

        self.server_command = server_command
        self.server_args = server_args or []
        self.server = server
        self._client = None
        self._available_tools = []
        self.auto_expand = auto_expand
        self.prefix = f"{name}_" if auto_expand else ""

        # 环境变量处理（优先级：env > env_keys > 自动检测）
        self.env = self._prepare_env(env, env_keys, server_command)

        # 自动发现工具
        self._discover_tools()

        # 设置默认描述或自动生成
        if description is None:
            description = self._generate_description()

        super().__init__(
            name=name,
            description=description
        )

    def _prepare_env(self,
                     env: Optional[Dict[str, str]],
                     env_keys: Optional[List[str]],
                     server_command: Optional[List[str]]) -> Dict[str, str]:
        """
        准备环境变量

        优先级：env > env_keys > 自动检测

        Args:
            env: 直接传递的环境变量字典
            env_keys: 要从系统环境变量加载的key列表
            server_command: 服务器命令（用于自动检测）

        Returns:
            合并后的环境变量字典
        """
        result_env = {}

        # 1. 自动检测（优先级最低）
        if server_command:
            # 从命令中提取服务器名称
            server_name = None
            for part in server_command:
                if "server-" in part:
                    # 提取类似 "@modelcontextprotocol/server-github" 中的 "server-github"
                    server_name = part.split("/")[-1] if "/" in part else part
                    break

            # 查找映射表
            if server_name and server_name in MCP_SERVER_ENV_MAP:
                auto_keys = MCP_SERVER_ENV_MAP[server_name]
                for key in auto_keys:
                    value = os.getenv(key)
                    if value:
                        result_env[key] = value
                        print(f"🔑 自动加载环境变量: {key}")

        # 2. env_keys指定的环境变量（优先级中等）
        if env_keys:
            for key in env_keys:
                value = os.getenv(key)
                if value:
                    result_env[key] = value
                    print(f"🔑 从env_keys加载环境变量: {key}")
                else:
                    print(f"⚠️  警告: 环境变量 {key} 未设置")

        # 3. 直接传递的env（优先级最高）
        if env:
            result_env.update(env)
            for key in env.keys():
                print(f"🔑 使用直接传递的环境变量: {key}")

        return result_env

    def _discover_tools(self):
        """发现MCP服务器提供的所有工具"""
        try:
            async def discover():
                client_source = self.server if self.server else self.server_command
                async with MCPClient(client_source, self.server_args, env=self.env) as client:
                    return await client.list_tools()

            self._available_tools = _run_async(discover())
        except Exception:
            # 工具发现失败不影响初始化
            self._available_tools = []

    def _generate_description(self) -> str:
        """生成增强的工具描述"""
        if not self._available_tools:
            return "连接到 MCP 服务器，调用工具、读取资源和获取提示词。"

        if self.auto_expand:
            # 展开模式：简单描述
            return f"MCP工具服务器，包含{len(self._available_tools)}个工具。这些工具会自动展开为独立的工具供Agent使用。"

        # 非展开模式：详细描述
        desc_parts = [
            f"MCP工具服务器，提供{len(self._available_tools)}个工具："
        ]

        # 列出所有工具
        for tool in self._available_tools:
            tool_name = tool.get('name', 'unknown')
            tool_desc = tool.get('description', '无描述')
            # 简化描述，只取第一句
            short_desc = tool_desc.split('.')[0] if tool_desc else '无描述'
            desc_parts.append(f"  • {tool_name}: {short_desc}")

        # 添加调用格式说明
        desc_parts.append("\n调用格式：返回JSON格式的参数")
        desc_parts.append('{"action": "call_tool", "tool_name": "工具名", "arguments": {...}}')

        # 添加示例
        if self._available_tools:
            first_tool = self._available_tools[0]
            tool_name = first_tool.get('name', 'example')
            desc_parts.append(f'\n示例：{{"action": "call_tool", "tool_name": "{tool_name}", "arguments": {{...}}}}')

        return "\n".join(desc_parts)

    def get_expanded_tools(self) -> List['Tool']:  # type: ignore
        """
        获取展开的工具列表

        将MCP服务器的每个工具包装成独立的Tool对象

        Returns:
            Tool对象列表
        """
        if not self.auto_expand:
            return []

        expanded_tools = []
        for tool_info in self._available_tools:
            wrapped_tool = MCPWrappedTool(
                mcp_tool=self,
                tool_info=tool_info,
                prefix=self.prefix
            )
            expanded_tools.append(wrapped_tool)

        return expanded_tools

    def run(self, parameters: Dict[str, Any]) -> str:
        """
        执行 MCP 操作

        Args:
            parameters: 包含以下参数的字典
                - action: 操作类型 (list_tools, call_tool, list_resources, read_resource, list_prompts, get_prompt)
                  如果不指定action但指定了tool_name，会自动推断为call_tool
                - tool_name: 工具名称（call_tool 需要）
                - arguments: 工具参数（call_tool 需要）
                - uri: 资源 URI（read_resource 需要）
                - prompt_name: 提示词名称（get_prompt 需要）
                - prompt_arguments: 提示词参数（get_prompt 可选）

        Returns:
            操作结果
        """
        # 智能推断action：如果没有action但有tool_name，自动设置为call_tool
        action = parameters.get("action", "").lower()
        if not action and "tool_name" in parameters:
            action = "call_tool"
            parameters["action"] = action

        if not action:
            return "错误：必须指定 action 参数或 tool_name 参数"

        async def run_mcp_operation():
            # 根据配置选择客户端创建方式
            client_source = self.server if self.server else self.server_command

            async with MCPClient(client_source, self.server_args, env=self.env) as client:
                if action == "list_tools":
                    tools = await client.list_tools()
                    if not tools:
                        return "没有找到可用的工具"
                    result = f"找到 {len(tools)} 个工具:\n"
                    for tool in tools:
                        result += f"- {tool['name']}: {tool['description']}\n"
                    return result

                elif action == "call_tool":
                    tool_name = parameters.get("tool_name")
                    arguments = parameters.get("arguments", {})
                    if not tool_name:
                        return "错误：必须指定 tool_name 参数"
                    result = await client.call_tool(tool_name, arguments)
                    return f"工具 '{tool_name}' 执行结果:\n{result}"

                elif action == "list_resources":
                    resources = await client.list_resources()
                    if not resources:
                        return "没有找到可用的资源"
                    result = f"找到 {len(resources)} 个资源:\n"
                    for resource in resources:
                        result += f"- {resource['uri']}: {resource['name']}\n"
                    return result

                elif action == "read_resource":
                    uri = parameters.get("uri")
                    if not uri:
                        return "错误：必须指定 uri 参数"
                    content = await client.read_resource(uri)
                    return f"资源 '{uri}' 内容:\n{content}"

                elif action == "list_prompts":
                    prompts = await client.list_prompts()
                    if not prompts:
                        return "没有找到可用的提示词"
                    result = f"找到 {len(prompts)} 个提示词:\n"
                    for prompt in prompts:
                        result += f"- {prompt['name']}: {prompt['description']}\n"
                    return result

                elif action == "get_prompt":
                    prompt_name = parameters.get("prompt_name")
                    prompt_arguments = parameters.get("prompt_arguments", {})
                    if not prompt_name:
                        return "错误：必须指定 prompt_name 参数"
                    messages = await client.get_prompt(prompt_name, prompt_arguments)
                    result = f"提示词 '{prompt_name}':\n"
                    for msg in messages:
                        result += f"[{msg['role']}] {msg['content']}\n"
                    return result

                else:
                    return f"错误：不支持的操作 '{action}'"

        try:
            return _run_async(run_mcp_operation())
        except Exception as e:
            return f"MCP 操作失败: {str(e)}"

    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义"""
        return [
            ToolParameter(
                name="action",
                type="string",
                description="操作类型: list_tools, call_tool, list_resources, read_resource, list_prompts, get_prompt",
                required=True
            ),
            ToolParameter(
                name="tool_name",
                type="string",
                description="工具名称（call_tool 操作需要）",
                required=False
            ),
            ToolParameter(
                name="arguments",
                type="object",
                description="工具参数（call_tool 操作需要）",
                required=False
            ),
            ToolParameter(
                name="uri",
                type="string",
                description="资源 URI（read_resource 操作需要）",
                required=False
            ),
            ToolParameter(
                name="prompt_name",
                type="string",
                description="提示词名称（get_prompt 操作需要）",
                required=False
            ),
            ToolParameter(
                name="prompt_arguments",
                type="object",
                description="提示词参数（get_prompt 操作可选）",
                required=False
            )
        ]


class MCPWrappedTool(Tool):
    """MCP工具包装器 - 将单个MCP工具包装成独立的 Tool

    这个类将MCP服务器的一个工具（如 maps_weather）包装成一个独立的Tool对象。
    Agent调用时只需提供参数，无需了解MCP的内部结构。

    示例：
        >>> # 内部使用，由MCPTool.get_expanded_tools() 自动创建
        >>> wrapped_tool = MCPWrappedTool(
        ...     mcp_tool=mcp_tool_instance,
        ...     tool_info={
        ...         "name": "maps_weather",
        ...         "description": "查询天气...",
        ...         "input_schema": {...}
        ...     }
        ... )
    """

    def __init__(self,
                 mcp_tool: MCPTool,
                 tool_info: Dict[str, Any],
                 prefix: str = ""):
        """
        初始化MCP包装工具

        Args:
            mcp_tool: 父MCP工具实例
            tool_info: MCP工具信息（包含name, description, input_schema）
            prefix: 工具名前缀（如 "amap_"）
        """
        self.mcp_tool = mcp_tool
        self.tool_info = tool_info
        self.mcp_tool_name = tool_info.get('name', 'unknown')

        # 构建工具名：prefix + mcp_tool_name
        tool_name = f"{prefix}{self.mcp_tool_name}" if prefix else self.mcp_tool_name

        # 获取描述
        description = tool_info.get('description', f'MCP工具: {self.mcp_tool_name}')

        # 解析参数schema
        self._parameters = self._parse_input_schema(tool_info.get('input_schema', {}))

        # 初始化父类
        super().__init__(
            name=tool_name,
            description=description
        )

    def _parse_input_schema(self, input_schema: Dict[str, Any]) -> List[ToolParameter]:
        """
        将MCP的input_schema转换为 ToolParameter 列表

        Args:
            input_schema: MCP工具的input_schema（JSON Schema格式）

        Returns:
            ToolParameter列表
        """
        parameters = []

        properties = input_schema.get('properties', {})
        required_fields = input_schema.get('required', [])

        for param_name, param_info in properties.items():
            param_type = param_info.get('type', 'string')
            param_desc = param_info.get('description', '')
            is_required = param_name in required_fields

            parameters.append(ToolParameter(
                name=param_name,
                type=param_type,  # 直接使用JSON Schema的类型字符串
                description=param_desc,
                required=is_required
            ))

        return parameters

    def get_parameters(self) -> List[ToolParameter]:
        """
        获取工具参数定义

        Returns:
            ToolParameter列表
        """
        return self._parameters

    def run(self, params: Dict[str, Any]) -> str:
        """
        执行MCP工具

        Args:
            params: 工具参数（直接传递给MCP工具）

        Returns:
            执行结果
        """
        # 构建MCP调用参数
        mcp_params = {
            "action": "call_tool",
            "tool_name": self.mcp_tool_name,
            "arguments": params
        }

        # 调用父MCP工具
        return self.mcp_tool.run(mcp_params)