"""Memory Search Tool for the Orchestrator."""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Tool, ToolResult

logger = logging.getLogger(__name__)


class MemorySearchTool(Tool):
    """Searches the vector database for past knowledge, architecture, and codebase indexing."""

    name = "MemorySearch"
    description = (
        "Search the local vector database for past knowledge, architectural decisions, "
        "journal entries, and codebase summaries. "
        "Always use this tool first to understand the context before formulating a plan."
    )

    parameters = {
        "type": "object",
        "properties": {
            "search_query": {
                "type": "string",
                "description": "Semantic query to search for (e.g., 'How does authentication work?' or 'What are the rules for modifying deployment scripts?').",
            },
        },
        "required": ["search_query"],
    }

    def __init__(self, vector_store: Any, ecosystem_name: str = "default_ecosystem"):
        self.vector_store = vector_store
        self.ecosystem_name = ecosystem_name

    def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("search_query", "")
        if not query:
            return ToolResult(success=False, output="", error="Missing search_query")

        if not self.vector_store:
            return ToolResult(
                success=False,
                output="",
                error="Vector store is not initialized (ChromaDB might be missing).",
            )

        try:
            results = self.vector_store.search(
                query=query,
                ecosystem_name=self.ecosystem_name,
                n_results=5,
            )
            
            if not results:
                return ToolResult(success=True, output="No relevant information found in memory.")

            output_parts = [f"Found {len(results)} results for '{query}':\n"]
            for i, res in enumerate(results, 1):
                meta = res.get("metadata", {})
                source = meta.get("source_id", "unknown")
                score = res.get("distance", 0.0)
                text = res.get("text", "")
                
                output_parts.append(f"--- Result {i} (Source: {source}, Distance: {score:.2f}) ---")
                output_parts.append(text)
                output_parts.append("")

            return ToolResult(success=True, output="\n".join(output_parts))
        except Exception as e:
            logger.exception("MemorySearch failed")
            return ToolResult(success=False, output="", error=f"MemorySearch failed: {e}")