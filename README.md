# Albert

**Shared memory for AI agents that's safe to trust: self-hosted, multi-tenant, and built so untrusted content can't steer your agents.**

[![CI](https://github.com/JIGGAI/albert/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/JIGGAI/albert/actions/workflows/ci.yml)
![Version](https://img.shields.io/badge/version-0.4.0-blue)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17%20%2B%20pgvector-4169E1?logo=postgresql&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-22%20tools-5A45FF)
![Hooks](https://img.shields.io/badge/hooks-Claude%20Code%20%7C%20Codex%20%7C%20Cursor%20%7C%20OpenClaw-D97757)
![Graph stores](https://img.shields.io/badge/graph-PostgreSQL%20%7C%20FalkorDB%20%7C%20Neo4j-008CC1)

Albert is a self-hosted memory service. Agents store what they learn, find it
again by meaning, by keyword and by relationship, and you get a live console
that shows every memory, how it connects to the others, and exactly why a
search returned what it did.

![The memory map: clusters of related memories with one open in the reading pane](docs/images/console-map.png)

## Highlights

- **Safe to trust.** Every memory records how far its source is trusted. Every
  write path scans for injected instructions and quarantines them before any
  agent can recall them, credential-shaped text is refused, and recall labels
  what is unverified. Clients can lower a memory's trust, never raise it.
- **Memory for teams, not just users.** Organizations, workspaces and shared
  organization-wide memories, with a capability-scoped key per agent and an
  audit trail that tells a human's change from an agent's.
- **No model required; your data stays yours.** The baseline runs on PostgreSQL
  alone, with zero model calls to store or recall. Embedding models, an LLM
  classifier and Graphiti are opt-in upgrades, local or hosted.
- **Automatic memory where developers work.** Native hooks for Claude Code,
  Codex, Cursor and OpenClaw recall on every prompt and capture as work happens,
  failing open through an encrypted local queue.
- **Explainable retrieval.** For any recall you can see which retriever found
  each memory, how it scored, and which scope it came from.
- **Hybrid recall.** Keyword search, vector similarity and the knowledge graph
  each rank candidates; reciprocal-rank fusion merges them, and if one backend
  is down the others still answer.

---

## The life of a memory

A memory goes through five stages. Each step below is a real span in Albert's
flight recorder, so you can watch every one of them in the console.

```mermaid
flowchart LR
    subgraph S1["1 · Store"]
        A["Agent calls<br/>memory_remember<br/>or memory_ingest"]
    end
    subgraph S2["2 · Index (worker)"]
        B[classify] --> C[chunk] --> D[embed] --> E[write_edges] --> F[link]
    end
    subgraph S3["3 · Recall"]
        G[auth] --> H[resolve_scope]
        H --> I[lexical]
        H --> J[vector]
        H --> K[graph]
        I --> L[fuse]
        J --> L
        K --> L
    end
    subgraph S4["4 · Learn from use"]
        M["recall counts<br/>recalled-together links"]
    end
    subgraph S5["5 · See it"]
        N["Live · Replay · Map<br/>Operations · Backends"]
    end
    A --> B
    F --> G
    L --> M --> N
```

### 1. Store

An agent calls `memory_remember` (a fact it wants kept) or `memory_ingest` (a
whole document or transcript, kept as an *episode*), or a client records a
whole working session through [automatic capture](docs/API_AND_MCP.md#automatic-capture).
Albert checks the key's capabilities, refuses anything that looks like a
credential, writes the record into the key's workspace (or, when asked and
allowed, organization-wide for every workspace to share) and queues it for
indexing. The call returns immediately.

### 2. Index

The worker picks the job up and runs five steps:

| Step | What happens |
|---|---|
| `classify` | Labels the memory (fact, decision, procedure, warning and eight more) and its sensitivity, and extracts entities and relationships. A regex extractor by default; an LLM when you configure one. |
| `chunk` | Splits long text on paragraph boundaries so each piece gets its own embedding. |
| `embed` | Turns every chunk into a vector, stored in PostgreSQL with pgvector. |
| `write_edges` | Writes the extracted entities and relationships to the knowledge graph, and ends the validity of edges an edit made stale. |
| `link` | Connects the memory to its nearest neighbours (`similar`) and to the previous dated entry from the same folder (`sequence`). |

### 3. Recall

An agent calls `memory_search` or `memory_assemble_context`. After `auth` and
`resolve_scope` (which tenant, which workspace plus the organization-wide
memories it shares, which sensitivities this key may see), three backends run:

| Backend | Finds memories by | Good at |
|---|---|---|
| `lexical` | PostgreSQL full-text search | Exact names, ids, error strings |
| `vector` | Cosine similarity over chunk embeddings | Same meaning, different words |
| `graph` | Entities and relationships matching the query | "What uses X?", "Who owns Y?" |

`fuse` merges the three rankings with reciprocal-rank fusion and returns the
top results, each labelled with the backends that found it.
`memory_assemble_context` goes one step further and returns a ready-to-use
block of text with citations, cut to a size you choose.

### 4. Learn from use

Every counted search teaches Albert something. A background pass turns search
traces into recall counts per memory and `recalled` links between memories that
keep being returned together. Memories that get used grow on the map; ones
that never do are easy to spot.

### 5. See it

The console shows all of it as it happens. See [the console](#the-console).

---

## Structure map

```mermaid
flowchart TB
    subgraph Clients
        C1["Agents over MCP<br/>(Codex, Claude, Cursor, OpenClaw)"]
        C2["Apps and scripts over REST"]
        C3["Operators in a browser"]
    end
    MCP["MCP gateway :8081<br/>22 tools, forwards the caller's key"]
    API["REST API :8080<br/>FastAPI · auth · scopes · audit"]
    CON["Console :8082<br/>Next.js · operator UI"]
    W["Worker<br/>indexing jobs · housekeeping"]
    REC["Flight recorder<br/>spans for every request and job"]
    subgraph PG["PostgreSQL + pgvector"]
        T1["episodes · memories · chunks + vectors"]
        T2["links · recalls · stats"]
        T3["traces · jobs · audit"]
        T4["entities · relationships"]
    end
    GS["Graph store (choose one)<br/>PostgreSQL · FalkorDB · Neo4j"]
    LLM["Optional models<br/>embeddings · classifier"]
    C1 --> MCP --> API
    C2 --> API
    C3 --> CON --> API
    API --> PG
    API --> GS
    API --> REC --> T3
    W --> PG
    W --> GS
    W --> LLM
    W --> REC
    GS -. default .-> T4
```

Where things live in the repository:

```text
src/albert/
  api.py              REST endpoints
  mcp_server.py       the 22 MCP tools
  services.py         storing, editing, forgetting, indexing
  classifier.py       regex and LLM classification and extraction
  chunking.py         paragraph-aware chunking
  embeddings.py       hashing, sentence-transformers, OpenAI-compatible
  search.py           lexical + vector + graph retrieval and fusion
  graph_store.py      the graph store interface and backend catalogue
  graph.py            PostgreSQL graph store
  graph_cypher.py     FalkorDB and Neo4j graph stores
  links.py            similar / sequence / recalled links, recall statistics
  clusters.py         cluster detection and naming for the map
  recorder.py         flight recorder (spans)
  trace_writer.py     off-thread trace persistence
  trace_feed.py       live trace stream
  console*.py         operator API: traces, map, operations, backends
  worker.py           job worker and housekeeping
  security.py         API keys, capabilities, sensitivity
  scope.py            which workspaces a read covers (its own plus the shared ones)
  credentials.py      Settings API: scoped keys, connection links, workspaces
  capture*.py         automatic capture: sessions, events, profiles, screening, retention
  provenance.py       where captured work came from, normalized before storage
  portability.py      export and import
  admin.py            albert-admin command line
console/              the operator console (Next.js)
migrations/           Alembic migrations 0001-0013
tests/                pytest suite, including one contract run against every graph store
docs/                 guides (see below)
```

---

## The console

Six screens. Five are read-only views fed by the flight recorder; Settings
issues credentials and manages workspaces. The side menu shows what the
console's key can see, and every view can be narrowed to one workspace (plus
the organization-wide memories it shares) or to the shared memories alone.

**Live** streams every request and job as it happens. **Replay** opens any one
of them as a waterfall of its steps, with the candidates each backend returned
and the fusion math behind the final ranking.

**Map** draws every memory as a node, linked to the ones it resembles, follows
or is recalled with, gathered into named clusters. Nodes pulse as agents recall
and store. Click one to read it in full.

**Operations** answers five questions: who is using memory, what does it hold,
are searches finding things, is the knowledge graph being built, and is the
service healthy.

![Retrieval quality: which backend earns the hits, which searches found nothing](docs/images/console-operations.png)

**Backends** shows which graph store and graph builder are in use, proves the
store is reachable, and gives the exact steps to switch.

**Workspaces** creates, renames and deletes workspaces; deleting one asks whether
its memories become organization-wide, move to another workspace, or are
forgotten.

**Settings** issues least-privilege API keys and one-time connection links, each held to a workspace or the whole
organization and never wider than the console's own key.

![Backends: stores and builders with live status and how to turn each on](docs/images/console-backends.png)

---

## Choose your backends

PostgreSQL with embeddings is the baseline every install has. The rest is
configuration.

| Choice | Options | Setting |
|---|---|---|
| Embeddings | `hashing` (no download, for development), `sentence-transformers` (local), `openai-compatible` | `ALBERT_EMBEDDING_PROVIDER` |
| Graph builder | regex, LLM classifier, Graphiti temporal extraction | `ALBERT_GRAPH_BUILDER` |
| Graph store | PostgreSQL (default), FalkorDB, Neo4j | `ALBERT_GRAPH_STORE` |

All three graph stores pass the same behavioural tests, run in CI against real
servers. Graphiti requires FalkorDB or Neo4j plus OpenAI-compatible chat and
embedding models. Details and trade-offs:
[graph backends](docs/GRAPH_BACKENDS.md) and [providers](docs/PROVIDERS.md).

---

## Quick start

```bash
cp .env.example .env          # set ALBERT_POSTGRES_PASSWORD and ALBERT_API_KEY_PEPPER
docker compose up --build -d
```

Create the first tenant and administrator key (printed once; store it safely):

```bash
docker compose exec api albert-admin bootstrap \
  --organization "Example" --workspace "Default" --principal "Administrator"
```

Store a memory and find it again:

```bash
curl -H "Authorization: Bearer $ALBERT_API_KEY" -H "Content-Type: application/json" \
  -d '{"content": "The deploy script lives in ops/deploy.sh and needs VPN."}' \
  http://127.0.0.1:8080/v1/memories

curl -H "Authorization: Bearer $ALBERT_API_KEY" -H "Content-Type: application/json" \
  -d '{"query": "how do I deploy?"}' \
  http://127.0.0.1:8080/v1/search
```

| Service | Address |
|---|---|
| REST API and OpenAPI docs | `http://localhost:8080/docs` |
| MCP (streamable HTTP) | `http://localhost:8081/mcp` |
| Console | `http://localhost:8082` (after issuing it a key, see [deployment](docs/DEPLOYMENT.md)) |

Everything binds to loopback by default. Put it behind TLS before exposing it.

---

## What agents can do

The 22 MCP tools, each with a REST equivalent:

| Group | Tools |
|---|---|
| Remember | `memory_remember`, `memory_ingest`, `memory_update` |
| Recall | `memory_search`, `memory_assemble_context`, `memory_get`, `memory_get_episode` |
| Forget | `memory_forget`, `memory_forget_episode` |
| Knowledge graph | `memory_query_graph`, `memory_get_subgraph`, `memory_add_relationship` |
| Work in progress | `working_memory_start`, `working_memory_update`, `working_memory_get`, `working_memory_complete`, `working_memory_fail` |
| Coordination | `memory_lock_acquire`, `memory_lock_renew`, `memory_lock_release` |
| Portability | `memory_export`, `memory_import` |

---

## Documentation

| Read this | For |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | How Albert is built: data model, pipelines, settings |
| [Deployment](docs/DEPLOYMENT.md) | Running it, the console, backups, upgrades |
| [REST and MCP](docs/API_AND_MCP.md) | Endpoints, tools, capabilities, the console API |
| [Graph backends](docs/GRAPH_BACKENDS.md) | PostgreSQL, FalkorDB, Neo4j; regex, LLM, Graphiti |
| [Providers](docs/PROVIDERS.md) | Embedding and classification models |
| [Security](docs/SECURITY.md) | Keys, sensitivity, what the console can see |
| [Integration handbook](docs/integrations/README.md) | Connecting Codex, Claude, Cursor, OpenClaw, REST clients |
| [Automatic-memory release gate](docs/AUTOMATIC_MEMORY_RELEASE.md) | Supported hosts, SLOs, rollback, beta limits, release evidence |
| [Status](docs/STATUS.md) | What is done, and what is not |

---

## Where it stands

Albert 0.4 is ready for self-hosted use on a private network: local installs,
internal deployments behind TLS, and controlled agent integrations with scoped
keys. It has not had an external security audit and is not a compliance-certified
public service; [status](docs/STATUS.md) lists what that would take.

Albert is a standalone system with its own infrastructure, credentials,
database and identity.
