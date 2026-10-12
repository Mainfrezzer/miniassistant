"""MCP-Client — MiniAssistant verbindet sich zu MCP-Servern (stdio lokal, Streamable HTTP remote).

Konfiguration: mcp_servers.<name> = {command, args, url, enabled, expose_to_agent, timeout}
- stdio (lokal): command + args
- remote (Streamable HTTP): url
Server werden bei Bedarf verbunden (lazy connect), Tools über tools/list, Aufrufe über tools/call.
"""
from __future__ import annotations

import logging
import os
from typing import Any

_log = logging.getLogger("miniassistant.mcp_client")

# In-Memory-Registry: name -> {"client": MCPClient, "tools": [Tool]}
_clients: dict[str, Any] = {}

# Bibliothek optional (install.sh: pip install -e '.[mcp]')
try:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.types import Tool
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


def is_available() -> bool:
    """mcp-Bibliothek installiert?"""
    return MCP_AVAILABLE


def list_configured_servers(config: dict[str, Any]) -> list[str]:
    return list((config.get("mcp_servers") or {}).keys())


def list_enabled_servers(config: dict[str, Any]) -> list[str]:
    out = []
    for name, cfg in (config.get("mcp_servers") or {}).items():
        if cfg.get("enabled", True):
            out.append(name)
    return out


def _server_config(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    return (config.get("mcp_servers") or {}).get(name)


async def _connect(name: str, cfg: dict[str, Any]) -> Any:
    """MCP-Server verbinden (stdio oder HTTP)."""
    url = (cfg.get("url") or "").strip()
    if url:
        # Remote: Streamable HTTP
        from mcp.client.streamable_http import streamable_http_client
        transport = streamable_http_client(url)
    else:
        # Lokal: stdio
        command = cfg.get("command")
        if not command:
            raise ValueError(f"MCP-Server '{name}' hat weder 'command' noch 'url'")
        params = StdioServerParameters(
            command=command,
            args=cfg.get("args") or [],
            env=os.environ,
        )
        transport = stdio_client(params)

    session = await transport.__aenter__()
    result = await session.initialize()
    return {"transport": transport, "session": session}


async def _disconnect(entry: Any) -> None:
    try:
        await entry["session"].close()
    except Exception:
        pass
    try:
        await entry["transport"].__aexit__(None, None, None)
    except Exception:
        pass


async def _ensure_connected(config: dict[str, Any], name: str) -> Any:
    """Verbinden, falls nicht bereits verbunden."""
    cfg = _server_config(config, name)
    if not cfg:
        raise ValueError(f"MCP-Server '{name}' nicht konfiguriert")
    if not cfg.get("enabled", True):
        raise ValueError(f"MCP-Server '{name}' nicht aktiviert")

    entry = _clients.get(name)
    if entry:
        return entry

    entry = await _connect(name, cfg)
    _clients[name] = entry
    _log.info("MCP-Server '%s' verbunden", name)
    return entry


async def get_tools(config: dict[str, Any], name: str) -> list[Tool]:
    """Tool-Discovery: tools/list."""
    entry = await _ensure_connected(config, name)
    result = await entry["session"].list_tools()
    return result.tools


async def call_tool(config: dict[str, Any], server_name: str, tool_name: str, args: dict[str, Any]) -> Any:
    """Tool aufrufen."""
    entry = await _ensure_connected(config, server_name)
    result = await entry["session"].call_tool(tool_name, args)
    return result


def call_tool_sync(config: dict[str, Any], server_name: str, tool_name: str, args: dict[str, Any]) -> Any:
    """Synchrones Tool-Aufruf-Interface (für chat_loop)."""
    import asyncio

    async def _run():
        return await call_tool(config, server_name, tool_name, args)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


def get_agent_tools(config: dict[str, Any]) -> list[dict[str, Any]]:
    """MCP-Tools für Agenten-Tool-Schema (synchron, lazy-connect im Hintergrund)."""
    if not MCP_AVAILABLE:
        return []

    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    out = []
    try:
        async def fetch_all():
            results = []
            for name in list_configured_servers(config):
                cfg = _server_config(config, name)
                if not cfg or not cfg.get("enabled", True):
                    continue
                if not cfg.get("expose_to_agent", True):
                    continue
                try:
                    tools = await get_tools(config, name)
                    for tool in tools:
                        results.append({
                            "server": name,
                            "name": tool.name,
                            "description": tool.description or "",
                            "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
                        })
                except Exception as e:
                    _log.warning("MCP-Server '%s' Tool-Discovery fehlgeschlagen: %s", name, e)
            return results

        out = loop.run_until_complete(fetch_all())
    finally:
        loop.close()

    return out


async def start_all_servers(config: dict[str, Any]) -> None:
    """Alle aktivierten MCP-Server verbinden (Tool-Discovery beim Start)."""
    if not MCP_AVAILABLE:
        return
    for name in list_enabled_servers(config):
        try:
            cfg = _server_config(config, name)
            if not cfg:
                continue
            await _connect(name, cfg)
            # Tool-Discovery durchführen
            await get_tools(config, name)
            _log.info("MCP-Server '%s' verbunden und Tools geladen", name)
        except Exception as e:
            _log.warning("MCP-Server '%s' konnte nicht verbunden werden: %s", name, e)
            # Alte Verbindung entfernen, falls fehlerhaft
            _clients.pop(name, None)


async def shutdown_all(config: dict[str, Any]) -> None:
    """Alle MCP-Verbindungen schließen."""
    for name in list(_clients.keys()):
        try:
            await _disconnect(_clients[name])
        except Exception:
            pass
    _clients.clear()


def is_connected(config: dict[str, Any], name: str) -> bool:
    """Prüft, ob verbunden."""
    return name in _clients


def status(config: dict[str, Any], name: str) -> dict[str, Any]:
    """Server-Status."""
    cfg = _server_config(config, name)
    if not cfg:
        return {"name": name, "configured": False}
    return {
        "name": name,
        "configured": True,
        "enabled": cfg.get("enabled", True),
        "connected": name in _clients,
        "transport": "http" if (cfg.get("url") or "").strip() else "stdio",
    }


def all_status(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [status(config, n) for n in list_configured_servers(config)]
