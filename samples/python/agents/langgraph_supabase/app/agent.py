import os
import asyncio
from collections.abc import AsyncIterable
from typing import Any, Literal

from langchain_core.messages import AIMessage, ToolMessage
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

    async def initialize(self):
        """Initialize the MCP client and load tools."""
        # Create MultiServerMCPClient for HTTP MCP server connection
        self.mcp_client = MultiServerMCPClient(
            {
                "supabase": {
                    "url": self.mcp_server_url,
                    "transport": "streamable_http",
                }
            }
        )
        
        # Get tools from MCP server
        self.tools = await self.mcp_client.get_tools()
        
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
        if self.mcp_client:
            await self.mcp_client.close()

    async def stream(self, query, context_id) -> AsyncIterable[dict[str, Any]]:
        """Stream agent responses."""
        if not self.graph:
            raise RuntimeError("Agent not initialized. Call initialize() first.")
        
        inputs = {'messages': [('user', query)]}
        config = {'configurable': {'thread_id': context_id}}

        for item in self.graph.stream(inputs, config, stream_mode='values'):
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
