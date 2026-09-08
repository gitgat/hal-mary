"""The MCP surface: hal-mary's decisions, Claude Cowork's hands.

``server`` builds the tool set and the guarded ASGI endpoint the web app mounts
at ``/mcp``. Nothing else in the package imports it, so a deployment with no
``MCP_TOKEN`` never constructs it.
"""

from .server import MCP_PATH, McpEndpoint, build_endpoint

__all__ = ["MCP_PATH", "McpEndpoint", "build_endpoint"]
