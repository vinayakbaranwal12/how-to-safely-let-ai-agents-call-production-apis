# Environment

The five live runs in `data/` were made on October 8, 2026, on a Mac with Apple silicon (macOS, Darwin 27.0.0, arm64).

| Component | Version |
| --- | --- |
| Python | 3.14.8 |
| openai (Python package) | 3.26.1 |
| Model | `openai-gpt-5.4` on DigitalOcean Serverless Inference (`https://inference.do-ai.run/v1`) |

The deterministic scenarios (`harness/scenarios.py`) use only the standard library and produce identical output on any Python 3.10 or later.
