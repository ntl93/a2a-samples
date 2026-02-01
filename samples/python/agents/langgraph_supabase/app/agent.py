import os
import asyncio
from collections.abc import AsyncIterable
from typing import Any, Literal

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.tools.structured import StructuredTool
from langchain_openai import AzureChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel
from langchain_mcp_adapters.client import MultiServerMCPClient


memory = MemorySaver()


class ResponseFormat(BaseModel):
    """Respond to the user in this format."""

    status: Literal['input_required', 'completed', 'error'] = 'input_required'
    message: str


class SupabaseAgent:
    """SupabaseAgent - a specialized assistant for Supabase database queries via MCP."""

    SYSTEM_INSTRUCTION = (
        'You are a specialized assistant for querying Supabase databases. '
        'You have access to MCP tools that can query Supabase databases via an HTTP MCP server. '
        'Use the available MCP tools to answer questions about database data. '
        'If the user asks about anything other than database queries or Supabase data, '
        'politely state that you cannot help with that topic and can only assist with database-related queries. '
        'Do not attempt to answer unrelated questions or use tools for other purposes.'
    )

    FORMAT_INSTRUCTION = (
        'Set response status to input_required if the user needs to provide more information to complete the request. '
        'Set response status to error if there is an error while processing the request. '
        'Set response status to completed if the request is complete.'
    )

    def __init__(self):

        print("Initializing SupabaseAgent...")
        print(f"Using MCP server URL: {os.getenv('SUPABASE_MCP_SERVER_URL', 'http://localhost:3000')}")
        print(f"Using SUPABASE_API_KEY: {'set' if os.getenv('SUPABASE_API_KEY') else 'not set'}")
        print(f"Using AZURE_OPENAI_API_KEY: {'set' if os.getenv('AZURE_OPENAI_API_KEY') else 'not set'}")
        self.model = AzureChatOpenAI(
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            api_version=os.getenv('AZURE_OPENAI_API_VERSION'),
            azure_deployment=os.getenv('AZURE_OPENAI_DEPLOYMENT_NAME'),
            azure_endpoint=os.getenv('AZURE_OPENAI_ENDPOINT'),
            temperature=1,
        )
        self.mcp_server_url = os.getenv("SUPABASE_MCP_SERVER_URL", "http://localhost:3000")
        self.mcp_client = None
        self.tools = []
        self.graph = None

    @staticmethod
    def _wrap_tool_with_schema_defaults(tool: BaseTool) -> BaseTool:
        """Wrap an MCP-backed tool to apply JSON-schema defaults.

        Supabase MCP tool schemas sometimes mark fields as required even when a
        default is provided (e.g. `schemas: ["public"]`). LLMs may omit those
        fields, causing tool calls to fail server-side.

        This wrapper injects `properties.<arg>.default` when an arg is missing.
        """

        if not isinstance(tool, StructuredTool):
            return tool

        schema = getattr(tool, "args_schema", None)
        if not isinstance(schema, dict):
            return tool

        properties: dict[str, Any] = schema.get("properties") or {}
        if not isinstance(properties, dict) or not properties:
            return tool

        defaults: dict[str, Any] = {}
        for key, prop in properties.items():
            if isinstance(prop, dict) and "default" in prop and key is not None:
                defaults[str(key)] = prop["default"]

        if not defaults:
            return tool

        original_coroutine = tool.coroutine

        async def call_tool_with_defaults(**arguments: dict[str, Any]):
            for key, value in defaults.items():
                if key not in arguments or arguments[key] is None:
                    arguments[key] = value
            return await original_coroutine(**arguments)

        return StructuredTool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            coroutine=call_tool_with_defaults,
            response_format=getattr(tool, "response_format", "content"),
            metadata=getattr(tool, "metadata", None),
        )

    async def initialize(self):
        """Initialize the MCP client and load tools."""
        # Create MultiServerMCPClient for HTTP MCP server connection
        self.mcp_client = MultiServerMCPClient(
            {
                "supabase": {
                    "url": self.mcp_server_url,
                    "transport": "streamable_http",
                    "headers": {  
                        "Authorization": f"Bearer {os.getenv('SUPABASE_API_KEY', '')}"
                    },  
                    # Supabase MCP does not support session termination via DELETE.
                    # Avoid noisy warnings on close.
                    "terminate_on_close": False,
                }
            }
        )
        
        # Get tools from MCP server
        self.tools = await self.mcp_client.get_tools()
        self.tools = [self._wrap_tool_with_schema_defaults(t) for t in self.tools]
        
        # Create the ReAct agent with MCP tools
        self.graph = create_react_agent(
            self.model,
            tools=self.tools,
            checkpointer=memory,
            prompt=self.SYSTEM_INSTRUCTION,
            response_format=(self.FORMAT_INSTRUCTION, ResponseFormat),
        )

    async def cleanup(self):
        """Cleanup MCP client resources."""
        # MultiServerMCPClient creates short-lived sessions per call; it does not
        # expose a close() method.
        self.mcp_client = None

    async def stream(self, query, context_id) -> AsyncIterable[dict[str, Any]]:
        """Stream agent responses."""
        if not self.graph:
            raise RuntimeError("Agent not initialized. Call initialize() first.")
        
        inputs = {'messages': [('user', query)]}
        config = {'configurable': {'thread_id': context_id}}

        async for item in self.graph.astream(inputs, config, stream_mode='values'):
            message = item['messages'][-1]
            if (
                isinstance(message, AIMessage)
                and message.tool_calls
                and len(message.tool_calls) > 0
            ):
                yield {
                    'is_task_complete': False,
                    'require_user_input': False,
                    'content': 'Querying Supabase database via MCP...',
                }
            elif isinstance(message, ToolMessage):
                yield {
                    'is_task_complete': False,
                    'require_user_input': False,
                    'content': 'Processing database results...',
                }

        yield self.get_agent_response(config)

    def get_agent_response(self, config):
        current_state = self.graph.get_state(config)
        structured_response = current_state.values.get('structured_response')
        if structured_response and isinstance(
            structured_response, ResponseFormat
        ):
            if structured_response.status == 'input_required':
                return {
                    'is_task_complete': False,
                    'require_user_input': True,
                    'content': structured_response.message,
                }
            if structured_response.status == 'error':
                return {
                    'is_task_complete': False,
                    'require_user_input': True,
                    'content': structured_response.message,
                }
            if structured_response.status == 'completed':
                return {
                    'is_task_complete': True,
                    'require_user_input': False,
                    'content': structured_response.message,
                }

        return {
            'is_task_complete': False,
            'require_user_input': True,
            'content': (
                'We are unable to process your request at the moment. '
                'Please try again.'
            ),
        }

    SUPPORTED_CONTENT_TYPES = ['text', 'text/plain']
