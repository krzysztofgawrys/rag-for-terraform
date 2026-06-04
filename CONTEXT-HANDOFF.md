# Context handoff — Terraform RAG review & test build

> For a Claude agent picking up work in this repo. Written by the Claude
> instance that did a code review + built the test suite with the maintainer
> (Kit / kgawrys, OPS/DevOps at Cognitran, Terragrunt shop; project is
> **deployed internally at Cognitran**, early traction). Read this before
> proposing changes — several "obvious" critiques were raised and then
> correctly withdrawn after the maintainer pushed back. Don't re-litigate them.

---

## Retrieval disambiguation - built + verified (2026-06-04)

The hardest open question - does retrieval rank the right module #1 among the
~58 near-identical `terraform-aws-security-group/modules/*` presets - was driven
to a grounded answer. Four commits on top of `67289df`:

- **De-dilute embeddings** (`embeddings.py`): description-first, cap boilerplate.
  Doubled the discriminating cosine (0.26 -> 0.48 for the redis/memcached pair).
- **Structured ports** (`ports.py`, migration 011): each preset's listening
  ports resolved DETERMINISTICALLY from the canonical `rules` map (root module's
  `rules` variable) or inline `from_port` literals - never a name heuristic,
  never an LLM. `_resolve_repo_ports` runs after each index and commits.
- **Hybrid search** (`vector_store.similarity_search`, migration 010): fuses
  cosine + lexical tsvector RRF + an exact-port boost that breaks near-dup ties.
- **Gated lexical**: the lexical signal joins the fusion ONLY when the query
  carries a discriminating token (a port or a catalog service-name); on a pure
  category paraphrase, ts_rank (no idf) only adds noise, so cosine alone.

**Verified on a from-scratch v5.3.1 index (production pipeline, committed code),
intent-only queries (service name stripped), repo-scoped:**

| quadrant | cosine | RRF+port | note |
|---|---|---|---|
| A. query names a PORT | 4-5/8 | **8/8, DOWN=0** | deterministic, robust |
| B. pure category, NO port/name | 4-6/8 | **= cosine, DOWN=0** | irreducible |
| C. query names the SERVICE | 8/8 | **8/8, DOWN=0** | lexical helps |

A is the grounded win: the exact port lifts the right preset to #1, independent
of description quality. C is trivial. B is the floor - pure category intent on
near-dups (redis vs memcached, postgres vs mysql) genuinely cannot be separated
without a port or name; the gate guarantees the hybrid is never WORSE than cosine
there. **The earlier "8/8" was state-dependent** - manual re-embed + manual port
backfill masked a `_resolve_repo_ports` commit bug (it never committed its
writes); that is fixed and a fresh reindex now auto-resolves ports, no manual step.

**Inherit as fact (do not re-discover the hard way):**
- **Pin `checkout_ref=v5.3.1`** for this repo. `master` is now **v6**, a full
  refactor (`preset_ingress_rules`, catalog-generated, NO `rules` map); the port
  feature is v5.x-structure-specific and yields 0 ports + ~90% fallback
  descriptions on v6.
- **B's absolute is description-non-deterministic** (4-6/8 across reindexes): the
  eval gate does 1 retry, so a few presets fall back to generic descriptions per
  run. The retrieval CODE is correct; the variance is the indexing LLM.
- **Catalog-hygiene TODO**: the root `.` module (and `_templates`, `examples/`)
  are query candidates and can outrank real services (root `.` took #1 for a
  vault intent). Exclude non-service modules from `similarity_search` candidates.
- **Do not remove the lexical gate** - RRF harms pure intent unless gated.
- Local DB was reduced to v5.3.1 presets by the test reindexes; a full unfiltered
  reindex restores wrappers/examples/tags. Migrations 010+011 verified on a clean
  DB; full suite 260 green. Local-only `docker-compose.yml` (port 3000) +
  `frontend/nginx.conf` (CSP off) are intentionally uncommitted.

See memory `project_retrieval_disambiguation` for the full investigation trail.

---

## How to work with this maintainer

Precise, evidence-based, pushes back hard when reasoning doesn't match observed
facts. Corrects unverified claims presented as fact (verify before asserting —
prices, versions, behaviour). Dislikes code written without being asked, and
dislikes walls of text. Values being corrected over being flattered. If you
catch yourself reframing a request to make it easier, stop and check the actual
artifact instead. Everything below was verified against the real code (and a
real Postgres 16) unless explicitly marked "NOT verified".

---

## What the project is (and is NOT)

Self-hostable RAG over an org's own Terraform modules. Clones module repos,
parses HCL, embeds, stores in Postgres+pgvector. Separately indexes *consumer*
repos to learn how modules are actually used, and distils that into "convention"
snippets. Serves an agentic query pipeline + MCP server.

**Core design (defended across the review; treat as settled, not up for debate):**

1. **Two-layer retrieval.** Embedding *narrows* a dense, self-similar catalog
   (~2500 modules); when embedding scores tie among near-duplicates, the agent
   escalates to *usage* tools (`get_module_usage`, `find_similar_usages`) to
   break the tie by how modules are really used. This is the whole point of the
   embedding layer — near-duplicate disambiguation, where lexical/BM25 fails.
2. **Token economy.** At 2500 modules you cannot hand an agent the repos and let
   it grep — the catalog alone blows the context window every query.
   Precomputed dry data (what each module does, its variables/outputs) + a
   shortlist is the reason RAG exists here. Do not "simplify" this away.
3. **Descriptive, not normative, with a human in the loop.** The system gives an
   agent the ability to *use* the org's modules; it is NOT meant to fix the
   org's mess. It faithfully reflects existing practice — including tech debt.
   That is the spec, not a bug. The maintainer's explicit position: "the user's
   hand is required." Compose/query prompts also steer toward newest module
   versions, so straightening the mess *while* helping the agent is possible —
   but supervised, never autonomous.

**What it is NOT:** an autonomous code generator, a correctness oracle, or a
replacement for human review. The README oversells "autonomous agent" — the
real, stronger and less-attackable value is "assistive retrieval over modules
that live in raw git (no registry sees them) + convention learning, with human
oversight."

---

## Verdict evolution — critiques RAISED then WITHDRAWN

Each of these I raised; the maintainer pushed back; the pushback was correct.
Listed so you don't repeat them as if new.

1. **"Module browsing is a commodity — HashiCorp's official MCP overlaps."**
   WITHDRAWN. HashiCorp MCP serves the public registry, provider docs, and the
   HCP/TFE *private* registry — but only for modules *published to that
   registry*. Modules referenced by `git::` source (this org's case) are
   invisible to it. For a Terragrunt shop, HashiCorp MCP is useless for their
   own modules.

2. **"Why RAG instead of an agent with git grep?"** WITHDRAWN. See "token
   economy" above. Infeasible at 2500 modules.

3. **"Do embeddings/pgvector earn their complexity over a structured filter +
   keyword search?"** WITHDRAWN. The dominant query pattern is
   discovery-by-intent + disambiguation among 15+ near-identical modules, where
   semantic ranking is exactly the thing lexical search can't do.

4. **"Conventions are authoritative ⇒ the system launders bad practice with
   confidence (popularity over correctness)."** WITHDRAWN as an architectural
   flaw. The contract is descriptive; faithful reproduction of practice is the
   spec. Criticising it for not auto-correcting debt is criticising the tool for
   not being a different tool. (See residual below — it survives only as a
   *wording* issue, not a design one.)

## Critiques that SURVIVED (still actionable, but minor / non-architectural)

- **Wording, not behaviour:** in human-facing surfaces (UI, docs), the prompt
  word `AUTHORITATIVE` and the `STRONG/MODERATE/WEAK/LOW_EVIDENCE` labels read
  as *normative* over *descriptive* data. STRONG just means ">80% of
  deployments do this", not "recommended". Leadership/users may read "popular"
  as "endorsed". *Keep* the language inside the prompts — there it is
  load-bearing against the model's generic Terraform priors (anti-hallucination),
  which is a real failure mode. Only reframe the human-facing copy, e.g.
  "observed convention (N deployments, X% consistency)".
- **`eval.md` checks faithfulness, not correctness** (verified verbatim in the
  prompt). A consistent-but-wrong convention scores 5/5. This is an accepted,
  documented limitation — make sure it stays *documented*, not a surprise.
- **README positioning** — sell assistive-with-oversight, not autonomy.
- **BSL 1.1 on a solo, zero-traction repo** — friction without benefit; deters
  the contributors who'd give it traction, protects a commercial upside the
  maintainer has decided not to pursue. Recommendation: relicense Apache-2.0 /
  MIT now (don't wait for the 2029 auto-convert). (Maintainer's decision.)
- **Manual convention distillation** — `POST /consumer/distill` is manual, so
  "authoritative" conventions can silently drift from the module index. Wire it
  into reindex or a cron.
- **Demo bearer token in the public README** — on scan, NOT a code leak
  (`.env.example` uses placeholders, no hardcoded secrets in code). It's a
  deliberate demo credential for terraform-rag.io. Still: confirm it's
  read-only-scoped and consider rotating; a public working bearer undercuts the
  security framing.

---

## Code review snapshot (from the real repo)

- ~10.8k LOC Python (37 files), ~2.7k LOC TS frontend, 10 SQL migrations,
  604-line CLAUDE.md.
- Discipline is good: **0 bare `except:`**, 53 typed `except Exception` with
  logging; detailed MCP tool descriptions; honest module header comments.
- Biggest files: `agent.py` 932, `retriever.py` 923, `mcp_tools.py` 913,
  `indexer.py` 812, `vector_store.py` 692, `auth.py` 616.
- Prompts (`app/prompts/`) are clearly the result of patching real agent
  failures one by one — e.g. the `"False"`-is-truthy-in-HCL trap, exact output
  names, the final sanity pass. Not cargo-cult.
- Before this session: **no real test suite** (two ad-hoc distiller scripts).

---

## Test suite state — 127 tests, all green, zero mocking

Last full run: **127 passed** with Postgres up. Everything verified against
real code; graph tests against real Postgres 16. No mocks anywhere — pure
functions tested directly, DB logic against a real DB.

| File | # | Covers |
|---|---|---|
| `tests/test_consumer_parser.py` | 32 | `_resolve_source`, `_extract_version_ref` (branch refs → `""`) |
| `tests/test_consumer_summaries.py` | 25 | `_extract_vars`, `_guess_env`/`_guess_region`, `build_usage_summary`/`build_compose_summary` (GOLDEN strings) |
| `tests/test_consumer_repo_golden.py` | 11 | `parse_consumer_repo` end-to-end (siblings, env override, source_locator) |
| `tests/test_distiller_parsing.py` | 19 | `extract_assessment`, `strip_preamble` (+ documents the latent bug) |
| `tests/test_parser_golden.py` | 11 | `parse_module` golden, pinned to hcl2 4.3.2 |
| `tests/test_graph.py` | 10 | recursive-CTE forward/reverse traversal + cycle termination (needs Postgres; skips cleanly without) |
| `tests/test_eval_scoring.py` | 19 | retrieval-eval scoring: `forbidden_refs` + `top_rank` + MRR (pure) |
| `tests/conftest.py` | — | DB env, schema, edge-seeding helper, NullPool rebind |
| `tests/fixtures/...` | — | HCL fixtures for the golden tests |
| `scripts/eval_scoring.py` | — | extended pure scoring core (new) |
| `scripts/eval_queries_adversarial.yaml` | — | adversarial fixtures (placeholder refs) |

Run:
```bash
pip install pytest pytest-asyncio pytest-timeout python-hcl2==4.3.2 \
            structlog pydantic-settings "sqlalchemy[asyncio]" asyncpg pgvector
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 POSTGRES_USER=postgres \
POSTGRES_PASSWORD=postgres POSTGRES_DB=ragtest pytest tests/ -v
```

### Design choices in the tests (don't "fix" these)
- **Pure functions only, no mocking.** Agent loop, LLM distiller, and model
  embeddings are deliberately *not* unit-tested — non-deterministic output makes
  mock tests test the mock. Use the eval harness + a recall@k / MRR threshold
  for those instead.
- **`conftest` rebinds the engine to `NullPool`** so pooled asyncpg connections
  aren't reused across pytest-asyncio's per-test event loops ("Future attached
  to a different loop"). Same class of problem the app's `make_session_factory()`
  solves for Celery.
- **Graph tests skip (not fail) when no Postgres** via `requires_db`.

---

## Confirmed latent bug (NOT fixed — documented by agreement)

`app/services/convention_distiller.py :: extract_assessment` uses
`re.search(r'\nASSESSMENT:\s*(\S+)', ...)`. `\S+` is greedy over non-space, so a
trailing period is captured **into** the label:

```
"ASSESSMENT: STRONG."  ->  label "STRONG."   (not "STRONG")
```

A label of `"STRONG."` won't match any downstream check against
`{STRONG, MODERATE, WEAK, LOW_EVIDENCE}`, so the convention silently falls out
of `stale`-filtering / RAG injection. **Fix: `\S+` → `\w+`.**
`tests/test_distiller_parsing.py::test_extract_assessment_trailing_punctuation_is_kept_LATENT_BUG`
asserts the *current* (buggy) behaviour with an instruction to flip the
assertion to `"STRONG"` once fixed. The maintainer chose to document, not patch,
in this session — patch is theirs to make.

---

## Hard constraints / gotchas verified this session

- **`python-hcl2` MUST stay at 4.3.2** (pinned in `requirements.txt`). v5+
  (tested 8.1.2) embeds surrounding quotes in every string literal and block
  label: `'"aws_s3_bucket"'`, `'"bucket_name"'`, and `source = '"../s3"'` no
  longer starts with `../` so `_resolve_source` leaves it unresolved → the
  dependency graph silently loses edges. `test_parser_golden.py` is the guard.
  **Do not bump hcl2 without re-running the golden tests.**
- **`source_locator` docstring mismatch.** `ParsedUsage`'s docstring advertises
  `consumer-repo@sha:path/file.tf:Lstart-Lend`, but `_parse_file` emits
  `{repo}@{sha[:7]}:{rel_path}` with **no line range**.
  `test_consumer_repo_golden.py` pins the actual format. If you implement line
  ranges, update that assertion.

---

## Eval work — partial, and the one piece NOT verified end-to-end

The existing harness (`scripts/eval_retrieval.py` + `scripts/eval_queries.yaml`)
measures *recall* ("did the right module appear in top-K?") — which passes even
when ranking is bad, the exact opposite of what the near-duplicate use case
needs. Added a pure, tested scoring core that fixes this:

- `scripts/eval_scoring.py` — `score_case()` (pure) with two new fields:
  - `forbidden_refs`: refs that must NOT appear (within the rank window) — the
    testable form of "deprecated/popular module must not outrank current".
  - `top_rank`: expected ref must be within the first N, not just somewhere in
    top_k — makes the eval sensitive to disambiguation quality.
  - plus `reciprocal_rank` / `mean_reciprocal_rank` (MRR) for rank-aware
    aggregate reporting. Backward compatible (no new fields ⇒ original
    behaviour).
- `scripts/eval_queries_adversarial.yaml` — 6 template cases (disambiguation
  via `top_rank: 1`, deprecation guards via `forbidden_refs`). **All
  module_refs are placeholders — replace with real org modules.**

**NOT done / NOT verified:**
1. **Wiring** — `eval_retrieval.run_query` still uses its own inline matching.
   Replace that block with `score_case(...)` and the loader with
   `case_from_entry(...)` so the harness understands the new fields. ~10 lines,
   HTTP path, not runnable in a sandbox.
2. **End-to-end eval run** — requires a running FastAPI server + a populated
   knowledge base + the embedding model (sentence-transformers; HuggingFace was
   not reachable in the build sandbox). This is the only deliverable this
   session that is **not** verified against a live system. Owner: maintainer.

---

## Remaining test work, prioritized

1. Wire `eval_scoring` into `eval_retrieval.py` (HTTP, ~10 lines) and run the
   eval end-to-end against the real KB. Add an adversarial case for the real
   "15 near-duplicate modules, correct is #7" set with `top_rank: 1`.
2. `parser._extract_tags` edge cases that need files on disk: `tags.txt` /
   `.tags` / `TAGS`, `locals.tf` as a **dict** (not list), a symlink escaping
   the repo, `.terraform/` skip. Cheap add to `test_parser_golden.py`.
3. `app/core/migrations.py` — advisory-lock runner: ordering, idempotency
   (second run is a no-op), concurrent-start serialization. DB test in the
   `test_graph.py` style.
4. `vector_store` upsert / `similarity_search` with **injected deterministic
   vectors** (needs pgvector in the test Postgres) + `indexer` code-hash cache
   (skip-unchanged vs catch-changed; needs git/embedding mocks). Medium cost.
5. NOT as unit tests: agent loop, LLM distiller, model embeddings → eval harness
   + threshold. API routes / auth / webhook HMAC → FastAPI `TestClient`
   integration (auth + HMAC are security-sensitive and worth doing).

---

## One-line summary

Solid, deliberately-scoped tool solving a real problem (token economy +
disambiguation over raw-git modules no registry sees); architecture is settled
and defensible; the gaps are in *tests* (now largely closed — 127, zero mocks)
and in *how it's described/licensed*, not in how it works. One real latent bug
(`extract_assessment` trailing `.`), one hard pin (`hcl2==4.3.2`), and the eval
end-to-end run is the only thing left unverified.
