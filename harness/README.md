# Harness

The gateway, the deterministic scenarios, and the live runner. Everything here except `live/live_agent.py` uses only the Python 3.10+ standard library.

## Files

| File | What it is |
| --- | --- |
| `gateway.py` | The mock production service and the gateway |
| `scenarios.py` | Deterministic fault-injection scenarios, one per row of the output table |
| `scenarios_output.txt` | The exact output of `python3 scenarios.py` |
| `validate_run.py` | Read-only validity checks for one run folder |
| `reconstruct_episodes.py` | Rebuilds an empty `episodes.jsonl` from a run's transcripts, after proving the rebuild reproduces `summary.md` |
| `live/live_agent.py` | The live runner for the raw, ask, and tiered arms |
| `live/tasks.py` | The four tasks, their injected faults, and their scoring |
| `live/PROTOCOL.md` | The protocol, written and frozen before the first live run |
| `live/fake_model.py` | A scripted model for testing the runner without API calls |

## Runner Options

Run these from `harness/live/`.

| Option | Default | Meaning |
| --- | --- | --- |
| `--model` | none | Model slug at the endpoint |
| `--base-url` | `https://inference.do-ai.run/v1` | OpenAI-compatible endpoint, or `OPENAI_BASE_URL` if set |
| `--api-key-env` | `MODEL_ACCESS_KEY` | Environment variable that holds the key |
| `--runs` | 25 | Episodes per task per arm |
| `--arms` | `raw,ask,tiered` | Arms to run |
| `--tasks` | all four | Tasks to run |
| `--approver` | `oracle` | `oracle` or `rubber-stamp` |
| `--max-steps` | 20 | Step limit per episode |
| `--temperature` | provider default | Sampling temperature |
| `--seed` | none | Sampling seed, if the endpoint supports one |
| `--workers` | 1 | Episodes run in parallel |
| `--out` | `live/results` | Output folder |
| `--fake` | off | Use the scripted model and make no API calls |

## Output

Each run writes `results/<run_id>/` with `config.json` (settings and code fingerprints), `episodes.jsonl` (one scored row per episode), `transcripts/<episode_id>.json` (the full tool-call transcript), `infra_errors.json`, and `summary.md`.
