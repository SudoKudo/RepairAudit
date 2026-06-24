# RepairAudit

This repo builds participant kits, ingests returned runs, scores edited code, and writes aggregate outputs.

The current flow is security-only. The code still writes `condition.txt` because older parts of the pipeline expect it, but the value is fixed to `security`.

## Scope

- Build kits from snippet metadata or a dataset CSV
- Run the participant web app against a locked LLM endpoint
- Score returned edits with detectors and, if enabled, the LLM judge
- Audit the judge and freeze one config for scoring
- Write summaries, modeling files, and the offline HTML report

The checked-in snippets and tests cover Python, C, C++, Java, and the dataset-backed path used for other languages.

## Repository Layout

```text
RepairAudit/
|-- README.md
|-- config.yaml
|-- requirements.txt
|-- .python-version
|-- .gitignore
|
|-- gui/
|   |-- kit_builder_gui.py              # Build participant kits
|   `-- study_gui.py                    # Run ingest, analysis, and report workflow
|
|-- scripts/
|   |-- study_cli.py                    # Main CLI entrypoint
|   |-- participant_kit.py              # Kit builder and cleanup logic
|   |-- participant_web_app_template.py # Participant web app stamped into each kit
|   `-- privacy_check.py                # Pre-publish scan
|
|-- tools/
|   |-- analysis/
|   |   |-- analyze_edits.py            # Per-run scoring pipeline
|   |   |-- detectors.py                # SQLi/CMDi heuristics
|   |   |-- llm_judge.py                # Judge prompts, parsing, and voting
|   |   |-- judge_audit.py              # Calibration-set builder and audit sweep
|   |   |-- interaction.py              # Merge snippet_log.csv into results.csv
|   |   |-- metrics.py                  # Per-run summary writers
|   |   |-- modeling.py                 # Snippet-level model dataset and fit
|   |   `-- stats.py                    # Aggregate summary stats
|   |
|   |-- domain_classification/
|   |   |-- classify.py                 # Add expertise labels to a raw dataset
|   |   |-- participant_ready.py        # Filter to rows safe to hand to participants
|   |   |-- verify_class_distribution.py # Check label spread after classification
|   |   |-- expertise_areas.jsonl        # Closed expertise taxonomy
|   |   `-- dataset_classified.zip       # Downloadable archive copy of the large dataset
|   |
|   |-- instrumentation/
|   |   |-- capture_env.py               # Save a public-safe environment snapshot
|   |   |-- diff_runner.py               # Build unified diffs and edit counts
|   |   |-- snippet_timer.py             # Record per-snippet timing markers
|   |   `-- start_timer.py               # Record run start/end timing
|   |
|   |-- reporting/
|   |   `-- html_report.py               # Build the offline HTML report
|   |
|   `-- validators/
|       `-- bandit_runner.py             # Run Bandit as a secondary check
|
|-- snippets/
|   |-- baseline/
|   |-- gold/
|   `-- prompts/task_descriptions.md
|
|-- data/
|   |-- metadata/snippet_metadata.csv
|   |-- datasets/classified/            # Local working CSVs for dataset-backed kits
|   |-- raw/.keep
|   `-- aggregated/.keep
|
|-- docs/figures/                        # README screenshots
|-- tests/                               # Regression tests for the main pipeline paths
|-- participant_kits/                   # Local generated kits; gitignored
`-- runs/                               # Local imported runs; gitignored
```

## Local Setup

The repo is pinned to Python `3.10.11` because that is the last Python `3.10` Windows release with official installers.

Example setup on Windows:

```powershell
git clone https://github.com/SudoKudo/RepairAudit.git
cd RepairAudit
C:\Users\<you>\AppData\Local\Programs\Python\Python310\python.exe -m venv venv
venv\Scripts\activate
venv\Scripts\python.exe -m pip install -r requirements.txt
```

If you already have `python` or `py` pointing at Python `3.10`, use that instead.

## LLM Endpoints

There are two LLM settings in this repo:

- Researcher-side judge settings live in [config.yaml](C:/Users/Bao%20Bun/Documents/GitHub/RepairAudit/config.yaml).
- Participant kit settings are stamped into each generated kit.

The participant app expects an Ollama-compatible HTTP endpoint. That can be:

- A local Ollama server on the participant machine
- A lab-hosted endpoint that exposes the same API shape

Current judge default:

- Model: `qwen2.5-coder:7b-instruct`
- URL: `http://localhost:11434/api/generate`

Current kit-builder default:

- Model: `qwen3.6:27b`

If you use a remote endpoint for participants, set that URL in the kit builder before generating the kit.

## Normal Flow

1. Prepare the kit source.
2. Audit the LLM judge if the judge config changed.
3. Build participant kits.
4. Distribute kits and collect returned ZIP files.
5. Extract each return ZIP into `runs/<phase>/`.
6. Analyze runs and merge interaction logs.
7. Build aggregate outputs and the HTML report.

## Main Commands

Commands below assume the virtual environment is active, or that you call `venv\Scripts\python.exe` directly.

### Build participant-ready dataset

```powershell
venv\Scripts\python.exe tools\domain_classification\classify.py `
  --input path\to\raw_source.csv `
  --output data/datasets/classified/dataset_classified.csv `
  --model qwen3.6:27b `
  --fresh

venv\Scripts\python.exe tools\domain_classification\verify_class_distribution.py `
  --input data/datasets/classified/dataset_classified.csv

venv\Scripts\python.exe -m scripts.study_cli build-participant-ready-dataset `
  --input_csv data/datasets/classified/dataset_classified.csv `
  --output_csv data/datasets/classified/dataset_participant_ready.csv `
  --rejected_csv data/datasets/classified/dataset_participant_rejected.csv
```

### Audit the judge

```powershell
venv\Scripts\python.exe -m scripts.study_cli build-judge-calibration
venv\Scripts\python.exe -m scripts.study_cli judge-audit --write_global_freeze
```

Outputs:

- `data/aggregated/judge_calibration.csv`
- `data/aggregated/judge_audit/audit_<timestamp>/`
- `data/aggregated/judge_freeze.json`

### Build participant kits

GUI:

```powershell
venv\Scripts\python.exe gui\kit_builder_gui.py
```

![Participant Kit Builder GUI](C:/Users/Bao%20Bun/Documents/GitHub/RepairAudit/docs/figures/Participant_Kit_Gui.png)
*Figure 1. Kit builder for participant IDs, endpoint settings, expertise areas, and sampling controls.*

CLI:

```powershell
venv\Scripts\python.exe -m scripts.study_cli build-participant-kit `
  --participant_id P101 `
  --phase pilot `
  --metadata_csv data/datasets/classified/dataset_participant_ready.csv `
  --expertise_areas "Backend / API Development,Security / Application Security" `
  --samples_per_hardness 3 `
  --selection_seed 42 `
  --participant_os windows `
  --llm_base_url https://lab-llm.example.edu `
  --out_root participant_kits
```

### Analyze returned runs

```powershell
venv\Scripts\python.exe -m scripts.study_cli analyze-run --participant_id P101 --phase pilot --metadata_csv data/metadata/snippet_metadata.csv
venv\Scripts\python.exe -m scripts.study_cli merge-interaction --run_dir runs/pilot/P101
```

### Build aggregate outputs

```powershell
venv\Scripts\python.exe -m scripts.study_cli aggregate-pilot
venv\Scripts\python.exe -m scripts.study_cli compute-stats --in_csv data/aggregated/pilot_summary.csv
venv\Scripts\python.exe -m scripts.study_cli compute-models --runs_root runs/pilot
venv\Scripts\python.exe -m scripts.study_cli build-report --phase pilot --out_html data/aggregated/report.html
```

### Study GUI

```powershell
venv\Scripts\python.exe gui\study_gui.py
```

Use it to:

- Review imported runs
- Run judge calibration and audit
- Toggle frozen judge scoring
- Launch analyze, merge, aggregate, stats, model, and report steps

![Research Console GUI](C:/Users/Bao%20Bun/Documents/GitHub/RepairAudit/docs/figures/Research_Console_GUI.png)
*Figure 2. Research console for ingest review, judge settings, and pipeline execution.*

## Kit Source Rules

Dataset-backed kit generation expects:

- `code_sample`
- `language`
- `hardness_strict`
- `primary_expertise_area`
- `sample_id` or `row_uid`

Useful extra columns:

- `secondary_expertise_areas`
- `file_path`
- `source_project`
- `cwe_primary`
- `vulnerability_type`
- `is_vulnerable`

Sampling rules:

- The builder filters by the selected expertise areas using both primary and secondary labels.
- It then selects `3` low, `3` medium, and `3` high rows by default.
- If one expertise bucket is short, it backfills from the same hardness bucket.
- If no expertise areas are selected, it samples from the full source file.

The CLI flag name is still `--metadata_csv` for compatibility, even when it points to a dataset CSV.

## Participant Kit Layout

Each generated kit keeps the top level small:

```text
participant_kits/<participant_id>/
|-- Launch_Study_Web_App.<bat|sh>
|-- README.md
|-- exports/
`-- .repairaudit/
    |-- participant_web_app.py
    |-- package_submission.py
    |-- study_config.lock.json
    |-- kit_manifest.json
    `-- run_<phase>_<participant_id>/
```

Participant-facing files:

- The launcher
- The browser app
- The finished ZIP inside `exports/`

Researcher-side local files:

- `participant_kits/_researcher_maps/`
- `participant_kits/_share_zips/`

## Run Folder Contract

An extracted participant return should contain:

- `baseline/`
- `edits/`
- `logs/snippet_log.csv`
- `logs/chat_log.jsonl`
- `logs/participant_profile.json`
- `study_assignment.json`
- `condition.txt`

Analysis adds:

- `analysis/results.csv`
- `analysis/summary.json`
- `analysis/summary.txt`
- `analysis/bandit.json`
- `diffs/*.diff`

## Judge Notes

The LLM judge is optional. When enabled, it can run:

- `cot`
- `zero_shot`
- `few_shot`
- `adaptive_cot`
- `self_verification`
- `self_consistency`

Parser modes:

- `strict_json`
- `embedded_json`
- `tolerant_json`

Vote rules:

- `majority`
- `conservative_present`
- `highest_confidence`

If you want repeatable scoring after a calibration pass, use the frozen judge config in `data/aggregated/judge_freeze.json`.

## Main Outputs

Per run:

- `runs/<phase>/<participant_id>/analysis/results.csv`
- `runs/<phase>/<participant_id>/analysis/summary.json`
- `runs/<phase>/<participant_id>/analysis/summary.txt`
- `runs/<phase>/<participant_id>/analysis/bandit.json`
- `runs/<phase>/<participant_id>/diffs/*.diff`

Aggregate:

- `data/aggregated/pilot_summary.csv`
- `data/aggregated/pilot_stats.txt`
- `data/aggregated/pilot_model_data.csv`
- `data/aggregated/pilot_models.json`
- `data/aggregated/pilot_models.txt`
- `data/aggregated/report.html`

## Checks

CLI help:

```powershell
venv\Scripts\python.exe -m scripts.study_cli --help
```

Compile check:

```powershell
venv\Scripts\python.exe -m compileall gui scripts tools tests
```

Tests:

```powershell
venv\Scripts\python.exe -m unittest discover -s tests -p "test*.py" -v
```

Privacy scan:

```powershell
venv\Scripts\python.exe -m scripts.study_cli privacy-check
```

## Publish Rules

Do not commit:

- `participant_kits/` contents
- `runs/` contents
- `data/aggregated/` outputs
- large local classified CSVs under `data/datasets/classified/`
- local venv folders
- GUI cache files

What stays in Git should mostly be source, tests, metadata, snippets, and docs.

## Troubleshooting

### Participant app does not open

- Confirm Python is installed on the participant machine.
- Run the launcher from the kit root.
- Keep the launcher terminal window open while the app is running.
- If the browser does not open automatically, use the localhost URL printed by the launcher.

### Participant app cannot reach the assistant

- If the kit uses a remote endpoint, verify network or VPN access.
- If the kit uses local Ollama, start `ollama serve` first.
- Check that the endpoint URL stamped into the kit is still valid.

### The report looks stale

Re-run in this order:

1. `analyze-run`
2. `merge-interaction`
3. `aggregate-pilot`
4. `compute-stats`
5. `compute-models`
6. `build-report`
