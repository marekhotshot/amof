"""Vector Database Memory module for AMOF.

Uses ChromaDB to store semantic indexes of codebase files and knowledge base articles.
ChromaDB requires sqlite3 >= 3.35.0; we optionally use pysqlite3-binary when system sqlite3 is older.
"""

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from amof.app_paths import vector_store_dir

# Use pysqlite3 if available so ChromaDB gets sqlite3 >= 3.35 (avoids "unsupported version" warning)
try:
    import pysqlite3
    sys.modules["sqlite3"] = pysqlite3
except ImportError:
    pass

try:
    import chromadb
except ImportError:
    chromadb = None

logger = logging.getLogger(__name__)


class VectorStore:
    """Manages the persistent ChromaDB connection."""

    def __init__(self, persist_directory: Optional[Path] = None):
        if chromadb is None:
            raise ImportError(
                "chromadb is not installed. Vector memory is optional. "
                "For pipx installs, run: pipx inject amof chromadb pysqlite3-binary."
            )
            
        self.persist_directory = persist_directory or vector_store_dir()
        self.persist_directory.mkdir(parents=True, exist_ok=True)
        
        # Use PersistentClient to save locally
        self.client = chromadb.PersistentClient(path=str(self.persist_directory))
        self._collections: Dict[str, Any] = {}

    def get_or_create_collection(self, ecosystem_name: str) -> Any:
        """Get or create a ChromaDB collection for a specific ecosystem."""
        name = ecosystem_name or "default_ecosystem"
        # ChromaDB collection names must be valid: [a-zA-Z0-9_-]
        safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:63]
        
        if safe_name not in self._collections:
            # Default embedding function is all-MiniLM-L6-v2 which runs locally
            collection = self.client.get_or_create_collection(name=safe_name)
            self._collections[safe_name] = collection
            
        return self._collections[safe_name]

    def upsert_document(
        self,
        doc_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        ecosystem_name: str = "default_ecosystem",
    ) -> None:
        """Chunk and store text in the vector database."""
        if not text or not text.strip():
            return
            
        collection = self.get_or_create_collection(ecosystem_name)
        
        # Simple character-based chunking for now
        max_chunk_size = 4000
        
        if len(text) > max_chunk_size:
            chunks = [text[i : i + max_chunk_size] for i in range(0, len(text), max_chunk_size)]
            ids = [f"{doc_id}_chunk_{i}" for i in range(len(chunks))]
            metadatas = []
            for i in range(len(chunks)):
                md = dict(metadata) if metadata else {}
                md["chunk_index"] = i
                md["source_id"] = doc_id
                metadatas.append(md)
                
            collection.upsert(
                documents=chunks,
                metadatas=metadatas,
                ids=ids,
            )
        else:
            collection.upsert(
                documents=[text],
                metadatas=[metadata or {}] if metadata else None,
                ids=[doc_id],
            )
            
    def search(
        self,
        query: str,
        ecosystem_name: str = "default_ecosystem",
        n_results: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retrieve relevant context from the vector database."""
        collection = self.get_or_create_collection(ecosystem_name)
        
        try:
            results = collection.query(
                query_texts=[query],
                n_results=n_results,
            )
        except Exception as e:
            logger.warning("Vector search failed: %s", e)
            return []
            
        formatted_results = []
        if results and results.get("documents") and results["documents"][0]:
            docs = results["documents"][0]
            metadatas = results.get("metadatas", [[]])[0] or [{}] * len(docs)
            distances = results.get("distances", [[]])[0] or [0.0] * len(docs)
            
            for doc, meta, dist in zip(docs, metadatas, distances):
                formatted_results.append({
                    "text": doc,
                    "metadata": meta,
                    "distance": dist,
                })
                
        return formatted_results