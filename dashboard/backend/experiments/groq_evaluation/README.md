# Groq vision pilot (isolated experiment)

Not part of the dashboard. It adds no routes or providers, writes no production records and makes no Gemini calls.

* **Model:** `qwen/qwen3.8-27b`, labelled **Preview** by Groq. As of 2026-11-19, Groq's docs say it supports vision (up to 3 images per request, 2,048 tokens per image), a 20 MB request limit, a 131K context window, 16,384 output tokens, and both JSON Object and JSON Schema modes.
  * Llama 4 Scout and Maverick have been shut down, and `qwen/qwen3.6-27b` was retired in favour of 3.8.
  * The only other vision model, `meta-models/muse-spark-open`, is enterprise-only and in limited preview.
* **Pair:** W30-0079 (2026-08-02) and W42-0095 (2026-10-25), FORECASTLE DECK AREA. The dashboard's image-pair check passes this pair.
* **Prompt:** the production `condition-compare-v1` prompt plus the production `ConditionAssessment` JSON Schema, with JSON Object mode. The output is checked with the production strict validator and the Phase 8.1 policy, both imported read-only.
* **Results:** `results/GRQ-*.json`. Each result holds the raw output, parsed JSON, schema validity, policy outcome, duration, usage and errors, plus the manual scores added afterwards. The API key is never written.
* **API key:** read only from the `GROQ_API_KEY` process environment variable. `.env` files are never read.

```
.venv\Scripts\python -m experiments.groq_evaluation.run_pilot            # dry run, no network
.venv\Scripts\python -m experiments.groq_evaluation.run_pilot --execute  # the single live request
```
