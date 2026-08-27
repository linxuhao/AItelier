# Models and providers: the full routing story

The README's [Quick Start](../README.md#quick-start) gets a deployment running;
this page is the reference behind it — what the routing tables mean, the
failover policy, and what each model name has to be.

## The three levels

Two of these used to share the word "model", which is why they now do not:

| | example | what it is |
|---|---|---|
| **provider** | `ark` | a host — a base URL plus the NAME of the secret it reads |
| **endpoint** | `ark/deepseek-v4-flash` | one concrete place to send a call |
| **model** | `flash` | an ordered list of endpoints. **This is what `agent_configs` name** |
| **agent** | `task_implementer` | a model + tools + a prompt template |

Order inside a model is policy, not preference: calls bind to the **first**
endpoint and the rest are tried only when one fails. So the last one should be
**pay-as-you-go** — a token plan runs out, and that final entry is what turns
"everything stops until the window resets" into "the next call goes elsewhere".
A spent plan is parked until the provider's own reset time. Binding is sticky
per pipeline step, never per call: provider prefix caches are per-provider, and
this workload measures 26:1 prefill:decode at an 89.4% hit rate, so alternating
endpoints mid-step converts cached input into full-price input.

**Several subscription plans on one model?** A route may use the dict form:

```jsonc
"flash": {"rotate":   ["ark/deepseek-v4-flash", "qwen/qwen3.8-flash"],
          "fallback": ["deepseek/deepseek-v4-flash"]}
```

Each new gateway — one per STEP — starts one position further around the
`rotate` pool, so consecutive steps draw on different plans and every plan's
5h/weekly window gets used instead of expiring behind a sticky first pick.
The step itself stays on one endpoint (the cache economy above lives inside a
step's tool loop; across steps only the system prompt is shared), failover
order is preserved from wherever the rotation started, and `fallback`
(pay-as-you-go) never rotates forward — it still only spends money when the
whole pool has failed or is parked. Plain-list routes behave exactly as
before.

## Pick your providers

The two tables are **deployment config, not repo content** — gitignored like
`.env`, shipped as examples:

```bash
cp llm_providers.example.json llm_providers.json   # providers: URL + key NAME
cp model_routes.example.json  model_routes.json    # models: which endpoints serve each
```

The **model names are the contract**: `agent_configs/*.yaml` and the vision
gate reference `flash` / `pro` / `glm` / `smart` / `vision`, so those keys must
exist. What sits behind them is yours — the examples are one operator's answer.
Skip this and both fall back to the example, so a fresh clone still runs.

What each model is FOR is fixed, because pipelines and tools pick by job:

| model | used by | has to be |
|---|---|---|
| `flash` | most maker and reviewer roles — the bulk of every run | cheap and fast; this is where the budget goes |
| `pro` | the PM, and roles the rest of the run is built on | a stronger generalist |
| `glm` | the architect, the final verifier, long-form documents | an alternative strong generalist |
| `smart` | offered to generated pipelines for judges and architects | strong at one-shot reasoning; **not** for long agentic tool loops |
| `vision` | the Godot readability gate | **must accept image input** — verify with a real frame, not a model card |

Keys are never in these files: a provider records the key's NAME, and the key
itself is a secret file (`~/.aitelier-secrets/<NAME>`). Editing all of this at
runtime — including from another agent — is the `/api/models` REST surface and
the matching [MCP tools](../README.md#use-aitelier-from-another-agent-mcp).

## Keys

Keys are **secret FILES, not environment variables** — so the test and build
subprocesses a pipeline runs cannot inherit them. Ask the code which ones you
need rather than trusting this page:

```bash
python -c "from core.external_deps import required_llm_keys, failover_llm_keys; \
           print('required:', required_llm_keys()); print('failover:', failover_llm_keys())"
```

`required` is the **first** endpoint of each model — what runs actually bind
to. `failover` is everything behind them: not needed to start, needed for an
outage to be a slowdown instead of a stop. The names are *derived from your
provider tables*, never fixed by documentation; with the shipped example
tables the probe prints `ARK_API_KEY` required, DeepSeek and Qwen as failover.

```bash
mkdir -p ~/.aitelier-secrets && chmod 700 ~/.aitelier-secrets
printf '%s' "<your-key>" > ~/.aitelier-secrets/<KEY_NAME>   # whatever the probe printed
chmod 600 ~/.aitelier-secrets/<KEY_NAME>
cp .env.example .env        # endpoints and options; NOT the keys
```

A missing key fails naming the provider, the key, the file to create, **and
the model whose endpoint list sent it there**.

One Docker-specific constraint: the container mounts **only the secret names
enumerated in `docker-compose.yml`** — the secrets dir itself is deliberately
not bind-mounted, so keys never appear in any workspace-visible path. Register
a provider whose key name isn't in that list and the file will sit unread on
the host until you add a matching `secrets:` entry to the compose file.
