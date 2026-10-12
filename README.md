# How to Safely Let AI Agents Call Production APIs

A small gateway and fault-injection harness that measures what happens when an AI agent's valid, well-formed tool calls hit a system with state behind it. The gateway sits between the agent and a mock production service and enforces per-task tool scope, intent-derived idempotency keys, an in-doubt state for calls with unknown outcomes, version preconditions, mutation and spend budgets, a blast-scope limit, and plan-then-apply approval for irreversible actions. I tested it two ways: with deterministic scenarios that need no model, and with 1,300 live episodes of one model on four fault-injected tasks. This repository publishes the harness, the raw live-run data, and the tabulated results, so every number can be re-checked from the raw data.

DigitalOcean Community article: link added on publication.

## Headline Results

Primary runs with the oracle approver, reported as run0 / A / B, out of 100 episodes per arm per run. Source: `results/<run>/summary.md`.

| Metric | Raw | Ask-everything | Tiered |
| --- | --- | --- | --- |
| Episodes with at least one harmful mutation | 48 / 45 / 48 | 0 / 0 / 0 | 0 / 0 / 0 |
| Double executions | 23 / 20 / 23 | 0 / 0 / 0 | 0 / 0 / 0 |
| Stale-precondition mutations | 25 / 25 / 25 | 0 / 0 / 0 | 0 / 0 / 0 |
| Task completion rate | 52% / 55% / 52% | 100% / 100% / 100% | 93% / 93% / 97% |
| Approval prompts per task | 0 | 1.75 | 0.50 |

The result, stated plainly: without the gateway, 45 to 48 of every 100 episodes ended with at least one harmful mutation. With the gateway's full set of controls, none did, in all five runs. The live runs cannot attribute that result to a single control, and they did not exercise approval review, because the model never proposed a target a reviewer would reject. `METHODS.md` covers what the runs do and do not show.

## Repository Layout

```text
how-to-safely-let-ai-agents-call-production-apis/
  README.md
  METHODS.md
  LICENSE                     (MIT)
  CITATION.cff
  requirements.txt            (live runner only)
  SHA256SUMS.txt
  harness/                    gateway.py, scenarios.py, scenarios_output.txt, validate_run.py,
                              reconstruct_episodes.py, live/ (runner, tasks, protocol, scripted model), harness README
  data/<run>/                 config.json, episodes.jsonl, infra_errors.json, summary.md, transcripts/ (raw, as written by the runner)
  results/<run>/summary.md    Per-run summary tables
  results/validation/         Validator output for runs A and B
  environment/ENVIRONMENT.md  Software versions for the live runs
```

## Run The Deterministic Scenarios

The scenarios need only the Python 3.10+ standard library. Each one runs a scripted behavior twice, once directly against the service and once through the gateway, and counts the mutations that landed:

```bash
cd harness
python3 scenarios.py | diff - scenarios_output.txt && echo "matches"
```

```text
[secondary_label Output]
matches
```

## Re-Check The Live Runs

`harness/validate_run.py` reads a run folder and the harness, confirms that the code fingerprints recorded in `config.json` match the files in `harness/`, and checks that every planned episode is accounted for:

```bash
python3 harness/validate_run.py data/A-primary --root harness
```

The last line reads `RESULT: all applicable checks passed` for each of the five folders in `data/`.

## Re-Run On Your Own Endpoint

Test the runner first with the scripted model, which makes no API calls:

```bash
cd harness/live
python3 live_agent.py --fake --runs 1
```

For a real run, install the `openai` package, put your key in `MODEL_ACCESS_KEY`, and name the model:

```bash
pip install -r requirements.txt
cd harness/live
export MODEL_ACCESS_KEY="your-key"
python3 live_agent.py --model openai-gpt-5.4 --runs 25
python3 live_agent.py --model openai-gpt-5.4 --runs 25 --approver rubber-stamp --arms ask,tiered
```

The first command is a primary run (300 episodes), and the second is a rubber-stamp run (200 episodes). Each run writes a new folder under `harness/live/results/`. See `harness/README.md` for the tasks, arms, and options.

## Integrity

Every file in this repository, except `SHA256SUMS.txt` and `.gitignore` themselves, is checksummed:

```bash
shasum -a 256 -c SHA256SUMS.txt
```

## Citation

See `CITATION.cff`. A plain-text form:

Baranwal, Vinayak. "Blast-radius gateway and fault-injection harness for AI agents calling production APIs." 2026. `https://github.com/vinayakbaranwal12/how-to-safely-let-ai-agents-call-production-apis`

## License

MIT. See `LICENSE`.
