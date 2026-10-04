# Embedding and classification providers

Albert supports a self-contained development embedder, a local semantic model,
and OpenAI-compatible embedding and classification APIs. The `runtime-check`
Compose service validates the selected provider before the API and worker start.

## Development hashing mode

This is the zero-download default:

```dotenv
ALBERT_INSTALL_SEMANTIC=false
ALBERT_EMBEDDING_PROVIDER=hashing
ALBERT_EMBEDDING_DIMENSIONS=384
ALBERT_LLM_PROVIDER=none
```

It provides deterministic vectors and exercises the complete pgvector path,
but similarity is token-based rather than genuinely semantic. Use it for local
development, tests, and offline smoke checks.

## Local semantic mode

The default local semantic model is `BAAI/bge-small-en-v1.5`, which emits 384
dimensions:

```dotenv
ALBERT_INSTALL_SEMANTIC=true
ALBERT_EMBEDDING_PROVIDER=sentence-transformers
ALBERT_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ALBERT_EMBEDDING_DIMENSIONS=384
ALBERT_LLM_PROVIDER=none
```

Build and start the stack:

```bash
docker compose build
docker compose up -d
```

The semantic image includes `sentence-transformers`. API and worker containers
share the `albert-model-cache` volume, so the runtime probe downloads the model
once and both services reuse it. Size hardware for the chosen model.

## OpenAI-compatible semantic mode

Use any endpoint implementing the OpenAI embeddings contract:

```dotenv
ALBERT_INSTALL_SEMANTIC=false
ALBERT_EMBEDDING_PROVIDER=openai-compatible
ALBERT_EMBEDDING_MODEL=text-embedding-3-small
ALBERT_EMBEDDING_DIMENSIONS=384
ALBERT_EMBEDDING_REQUEST_DIMENSIONS=true
ALBERT_OPENAI_BASE_URL=https://api.openai.com/v1
ALBERT_OPENAI_API_KEY=<secret-store value>
```

The startup probe performs one embedding request and refuses to start if the
provider fails or returns the wrong number of dimensions.

The immutable Albert schema uses 384 dimensions. Set
`ALBERT_EMBEDDING_REQUEST_DIMENSIONS=true` only when the provider supports the
optional OpenAI `dimensions` request field. A provider that cannot emit 384
dimensions requires a deliberate database migration before use. Albert caps
embeddings at 2,000 dimensions because pgvector's HNSW `vector` operator class
has that limit.

## Chunking

Every memory is embedded as a sequence of overlapping chunks, so long episodes
are neither rejected by remote embedders with input limits nor silently
truncated by local models:

```dotenv
ALBERT_CHUNK_CHARACTERS=1500
ALBERT_CHUNK_OVERLAP_CHARACTERS=200
ALBERT_MAX_CHUNKS_PER_MEMORY=200
```

Chunks follow paragraph and sentence boundaries where possible. A memory that
exceeds the chunk cap is indexed up to the cap and marked
`metadata.embedding.truncated = true`. Vector search returns the best-matching
chunk per memory and reports its `chunk_index`.

`ALBERT_MIN_VECTOR_SIMILARITY` defaults to `0.2`. Results below the threshold
are omitted instead of presenting an unrelated nearest neighbor as relevant.
Tune it with a representative evaluation corpus when changing embedding models.

## Model-based classification and graph extraction

Classification can use the same or another OpenAI-compatible endpoint:

```dotenv
ALBERT_LLM_PROVIDER=openai-compatible
ALBERT_CLASSIFICATION_MODEL=<JSON-capable model name>
ALBERT_OPENAI_BASE_URL=<provider base URL>
ALBERT_OPENAI_API_KEY=<secret-store value>
```

The classifier requests JSON containing memory type, sensitivity, entities, and
typed relationships, and tells the model the allowed values for each label.
Output is parsed, bounded (40 entities, 60 relationships) and validated before
it reaches storage; a label outside the vocabulary falls back to `fact` or
`internal` rather than failing the memory's indexing. The model must accept
`temperature: 0` and `response_format: json_object`.

Pick a model that reliably returns relationships, not only entities. On a
sample of real team notes, `gpt-4.1-mini` produced typed edges for most
memories while `gpt-4.1-nano` produced entities and no edges at all. Without a model, deterministic extraction recognizes
common relationships such as `uses`, `depends_on`, `integrates_with`, `runs_on`,
`belongs_to`, `owns`, `owned_by`, `manages`, `supports`, `prefers`, and
`works_on`.

Never put provider credentials in memory, Git, or agent instructions.

## Changing providers

If the new embedding model has the same dimensions, deploy the new provider and
queue existing active memories:

```bash
docker compose run --rm api albert-admin reindex-memories
```

Use `--all` to force complete graph and embedding regeneration. The worker
processes queued jobs and expires extracted graph edges that are no longer
present. Until a memory is re-embedded, vector search skips its old chunks:
vectors from a different model are never compared with the active model's
query vector, so results degrade to lexical and graph for those memories
rather than ranking noise.

Changing vector dimensions changes the PostgreSQL column type and HNSW index.
Do not change `ALBERT_EMBEDDING_DIMENSIONS` on an existing database without a
planned data migration. `albert-admin validate-runtime` detects a mismatch and
stops startup instead of silently producing invalid vectors.

## Manual validation

```bash
docker compose run --rm runtime-check
docker compose exec api albert-admin validate-runtime --probe-embedding
```

The command checks configuration, provider output dimensions, the PostgreSQL
vector column, and the HNSW index without printing credentials.
