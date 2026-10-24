"""REST API wrapper around scraper.py.

Exposes the keyword search as an HTTP service so other programs can ask for
links without knowing anything about Selenium:

    GET /search?keyword=gym&limit=5&type=reels

Run it with:
    uvicorn grab_in_server:app --host 0.0.0.0 --port 8000
"""
import json
import os
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query

import scraper

CREDENTIALS_FILE = Path(os.getenv('CREDENTIALS_FILE', '/app/credentials.json'))
MAX_LIMIT = int(os.getenv('MAX_LIMIT', '25'))
QUEUE_WAIT_SECONDS = int(os.getenv('QUEUE_WAIT_SECONDS', '900'))

app = FastAPI(
    title='Instagram keyword scraper',
    description='Search Instagram for a keyword and get back post/reel links.',
    version='1.0.0',
)

# Selenium drives one real Chrome profile, so only one search may run at a time.
# Requests queue on this lock instead of corrupting each other's session.
_run_lock = threading.Lock()


def load_credentials():
    """Read username/password/two_factor_code from the shared credentials file."""
    if not CREDENTIALS_FILE.exists():
        raise HTTPException(500, f'Credentials file not found: {CREDENTIALS_FILE}')
    try:
        data = json.loads(CREDENTIALS_FILE.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, f'Credentials file {CREDENTIALS_FILE} is not valid JSON: {exc}')

    username = str(data.get('username') or os.getenv('IG_USERNAME') or '').strip()
    password = str(data.get('password') or os.getenv('IG_PASSWORD') or '')
    if not username or not password:
        raise HTTPException(
            500, f'Fill in "username" and "password" in {CREDENTIALS_FILE} before using the API.'
        )
    return username, password, str(data.get('two_factor_code') or '').strip()


def fetch_links(keyword, limit, result_type, save_csv=False):
    """Run the full scraper flow for one keyword and return the links."""
    username, password, code = load_credentials()

    if not _run_lock.acquire(timeout=QUEUE_WAIT_SECONDS):
        raise HTTPException(503, 'Another search is already running; try again shortly.')
    driver = None
    try:
        driver = scraper.make_driver()
        scraper.login(driver, username, password, code)
        links = scraper.search(driver, keyword, limit, result_type)
        if links and save_csv:
            scraper.write_results(keyword, links)
        return links
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001 - teardown must never mask the real error
                pass
        _run_lock.release()


@app.get('/health')
def health():
    """Liveness probe: says nothing about Instagram, only that the API is up."""
    return {
        'status': 'ok',
        'credentials_file': str(CREDENTIALS_FILE),
        'credentials_present': CREDENTIALS_FILE.exists(),
        'busy': _run_lock.locked(),
        'max_limit': MAX_LIMIT,
    }


@app.get('/search')
def search(
    keyword: str = Query(..., min_length=1, max_length=100,
                          description='keyword typed into the Instagram search menu'),
    limit: int = Query(5, ge=1, le=MAX_LIMIT, description='how many links to return'),
    result_type: Literal['reels', 'posts', 'both'] = Query('both', alias='type',
                                                           description='which links to return; reels come first'),
    save_csv: bool = Query(False, description='also write the run to output/*.csv'),
):
    """Search a keyword and return the top N post/reel links as JSON."""
    started = time.time()
    scraper.log(f'[api] search keyword="{keyword}" limit={limit} type={result_type}')

    try:
        links = fetch_links(keyword, limit, result_type, save_csv=save_csv)
    except HTTPException:
        raise
    except RuntimeError as exc:
        # e.g. Instagram served a challenge or login was rejected
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - report any driver failure as a gateway error
        raise HTTPException(502, f'{type(exc).__name__}: {exc}') from exc

    if not links:
        raise HTTPException(404, f'No links found for "{keyword}".')

    elapsed = round(time.time() - started, 2)
    scraper.log(f'[api] returning {len(links)} link(s) in {elapsed}s')
    return {
        'keyword': keyword,
        'limit': limit,
        'type': result_type,
        'count': len(links),
        'links': links,
        'elapsed_seconds': elapsed,
    }
