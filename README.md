# ECI Affidavit Downloader (Tamil Nadu, Assembly Gen/Bye)

Production-ready Python + Playwright automation to download candidate affidavits from the ECI affidavit portal:
<https://affidavit.eci.gov.in/>

## What this automation does

It automates:
1. Open portal.
2. Set filters:
   - Election: **Assembly Gen Bye Election**
   - Period: **Mar May 2026**
   - Type: **AC General**
   - State: **Tamil Nadu**
3. Apply search and switch to **Accepted** tab.
4. Iterate all candidates (with pagination support).
5. Open **View More**.
6. Download affidavit PDF.
7. Save as:
   - `~/Documents/TN electionss/affidavit/<Constituency Name>/<Candidate Name>.pdf`

---

## Features

- Sync Playwright workflow (stable and readable).
- Retry with exponential backoff for flaky UI/network actions.
- Resumable progress with `progress.json`.
- Idempotent behavior (skip existing files and completed candidates).
- Dry-run mode (navigate + log candidate names, no file downloads).
- Structured logging:
  - `logs/info.log`
  - `logs/error.log`
- Ctrl+C safe stop with progress persistence.
- Download validation (file exists + non-zero size).
- Human-like throttling between actions.
- CAPTCHA/human-verification pause and continue workflow.

---

## Project files

- `main.py` – full automation logic.
- `config.yaml` – selectors, runtime controls, paths, retries, browser settings.
- `requirements.txt` – Python dependencies.
- `.env.example` – optional user-agent override.

---

## Setup

### 1) Create Python environment (Python 3.11+)

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2) Install dependencies

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 3) Optional: configure user-agent

```bash
cp .env.example .env
# edit .env if needed
```

### 4) Review selectors in `config.yaml`

Website structures can change. Adjust selectors before first production run.

---

## Run

### Default run

```bash
python main.py
```

### Run with CLI options

```bash
python main.py --headless false --max-candidates 500 --resume true --dry-run false
```

### Dry-run example

```bash
python main.py --dry-run true --headless false --max-candidates 50
```

---

## Command-line options

- `--headless true/false`
- `--max-candidates N`
- `--resume true/false`
- `--dry-run true/false`
- `--config path/to/config.yaml`

CLI options override `config.yaml` runtime values.

---

## Progress and logs

### Progress file (`progress.json`)

Each candidate attempt appends a record with:
- `candidate_name`
- `constituency_name`
- `candidate_url`
- `status` (`success` / `failed` / `skipped`)
- `saved_path`
- `error_message`
- `timestamp`

Completed candidates are tracked for resume-safe skipping.

### Logs

- `logs/info.log` for action flow.
- `logs/error.log` for exceptions/failures.

End-of-run summary includes total seen, success, failed, skipped, and runtime.

---

## How to update selectors if website changes

All selectors are centralized in `config.yaml` under `selectors`.

1. Open site with headful mode:
   ```bash
   python main.py --headless false --dry-run true --max-candidates 10
   ```
2. Inspect failing step from logs.
3. Update specific selector list in priority order (most specific first, generic fallback later).
4. Re-run dry-run.

Selectors likely to need updates:
- filter dropdown selectors
- search/apply button selectors
- accepted tab selector
- candidate row/name/constituency selectors
- view-more and download selectors
- next-page selector

---

## Troubleshooting

### Timeout errors
- Increase `runtime.timeout_ms` and `runtime.navigation_timeout_ms` in `config.yaml`.
- Reduce `max_candidates` and test in smaller batches.
- Run headful (`--headless false`) to observe UI timing.

### Popup blocked / view-more not opening
- Some rows may navigate same tab instead of opening popup; script handles both.
- Update `view_more_candidates` selectors if portal markup changed.

### Missing download event
- Ensure download button selector matches active detail page.
- Some detail pages may render download after delay; raise timeout and add better selector.

### Stale row/element issues
- Retried automatically through exponential backoff.
- If frequent, reduce speed by increasing throttle values in config.

### CAPTCHA / human verification
- Script detects common indicators and waits.
- Solve challenge manually in browser window; script resumes automatically.
- Do **not** bypass security controls.

---


## Transfer to another GitHub repository

If you want to move these generated files to another repository (for example `election-affidavit-downloader-2026`), use:

```bash
./transfer_to_repo.sh https://github.com/<your-user>/election-affidavit-downloader-2026.git
```

The script copies only the project deliverables and then commits/pushes in the target repository:
- `main.py`
- `config.yaml`
- `requirements.txt`
- `.env.example`
- `README.md`

## Legal and compliance note

Use this automation only where permitted by the portal’s terms and applicable law. Respect load constraints (throttling is enabled) and do not attempt to bypass authentication/verification/security controls.

---

## Sample expected console output

```text
2026-04-11 10:00:01,123 | INFO | Opening portal: https://affidavit.eci.gov.in/
2026-04-11 10:00:08,844 | INFO | Applying filters: Election, Period, Type, State
2026-04-11 10:00:12,152 | INFO | Downloaded: A. Candidate -> /home/user/Documents/TN electionss/affidavit/XYZ/A. Candidate.pdf
2026-04-11 10:00:14,901 | INFO | Skipping (already completed): B. Candidate | ABC Constituency
2026-04-11 10:05:29,512 | INFO | Summary | total_seen=250 success=198 failed=12 skipped=40 runtime_sec=328.39
```
