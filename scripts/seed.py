"""Create Shivani's vector index and seed embedded recovery procedures."""

import argparse
import asyncio
import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pymongo import AsyncMongoClient
from pymongo.operations import SearchIndexModel

from backend.agent.procedures import SEED_PROCEDURES, format_procedure_text
from backend.agent.retrieval import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_VECTOR_INDEX,
    EMBEDDING_DIMENSIONS,
    create_embedding,
)


COLLECTION_NAME = "recovery_procedures"


def vector_index_model(index_name: str) -> SearchIndexModel:
    """Return the locked Atlas Vector Search index definition."""

    return SearchIndexModel(
        definition={
            "fields": [
                {
                    "type": "vector",
                    "path": "embedding",
                    "numDimensions": EMBEDDING_DIMENSIONS,
                    "similarity": "cosine",
                },
                {"type": "filter", "path": "active"},
                {"type": "filter", "path": "fault_type"},
            ]
        },
        name=index_name,
        type="vectorSearch",
    )


async def ensure_collection(database: Any) -> Any:
    """Create the procedure collection if needed and return it."""

    if COLLECTION_NAME not in await database.list_collection_names():
        await database.create_collection(COLLECTION_NAME)
    return database[COLLECTION_NAME]


async def ensure_vector_index(collection: Any, index_name: str) -> str:
    """Create the vector index unless an index with that name exists."""

    cursor = await collection.list_search_indexes(index_name)
    existing = await cursor.to_list(length=None)
    if existing:
        return str(existing[0].get("status", "EXISTS"))

    return str(
        await collection.create_search_index(
            model=vector_index_model(index_name)
        )
    )


async def seed_procedures(collection: Any, openai_client: Any) -> int:
    """Generate embeddings and idempotently upsert the two seed runbooks."""

    for procedure in SEED_PROCEDURES:
        embedding = await create_embedding(
            format_procedure_text(procedure), openai_client
        )
        document: Dict[str, Any] = procedure.model_dump(exclude_none=True)
        document["embedding"] = embedding
        document["embedding_model"] = os.getenv(
            "EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
        )
        document["embedding_dimensions"] = EMBEDDING_DIMENSIONS
        await collection.replace_one(
            {"procedure_id": procedure.procedure_id},
            document,
            upsert=True,
        )
    return len(SEED_PROCEDURES)


async def run(index_only: bool = False) -> Dict[str, Any]:
    """Run collection/index setup and optionally embed and seed procedures."""

    load_dotenv()
    mongo_uri = os.getenv("MONGODB_URI")
    database_name = os.getenv("MONGODB_DB", "ghostshift")
    index_name = os.getenv("VECTOR_INDEX_NAME", DEFAULT_VECTOR_INDEX)
    if not mongo_uri:
        raise RuntimeError("MONGODB_URI is required")

    client = AsyncMongoClient(mongo_uri)
    try:
        await client.admin.command("ping")
        collection = await ensure_collection(client[database_name])
        index_status = await ensure_vector_index(collection, index_name)
        seeded = 0
        if not index_only:
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError(
                    "OPENAI_API_KEY is required to embed and seed procedures"
                )
            seeded = await seed_procedures(collection, AsyncOpenAI())
        return {
            "database": database_name,
            "collection": COLLECTION_NAME,
            "index": index_name,
            "index_status": index_status,
            "seeded": seeded,
        }
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="Create the collection/index without requiring an OpenAI key.",
    )
    args = parser.parse_args()
    result = asyncio.run(run(index_only=args.index_only))
    print(
        "Atlas setup complete: "
        f"database={result['database']}, "
        f"collection={result['collection']}, "
        f"index={result['index']}, "
        f"index_status={result['index_status']}, "
        f"seeded={result['seeded']}"
    )


if __name__ == "__main__":
    main()
