import argparse
import json
import logging
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
from dotenv import load_dotenv
from playwright.sync_api import Browser, BrowserContext, Error, Locator, Page, TimeoutError, sync_playwright


STOP_REQUESTED = False


def setup_signal_handlers() -> None:
    def _handler(signum, frame):  # noqa: ARG001
        global STOP_REQUESTED
        STOP_REQUESTED = True
        logging.getLogger("eci_automation").warning("Stop requested (signal=%s). Graceful shutdown in progress.", signum)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


@dataclass
class CandidateRecord:
    candidate_name: str
    constituency_name: str
    candidate_url: str
    status: str
    saved_path: str
    error_message: str
    timestamp: str


class ProgressStore:
    def __init__(self, progress_path: Path) -> None:
        self.progress_path = progress_path
        self.data: Dict[str, Any] = {
            "meta": {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            "records": [],
            "completed_keys": {},
        }
        self._load_if_exists()

    def _load_if_exists(self) -> None:
        if self.progress_path.exists():
            try:
                self.data = json.loads(self.progress_path.read_text(encoding="utf-8"))
            except Exception:
                logging.getLogger("eci_automation").exception("Progress file exists but could not be read. Starting a fresh progress state.")

    def _candidate_key(self, candidate_name: str, constituency_name: str) -> str:
        return f"{candidate_name.strip().lower()}::{constituency_name.strip().lower()}"

    def is_completed(self, candidate_name: str, constituency_name: str) -> bool:
        return self._candidate_key(candidate_name, constituency_name) in self.data.get("completed_keys", {})

    def add_record(self, record: CandidateRecord) -> None:
        key = self._candidate_key(record.candidate_name, record.constituency_name)
        if record.status == "success":
            self.data.setdefault("completed_keys", {})[key] = record.timestamp
        self.data.setdefault("records", []).append(record.__dict__)
        self.data["meta"]["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.save()

    def save(self) -> None:
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        self.progress_path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download candidate affidavits from ECI portal")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("--headless", type=str, choices=["true", "false"], help="Run browser headless")
    parser.add_argument("--max-candidates", type=int, help="Maximum candidates to process")
    parser.add_argument("--resume", type=str, choices=["true", "false"], help="Resume from progress file")
    parser.add_argument("--dry-run", type=str, choices=["true", "false"], help="Only log candidate names without downloading")
    return parser.parse_args()


def str_to_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.lower() == "true"


def load_config(config_path: Path, args: argparse.Namespace) -> Dict[str, Any]:
    load_dotenv()
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    runtime = cfg["runtime"]
    runtime["headless"] = str_to_bool(args.headless, runtime["headless"])
    runtime["resume"] = str_to_bool(args.resume, runtime["resume"])
    runtime["dry_run"] = str_to_bool(args.dry_run, runtime["dry_run"])
    if args.max_candidates is not None:
        runtime["max_candidates"] = args.max_candidates

    output_base = os.path.expanduser(cfg["paths"]["output_base"])
    cfg["paths"]["output_base"] = output_base

    ua = cfg["browser"].get("user_agent", "")
    if ua == "${ECI_USER_AGENT}":
        cfg["browser"]["user_agent"] = os.getenv("ECI_USER_AGENT", "")

    return cfg


def setup_logging(logs_dir: Path) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("eci_automation")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    info_handler = logging.FileHandler(logs_dir / "info.log", encoding="utf-8")
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)

    error_handler = logging.FileHandler(logs_dir / "error.log", encoding="utf-8")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(info_handler)
    logger.addHandler(error_handler)
    logger.addHandler(console_handler)

    return logger


def sanitize_filename(raw_name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", raw_name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:180] if cleaned else "unknown"


def wait_random(runtime_cfg: Dict[str, Any]) -> None:
    time.sleep(random.uniform(runtime_cfg["throttle_min_seconds"], runtime_cfg["throttle_max_seconds"]))


def with_retry(logger: logging.Logger, retries: int, backoff: float, action_name: str, fn: Callable[[], Any]) -> Any:
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as err:  # noqa: BLE001
            last_err = err
            logger.warning("%s failed on attempt %s/%s: %s", action_name, attempt, retries, err)
            if attempt < retries:
                sleep_sec = backoff ** attempt
                time.sleep(sleep_sec)
    raise RuntimeError(f"Action failed after retries: {action_name}") from last_err


def click_first_visible(page: Page, selectors: List[str], timeout_ms: int) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count() > 0 and locator.is_visible():
            locator.click(timeout=timeout_ms)
            return True
    return False


def select_filter_option(page: Page, select_selector: str, value_text: str, timeout_ms: int) -> None:
    select = page.locator(select_selector).first
    select.wait_for(state="visible", timeout=timeout_ms)
    try:
        select.select_option(label=value_text, timeout=timeout_ms)
        return
    except Error:
        pass

    select.click(timeout=timeout_ms)
    option = page.get_by_role("option", name=re.compile(re.escape(value_text), re.I)).first
    if option.count() > 0:
        option.click(timeout=timeout_ms)
        return

    page.get_by_text(value_text, exact=False).first.click(timeout=timeout_ms)


def detect_human_verification(page: Page, captcha_indicators: List[str]) -> bool:
    for indicator in captcha_indicators:
        if page.locator(indicator).count() > 0:
            return True
    return False


def wait_for_manual_verification(page: Page, logger: logging.Logger, captcha_indicators: List[str]) -> None:
    if not detect_human_verification(page, captcha_indicators):
        return
    logger.warning("Human verification detected. Please solve it in the browser. Waiting to continue...")
    while detect_human_verification(page, captcha_indicators):
        page.wait_for_timeout(2000)


def apply_filters(page: Page, cfg: Dict[str, Any], logger: logging.Logger) -> None:
    selectors = cfg["selectors"]
    timeout_ms = cfg["runtime"]["timeout_ms"]

    logger.info("Applying filters: Election, Period, Type, State")
    select_filter_option(page, selectors["election_dropdown"], selectors["election_option"], timeout_ms)
    wait_random(cfg["runtime"])
    select_filter_option(page, selectors["period_dropdown"], selectors["period_option"], timeout_ms)
    wait_random(cfg["runtime"])
    select_filter_option(page, selectors["type_dropdown"], selectors["type_option"], timeout_ms)
    wait_random(cfg["runtime"])
    select_filter_option(page, selectors["state_dropdown"], selectors["state_option"], timeout_ms)

    if not click_first_visible(page, selectors["search_button_candidates"], timeout_ms):
        raise RuntimeError("Could not find Search/Apply button.")

    page.wait_for_timeout(2000)
    if not click_first_visible(page, selectors["accepted_tab_candidates"], timeout_ms):
        logger.warning("Accepted tab selector fallback failed; trying role-based fallback.")
        page.get_by_role("tab", name=re.compile("Accepted", re.I)).first.click(timeout=timeout_ms)

    page.wait_for_timeout(1500)


def get_candidate_rows(page: Page, cfg: Dict[str, Any]) -> Locator:
    for row_selector in cfg["selectors"]["candidate_row_candidates"]:
        rows = page.locator(row_selector)
        if rows.count() > 0:
            return rows
    return page.locator("table tbody tr")


def extract_text_with_fallback(row: Locator, candidate_selectors: List[str]) -> str:
    for selector in candidate_selectors:
        loc = row.locator(selector).first
        if loc.count() > 0:
            text = loc.inner_text().strip()
            if text:
                return text
    return ""


def open_candidate_detail(page: Page, row: Locator, cfg: Dict[str, Any], logger: logging.Logger) -> Page:
    timeout_ms = cfg["runtime"]["timeout_ms"]
    popup: Optional[Page] = None

    for button_selector in cfg["selectors"]["view_more_candidates"]:
        btn = row.locator(button_selector).first
        if btn.count() == 0:
            continue
        try:
            with page.expect_popup(timeout=timeout_ms) as popup_info:
                btn.click(timeout=timeout_ms)
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            return popup
        except TimeoutError:
            logger.warning("No popup for selector %s; trying direct navigation.", button_selector)
            try:
                btn.click(timeout=timeout_ms)
                page.wait_for_timeout(1200)
                return page
            except Exception:  # noqa: BLE001
                continue

    raise RuntimeError("Unable to open candidate detail page.")


def download_affidavit(detail_page: Page, target_path: Path, cfg: Dict[str, Any], logger: logging.Logger) -> Path:
    timeout_ms = cfg["runtime"]["timeout_ms"]

    for dl_selector in cfg["selectors"]["download_button_candidates"]:
        button = detail_page.locator(dl_selector).first
        if button.count() == 0:
            continue
        with detail_page.expect_download(timeout=timeout_ms) as download_info:
            button.click(timeout=timeout_ms)
        download = download_info.value
        target_path.parent.mkdir(parents=True, exist_ok=True)
        download.save_as(str(target_path))

        if target_path.exists() and target_path.stat().st_size > 0:
            return target_path
        raise RuntimeError("Downloaded file missing or empty.")

    raise RuntimeError("Download button not found in candidate detail page.")


def click_next_page(page: Page, cfg: Dict[str, Any]) -> bool:
    timeout_ms = cfg["runtime"]["timeout_ms"]
    for nxt in cfg["selectors"]["next_page_candidates"]:
        loc = page.locator(nxt).first
        if loc.count() > 0 and loc.is_visible():
            disabled = loc.get_attribute("disabled")
            aria_disabled = loc.get_attribute("aria-disabled")
            if disabled is not None or aria_disabled == "true":
                return False
            loc.click(timeout=timeout_ms)
            page.wait_for_timeout(1200)
            return True
    return False


def process_candidates(page: Page, cfg: Dict[str, Any], progress: ProgressStore, logger: logging.Logger) -> Dict[str, int]:
    max_candidates = cfg["runtime"]["max_candidates"]
    dry_run = cfg["runtime"]["dry_run"]
    retries = cfg["runtime"]["retries"]
    backoff = cfg["runtime"]["retry_backoff_seconds"]

    stats = {"total_seen": 0, "success": 0, "failed": 0, "skipped": 0}
    processed = 0

    while not STOP_REQUESTED and processed < max_candidates:
        rows = get_candidate_rows(page, cfg)
        row_count = rows.count()
        if row_count == 0:
            logger.info("No candidate rows found on current page.")
            break

        for idx in range(row_count):
            if STOP_REQUESTED or processed >= max_candidates:
                break

            row = rows.nth(idx)
            candidate_name = extract_text_with_fallback(row, cfg["selectors"]["candidate_name_candidates"]) or f"candidate_{processed + 1}"
            constituency_name = extract_text_with_fallback(row, cfg["selectors"]["constituency_name_candidates"]) or "unknown_constituency"
            stats["total_seen"] += 1

            if progress.is_completed(candidate_name, constituency_name):
                stats["skipped"] += 1
                logger.info("Skipping (already completed): %s | %s", candidate_name, constituency_name)
                continue

            candidate_url = ""
            timestamp = datetime.now(timezone.utc).isoformat()

            if dry_run:
                logger.info("[DRY RUN] Candidate: %s | Constituency: %s", candidate_name, constituency_name)
                progress.add_record(
                    CandidateRecord(
                        candidate_name=candidate_name,
                        constituency_name=constituency_name,
                        candidate_url=candidate_url,
                        status="skipped",
                        saved_path="",
                        error_message="dry_run",
                        timestamp=timestamp,
                    )
                )
                stats["skipped"] += 1
                processed += 1
                continue

            constituency_dir = Path(cfg["paths"]["output_base"]) / sanitize_filename(constituency_name)
            final_path = constituency_dir / f"{sanitize_filename(candidate_name)}.pdf"

            if final_path.exists() and final_path.stat().st_size > 0:
                logger.info("Skipping (file already exists): %s", final_path)
                progress.add_record(
                    CandidateRecord(
                        candidate_name=candidate_name,
                        constituency_name=constituency_name,
                        candidate_url=candidate_url,
                        status="skipped",
                        saved_path=str(final_path),
                        error_message="already_exists",
                        timestamp=timestamp,
                    )
                )
                stats["skipped"] += 1
                processed += 1
                continue

            try:
                def _run_download() -> Path:
                    detail_page = open_candidate_detail(page, row, cfg, logger)
                    nonlocal candidate_url
                    candidate_url = detail_page.url
                    try:
                        downloaded_path = download_affidavit(detail_page, final_path, cfg, logger)
                        return downloaded_path
                    finally:
                        if detail_page != page:
                            detail_page.close()

                saved = with_retry(logger, retries, backoff, f"download {candidate_name}", _run_download)
                progress.add_record(
                    CandidateRecord(
                        candidate_name=candidate_name,
                        constituency_name=constituency_name,
                        candidate_url=candidate_url,
                        status="success",
                        saved_path=str(saved),
                        error_message="",
                        timestamp=timestamp,
                    )
                )
                stats["success"] += 1
                logger.info("Downloaded: %s -> %s", candidate_name, saved)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed candidate %s | %s", candidate_name, constituency_name)
                progress.add_record(
                    CandidateRecord(
                        candidate_name=candidate_name,
                        constituency_name=constituency_name,
                        candidate_url=candidate_url,
                        status="failed",
                        saved_path=str(final_path),
                        error_message=str(exc),
                        timestamp=timestamp,
                    )
                )
                stats["failed"] += 1

            processed += 1
            wait_random(cfg["runtime"])

        if STOP_REQUESTED or processed >= max_candidates:
            break

        if not click_next_page(page, cfg):
            logger.info("No more pages available in candidate list.")
            break

    return stats


def create_browser_context(browser: Browser, cfg: Dict[str, Any]) -> BrowserContext:
    context_options: Dict[str, Any] = {
        "accept_downloads": True,
        "viewport": cfg["browser"]["viewport"],
    }
    if cfg["browser"].get("user_agent"):
        context_options["user_agent"] = cfg["browser"]["user_agent"]
    return browser.new_context(**context_options)


def main() -> int:
    args = parse_args()
    cfg = load_config(Path(args.config), args)

    logger = setup_logging(Path(cfg["paths"]["logs_dir"]))
    setup_signal_handlers()

    progress_path = Path(cfg["paths"]["progress_file"])
    if not cfg["runtime"]["resume"] and progress_path.exists():
        progress_path.unlink()
    progress = ProgressStore(progress_path)

    start = time.time()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=cfg["runtime"]["headless"])
            context = create_browser_context(browser, cfg)
            page = context.new_page()

            page.set_default_timeout(cfg["runtime"]["timeout_ms"])
            page.set_default_navigation_timeout(cfg["runtime"]["navigation_timeout_ms"])

            logger.info("Opening portal: %s", cfg["portal"]["url"])
            page.goto(cfg["portal"]["url"], wait_until="domcontentloaded")

            wait_for_manual_verification(page, logger, cfg["selectors"]["captcha_indicators"])

            with_retry(
                logger,
                cfg["runtime"]["retries"],
                cfg["runtime"]["retry_backoff_seconds"],
                "apply filters",
                lambda: apply_filters(page, cfg, logger),
            )

            stats = process_candidates(page, cfg, progress, logger)

            context.close()
            browser.close()

    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt received. Progress has been persisted.")
        return 130
    except Exception:
        logger.exception("Fatal automation error.")
        return 1
    finally:
        progress.save()

    elapsed = round(time.time() - start, 2)
    logger.info(
        "Summary | total_seen=%s success=%s failed=%s skipped=%s runtime_sec=%s",
        stats["total_seen"],
        stats["success"],
        stats["failed"],
        stats["skipped"],
        elapsed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
