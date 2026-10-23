"""Instagram keyword search scraper.

Logs in with credentials from a JSON file, uses the in-app search menu for a
keyword, then returns the top N post / reel links. Designed to run headless
inside Docker.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

OUTPUT_DIR = Path(os.getenv('OUTPUT_DIR', '/app/output'))
DEBUG_DIR = OUTPUT_DIR / 'debug'
CREDENTIALS_FILE = Path(os.getenv('CREDENTIALS_FILE', '/app/credentials.json'))
PROFILE_DIR = Path(os.getenv('CHROME_PROFILE_DIR', '/app/data/chrome-profile'))
CODE_FILE = Path(os.getenv('CODE_FILE', '/app/data/login_code.txt'))
CODE_WAIT_SECONDS = int(os.getenv('CODE_WAIT_SECONDS', '600'))
CHROMIUM_BINARY = os.getenv('CHROMIUM_BINARY', '/usr/bin/chromium')
CHROMEDRIVER_PATH = os.getenv('CHROMEDRIVER_PATH', '/usr/bin/chromedriver')

LOGIN_URL = 'https://www.instagram.com/accounts/login/'
HOME_URL = 'https://www.instagram.com/'
SEARCH_URL = 'https://www.instagram.com/explore/search/'

USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/131.0.0.0 Safari/537.36'
)

# Instagram's login form currently uses name="email" for the identifier and
# name="pass" for the password; older/alternate layouts use name="username".
USERNAME_SELECTORS = [
    (By.CSS_SELECTOR, 'input[name="email"]'),
    (By.CSS_SELECTOR, 'input[name="username"]'),
    (By.CSS_SELECTOR, 'input[autocomplete^="username"]'),
    (By.CSS_SELECTOR, 'input[aria-label*="username" i]'),
    (By.CSS_SELECTOR, 'input[placeholder*="username" i]'),
]
PASSWORD_SELECTORS = [
    (By.CSS_SELECTOR, 'input[name="pass"]'),
    (By.CSS_SELECTOR, 'input[name="password"]'),
    (By.CSS_SELECTOR, 'input[type="password"]'),
]
LOGIN_INPUT_SELECTORS = USERNAME_SELECTORS + PASSWORD_SELECTORS
SEARCH_INPUT_SELECTORS = [
    (By.CSS_SELECTOR, 'input[aria-label="Search input"]'),
    (By.CSS_SELECTOR, 'input[placeholder="Search"]'),
    (By.CSS_SELECTOR, 'input[type="search"]'),
    (By.CSS_SELECTOR, 'input[type="text"]'),
]
POST_LINK_SELECTOR = 'a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]'
POST_URL_RE = re.compile(r'^https://www\.instagram\.com/(p|reel|tv)/([A-Za-z0-9_-]+)')
BLOCK_URL_MARKERS = ('/challenge/', '/two_factor/', '/accounts/login', '/accounts/suspended')
TYPE_MATCH = {'posts': {'p'}, 'reels': {'reel'}, 'both': {'p', 'reel', 'tv'}}


def log(message):
    print(message, flush=True)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def load_settings():
    parser = argparse.ArgumentParser(description='Instagram keyword search scraper.')
    parser.add_argument('-k', '--keyword', help='keyword to search in the search menu')
    parser.add_argument('-n', '--limit', type=int, help='number of links to return')
    parser.add_argument('--type', dest='result_type', choices=sorted(TYPE_MATCH),
                        help='posts, reels or both (the keyword page lists reel '
                             'links only when Instagram returns them)')
    parser.add_argument('--credentials', default=str(CREDENTIALS_FILE),
                        help='path to the credentials JSON file')
    parser.add_argument('--login-only', action='store_true',
                        help='only verify login, do not run a search')
    parser.add_argument('--code', help='security code Instagram emails/texts during login')
    args = parser.parse_args()

    creds_path = Path(args.credentials)
    if not creds_path.exists():
        raise SystemExit(
            f'Credentials file not found: {creds_path}\n'
            'Create it with {"username": "...", "password": "..."} and retry.'
        )
    try:
        data = json.loads(creds_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise SystemExit(f'Credentials file {creds_path} is not valid JSON: {exc}') from exc
    if not isinstance(data, dict):
        raise SystemExit(f'Credentials file {creds_path} must contain a JSON object.')

    def pick(key, default=None):
        value = args.__dict__.get(key) or data.get(key) or default
        return value

    settings = {
        'username': str(data.get('username') or os.getenv('IG_USERNAME') or '').strip(),
        'password': str(data.get('password') or os.getenv('IG_PASSWORD') or ''),
        'keyword': str(pick('keyword', '') or '').strip(),
        'limit': int(pick('limit', 10) or 10),
        'result_type': str(pick('result_type', 'both') or 'both').lower(),
        'code': str(args.code or data.get('two_factor_code') or '').strip(),
        'login_only': args.login_only,
    }
    if settings['result_type'] not in TYPE_MATCH:
        raise SystemExit(f"result_type must be one of {sorted(TYPE_MATCH)}.")
    if not settings['username'] or not settings['password']:
        raise SystemExit(
            f'Fill in "username" and "password" in {creds_path} before running.'
        )
    if not settings['login_only']:
        if not settings['keyword']:
            raise SystemExit('No keyword provided (use --keyword or set "keyword" in the file).')
        if settings['limit'] < 1:
            raise SystemExit('limit must be a positive integer.')
    return settings


# --------------------------------------------------------------------------- #
# browser plumbing
# --------------------------------------------------------------------------- #
def make_driver():
    options = Options()
    options.binary_location = CHROMIUM_BINARY
    for arg in (
        '--headless=new',
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--window-size=1440,1000',
        '--lang=en-US',
        '--disable-extensions',
        '--blink-settings=imagesEnabled=false',
        f'--user-agent={USER_AGENT}',
        '--disable-blink-features=AutomationControlled',
    ):
        options.add_argument(arg)
    options.add_experimental_option('excludeSwitches', ['enable-automation'])
    options.add_experimental_option('useAutomationExtension', False)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    options.add_argument(f'--user-data-dir={PROFILE_DIR}')

    driver = webdriver.Chrome(service=Service(CHROMEDRIVER_PATH), options=options)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(30)
    try:
        driver.execute_cdp_cmd(
            'Page.addScriptToEvaluateOnNewDocument',
            {'source': 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'},
        )
    except WebDriverException:
        pass
    return driver


def safe_get(driver, url, settle=3):
    """Navigate, tolerating renderer timeouts: heavy IG pages can hang a load."""
    try:
        driver.get(url)
    except TimeoutException:
        log(f'[warn] page load timed out for {url}; stopping the load and continuing.')
        try:
            driver.execute_script('window.stop();')
        except WebDriverException:
            pass
    time.sleep(settle)


def visible_elements(driver, by, selector):
    for element in driver.find_elements(by, selector):
        try:
            if element.is_displayed() and element.is_enabled():
                return element
        except WebDriverException:
            continue
    return None


def find_first(driver, selectors, timeout=25, root=None):
    """Return the first visible+enabled element from several (by, selector) pairs."""
    scope = root or driver

    def lookup(_):
        for by, selector in selectors:
            element = visible_elements(scope, by, selector)
            if element is not None:
                return element
        return False

    try:
        return WebDriverWait(driver, timeout, poll_frequency=0.4).until(lookup)
    except TimeoutException:
        return None


def click_text(driver, texts, timeout=6):
    """Click a button / role=button / link whose visible text matches exactly."""
    xpaths = []
    for text in texts:
        xpaths += [
            f'//button[normalize-space()="{text}"]',
            f'//*[@role="button"][normalize-space()="{text}"]',
            f'//a[normalize-space()="{text}"]',
            f'//button[.//span[normalize-space()="{text}"]]',
            f'//div[@role="button"][.//span[normalize-space()="{text}"]]',
        ]
    deadline = time.time() + timeout
    while time.time() < deadline:
        for xpath in xpaths:
            for element in driver.find_elements(By.XPATH, xpath):
                try:
                    if element.is_displayed():
                        driver.execute_script('arguments[0].click();', element)
                        return True
                except (WebDriverException, StaleElementReferenceException):
                    continue
        time.sleep(0.4)
    return False


def snapshot(driver, label):
    """Save a screenshot + page source so failures can be inspected."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        driver.save_screenshot(str(DEBUG_DIR / f'{label}.png'))
        (DEBUG_DIR / f'{label}.html').write_text(driver.page_source, encoding='utf-8')
        log(f'[debug] saved {DEBUG_DIR.name}/{label}.png and .html ({driver.current_url})')
    except WebDriverException as exc:
        log(f'[debug] could not save {label}: {exc}')


def dismiss_dialogs(driver, rounds=3):
    """Close cookie / notification / save-login popups that block interactions."""
    labels = [
        'Allow all cookies', 'Accept all', 'Accept', 'Only allow essential cookies',
        'Not now', 'Not Now', 'Later', 'Cancel', 'Dismiss', 'Close', 'Save info',
    ]
    for _ in range(rounds):
        clicked = click_text(driver, labels, timeout=3)
        if not clicked:
            return
        time.sleep(1.2)


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #
def is_authenticated(driver):
    url = driver.current_url
    if any(marker in url for marker in BLOCK_URL_MARKERS):
        return False
    # A visible login form means we are still logged out.
    for by, selector in LOGIN_INPUT_SELECTORS:
        if visible_elements(driver, by, selector) is not None:
            return False
    indicators = [
        (By.CSS_SELECTOR, 'a[href="/direct/inbox/"]'),
        (By.CSS_SELECTOR, 'a[href="/accounts/edit/"]'),
        (By.CSS_SELECTOR, 'a[href="/explore/search/"]'),
        (By.CSS_SELECTOR, 'svg[aria-label="Home"]'),
        (By.CSS_SELECTOR, 'svg[aria-label="Search"]'),
    ]
    return any(visible_elements(driver, by, sel) is not None for by, sel in indicators)


def assert_not_blocked(driver, stage):
    url = driver.current_url
    for marker, reason in (
        ('/challenge/', 'Instagram served a security challenge'),
        ('/two_factor/', 'two-factor authentication is required'),
        ('/accounts/suspended', 'the account is suspended'),
    ):
        if marker in url:
            snapshot(driver, f'blocked_{stage}')
            raise RuntimeError(f'{reason}; complete it manually and retry.')


LOGIN_ERRORS = (
    ('login information you entered is incorrect',
     'Instagram rejected the saved credentials (wrong username or password).'),
    ('Sorry, your password was incorrect',
     'Instagram reports an incorrect password.'),
    ('Please wait a few minutes',
     'Instagram is rate-limiting login attempts from this IP; wait and retry.'),
    ('suspicious login attempt',
     'Instagram flagged this as a suspicious login attempt; open the account and approve it.'),
    ('code you entered is incorrect',
     'Instagram rejected the security code; it may have expired.'),
    ('code is incorrect',
     'Instagram rejected the security code; it may have expired.'),
    ('invalid code',
     'Instagram rejected the security code as invalid.'),
    ('not a valid code',
     'Instagram rejected the security code as invalid.'),
)


def detect_login_error(driver):
    for needle, message in LOGIN_ERRORS:
        try:
            if driver.find_elements(By.XPATH, f'//*[contains(., "{needle}")]'):
                return message
        except WebDriverException:
            return None
    return None


def on_code_entry_page(driver):
    url = driver.current_url
    if 'codeentry' in url or '/auth_platform/' in url:
        return True
    try:
        return bool(driver.find_elements(By.XPATH, '//*[contains(., "Enter the code we sent")]'))
    except WebDriverException:
        return False


def await_security_code(provided):
    """Return a security code: from the CLI flag, or from a file we poll."""
    if provided:
        log('Using the security code passed on the command line.')
        return provided
    log(f'Instagram wants a security code. Write it into {CODE_FILE} (host: data/login_code.txt)')
    log(f'or re-run with --code <code>. Waiting up to {CODE_WAIT_SECONDS}s...')
    deadline = time.time() + CODE_WAIT_SECONDS
    while time.time() < deadline:
        if CODE_FILE.exists():
            code = CODE_FILE.read_text(encoding='utf-8').strip()
            if code:
                CODE_FILE.unlink()
                log('Security code picked up.')
                return code
        time.sleep(3)
    raise RuntimeError(
        f'Timed out after {CODE_WAIT_SECONDS}s waiting for the security code in {CODE_FILE}.'
    )


def submit_security_code(driver, code):
    field = find_first(driver, [
        (By.CSS_SELECTOR, 'input[inputmode="numeric"]'),
        (By.CSS_SELECTOR, 'input[name="email"][type="text"]'),
        (By.CSS_SELECTOR, 'input[type="text"]'),
    ], timeout=20)
    if field is None:
        snapshot(driver, 'code_field_missing')
        raise RuntimeError('Security code input not found; see output/debug/code_field_missing.png.')
    try:
        field.click()
    except WebDriverException:
        pass
    field.clear()
    field.send_keys(code)
    time.sleep(0.5)
    log(f'Typed {len(field.get_attribute("value") or "")} character(s) into the code field.')
    clicked = click_text(driver, ['Continue', 'Next', 'Confirm', 'Submit', 'Log in'], timeout=6)
    if not clicked:
        field.send_keys(Keys.ENTER)
    log(f'Submitted the security code (button clicked: {clicked}).')


def login(driver, username, password, code=''):
    log('Opening Instagram home page...')
    safe_get(driver, HOME_URL)
    dismiss_dialogs(driver)
    if is_authenticated(driver):
        log('Existing session found in the browser profile; skipping login form.')
        return

    log('Logging in with saved credentials...')
    safe_get(driver, LOGIN_URL, settle=2)
    username_field = find_first(driver, USERNAME_SELECTORS, timeout=35)
    if username_field is None:
        snapshot(driver, 'login_form_missing')
        raise RuntimeError('Login form not found; check output/debug for details.')
    username_field.clear()
    username_field.send_keys(username)

    password_field = find_first(driver, PASSWORD_SELECTORS, timeout=20)
    if password_field is None:
        snapshot(driver, 'login_password_missing')
        raise RuntimeError('Password field not found; check output/debug for details.')
    password_field.clear()
    password_field.send_keys(password)

    # The Log in control is a div[role="button"] on the current layout.
    submit = find_first(driver, [
        (By.CSS_SELECTOR, 'button[type="submit"]'),
        (By.XPATH, '//*[@role="button"][normalize-space()="Log in"]'),
        (By.XPATH, '//form//*[@role="button"]'),
    ], timeout=15)
    submitted = False
    if submit is not None:
        try:
            submit.click()
            submitted = True
        except (ElementClickInterceptedException, WebDriverException):
            try:
                driver.execute_script('arguments[0].click();', submit)
                submitted = True
            except WebDriverException:
                submitted = False
    if not submitted:
        log('[warn] Log in control not found; submitting the form with Enter.')
        password_field.send_keys(Keys.ENTER)

    deadline = time.time() + 45
    code_attempts = 0
    while time.time() < deadline:
        dismiss_dialogs(driver, rounds=1)
        assert_not_blocked(driver, 'login')
        if is_authenticated(driver):
            log('Login confirmed.')
            dismiss_dialogs(driver)
            return
        if on_code_entry_page(driver):
            if code_attempts >= 2:
                snapshot(driver, 'code_rejected')
                raise RuntimeError('The security code was rejected twice; re-run for a new code.')
            code_attempts += 1
            snapshot(driver, f'code_entry_{code_attempts}')
            log('Instagram sent a security code to the account email/phone.')
            submit_security_code(driver, await_security_code(code))
            time.sleep(6)
            error = detect_login_error(driver)
            if error:
                snapshot(driver, 'code_rejected')
                raise RuntimeError(f'{error} See output/debug/code_rejected.png.')
            deadline = time.time() + 90
            continue
        error = detect_login_error(driver)
        if error:
            snapshot(driver, 'login_rejected')
            raise RuntimeError(f'{error} See output/debug/login_rejected.png.')
        time.sleep(2)

    snapshot(driver, 'login_not_confirmed')
    raise RuntimeError(
        'Login was not confirmed. Instagram may be asking for a code, captcha or '
        'verification - see output/debug/login_not_confirmed.png.'
    )


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
def open_search_results(driver, keyword):
    """Type the keyword into the search menu, then open its results page.

    In the headless layout the dropdown only lists matching accounts and Enter
    does not navigate, so after typing we open the keyword results page the same
    search menu serves for that keyword.
    """
    log('Opening the search menu...')
    safe_get(driver, SEARCH_URL)
    dismiss_dialogs(driver)

    search_input = find_first(driver, SEARCH_INPUT_SELECTORS, timeout=20)
    if search_input is None:
        log('[warn] Search input not found; using the keyword results URL directly.')
    else:
        try:
            search_input.click()
        except (ElementClickInterceptedException, WebDriverException):
            driver.execute_script('arguments[0].focus(); arguments[0].click();', search_input)
        search_input.clear()
        for chunk in keyword:
            search_input.send_keys(chunk)
            time.sleep(0.05)
        log(f'Typed "{keyword}" into the search box.')
        time.sleep(4)

        # Layouts that expose a keyword row in the dropdown: use it when present.
        for anchor in driver.find_elements(By.CSS_SELECTOR, 'a[href*="explore/search/keyword"]'):
            try:
                if anchor.is_displayed():
                    driver.execute_script('arguments[0].click();', anchor)
                    log('Opened the keyword row from the search dropdown.')
                    time.sleep(4)
                    break
            except WebDriverException:
                continue

        if 'explore/search/keyword' not in driver.current_url:
            search_input.send_keys(Keys.ENTER)
            log('Submitted the search box with Enter.')
            time.sleep(6)

    if 'explore/search/keyword' not in driver.current_url:
        log('Opening the keyword results page for the typed search.')
        safe_get(driver, f'{SEARCH_URL}keyword/?q={quote(keyword)}', settle=5)

    log(f'Search results page: {driver.current_url}')


def collect_links(driver, keyword, limit, result_type, max_scrolls=20):
    wanted = TYPE_MATCH[result_type]
    links, seen = [], set()
    stalls = 0

    for scroll in range(max_scrolls):
        for element in driver.find_elements(By.CSS_SELECTOR, POST_LINK_SELECTOR):
            try:
                href = element.get_attribute('href') or ''
            except StaleElementReferenceException:
                continue
            match = POST_URL_RE.match(href)
            if not match:
                continue
            kind, shortcode = match.group(1), match.group(2)
            if kind not in wanted:
                continue
            canonical = f'https://www.instagram.com/{kind}/{shortcode}/'
            if canonical not in seen:
                seen.add(canonical)
                links.append(canonical)
                if len(links) >= limit:
                    return links
        log(f'  scroll {scroll + 1}: {len(links)} link(s) collected')
        before = len(links)
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(2.5)
        if len(links) == before:
            stalls += 1
            for _ in range(2):
                driver.execute_script('window.scrollBy(0, 1200);')
                time.sleep(1.2)
            if stalls >= 4:
                log('  no new links after several scrolls; stopping.')
                break
        else:
            stalls = 0
    return links


def search(driver, keyword, limit, result_type):
    open_search_results(driver, keyword)
    assert_not_blocked(driver, 'search')
    log(f'Collecting up to {limit} {result_type} link(s) for "{keyword}"...')
    links = collect_links(driver, keyword, limit, result_type)
    if not links:
        snapshot(driver, 'no_links_found')
    return links


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def write_results(keyword, links):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^A-Za-z0-9]+', '_', keyword).strip('_').lower() or 'keyword'
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = OUTPUT_DIR / f'results_{slug}_{stamp}.csv'

    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['rank', 'keyword', 'url'])
        for rank, link in enumerate(links, start=1):
            writer.writerow([rank, keyword, link])
    (OUTPUT_DIR / 'latest.csv').write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
    return path


def main():
    settings = load_settings()
    driver = make_driver()
    try:
        login(driver, settings['username'], settings['password'], settings['code'])
        if settings['login_only']:
            log('Login-only run finished successfully.')
            return 0
        links = search(driver, settings['keyword'], settings['limit'], settings['result_type'])
        if not links:
            log('No links were found for this keyword.')
            return 2
        path = write_results(settings['keyword'], links)
        log('')
        log(f'Found {len(links)} link(s) for "{settings["keyword"]}":')
        for rank, link in enumerate(links, start=1):
            log(f'{rank}. {link}')
        log('')
        log(f'Saved to {path}')
        return 0
    finally:
        driver.quit()


if __name__ == '__main__':
    sys.exit(main())
