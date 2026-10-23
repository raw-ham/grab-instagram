"""Collect visible Instagram hashtag post links in a headless Docker browser."""
import csv
import getpass
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

OUTPUT = Path('/app/output')
LOGIN_URL = 'https://www.instagram.com/accounts/login/'
USERNAME_SELECTORS = [
    (By.NAME, 'username'),
    (By.CSS_SELECTOR, 'input[autocomplete="username"]'),
    (By.CSS_SELECTOR, 'input[aria-label*="username" i]'),
    (By.CSS_SELECTOR, 'input[placeholder*="username" i]'),
]
PASSWORD_SELECTORS = [
    (By.NAME, 'password'),
    (By.CSS_SELECTOR, 'input[autocomplete="current-password"]'),
    (By.CSS_SELECTOR, 'input[type="password"]'),
]


def make_driver():
    options = Options()
    options.binary_location = '/usr/bin/chromium'
    for arg in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
                '--window-size=1440,1000'):
        options.add_argument(arg)
    driver = webdriver.Chrome(service=Service('/usr/bin/chromedriver'), options=options)
    driver.set_page_load_timeout(45)
    return driver


def find_visible(driver, selectors, timeout=35):
    def lookup(d):
        for by, selector in selectors:
            for element in d.find_elements(by, selector):
                try:
                    if element.is_displayed() and element.is_enabled():
                        return element
                except WebDriverException:
                    pass
        return False
    return WebDriverWait(driver, timeout, poll_frequency=0.5).until(lookup)


def debug(driver, label):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    driver.save_screenshot(str(OUTPUT / f'{label}.png'))
    (OUTPUT / f'{label}.html').write_text(driver.page_source, encoding='utf-8')
    print(f'Debug: {driver.current_url} | {driver.title}')
    print(f'Saved output/{label}.png and output/{label}.html (may contain private data).')


def authenticated(driver):
    path = urlparse(driver.current_url).path
    if any(s in path for s in ('/accounts/login', '/challenge/', '/two_factor/', '/accounts/onetap/')):
        return False
    # Login UI can change: require an authenticated navigation control, not URL alone.
    selectors = [
        'a[href="/direct/inbox/"]',
        'a[href="/accounts/edit/"]',
        'a[href="/explore/"]',
    ]
    return any(driver.find_elements(By.CSS_SELECTOR, selector) for selector in selectors)


def login(driver, username, password):
    print('Opening Instagram login page...')
    driver.get(LOGIN_URL)
    try:
        user_field = find_visible(driver, USERNAME_SELECTORS)
        pass_field = find_visible(driver, PASSWORD_SELECTORS)
        user_field.clear()
        user_field.send_keys(username)
        pass_field.send_keys(password)
        submit = find_visible(driver, [
            (By.CSS_SELECTOR, 'button[type="submit"]'),
            (By.XPATH, '//form//button[not(@disabled)]'),
        ], timeout=15)
        submit.click()
    except TimeoutException as exc:
        debug(driver, 'login_fields_missing')
        raise RuntimeError('Login form unavailable; inspect debug screenshot.') from exc

    try:
        WebDriverWait(driver, 30, poll_frequency=1).until(authenticated)
    except TimeoutException as exc:
        debug(driver, 'login_not_confirmed')
        raise RuntimeError('Login not confirmed. Verification, consent, or access restriction may be present.') from exc
    print('Authenticated navigation detected.')


def scrape(driver, keyword, limit):
    tag = re.sub(r'\s+', '', keyword.strip().lstrip('#'))
    if not tag or '/' in tag or '?' in tag:
        raise ValueError('Enter a valid single hashtag (e.g. photography).')
    url = f'https://www.instagram.com/explore/tags/{quote(tag, safe="")}/'
    print('Opening', url)
    driver.get(url)
    found = []
    seen = set()
    stalled = 0
    last_count = -1
    for attempt in range(30):
        try:
            WebDriverWait(driver, 8).until(lambda d: d.find_elements(
                By.CSS_SELECTOR, 'a[href*="/p/"], a[href*="/reel/"]'))
        except TimeoutException:
            pass
        for link in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/p/"], a[href*="/reel/"]'):
            href = link.get_attribute('href') or ''
            match = re.match(r'^https://www\.instagram\.com/(p|reel)/([A-Za-z0-9_-]+)/?', href)
            if match:
                canonical = f'https://www.instagram.com/{match.group(1)}/{match.group(2)}/'
                if canonical not in seen:
                    seen.add(canonical)
                    found.append(canonical)
                    if len(found) >= limit:
                        return found
        print(f'Scroll {attempt + 1}: {len(found)} links')
        stalled = stalled + 1 if len(found) == last_count else 0
        if stalled >= 5:
            break
        last_count = len(found)
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight)')
        time.sleep(2)
    if not found:
        debug(driver, 'no_posts_found')
    return found


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    username = input('Instagram username: ').strip()
    password = getpass.getpass('Instagram password: ')
    keyword = input('Hashtag to search: ').strip()
    try:
        limit = int(input('Number of posts: '))
    except ValueError as exc:
        raise SystemExit('Number of posts must be an integer.') from exc
    if not username or not password or not keyword or limit < 1:
        raise SystemExit('Username, password, hashtag and a positive limit are required.')
    driver = make_driver()
    try:
        login(driver, username, password)
        links = scrape(driver, keyword, limit)
        output_file = OUTPUT / 'instagram_posts.csv'
        with output_file.open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['post_url'])
            writer.writerows([[link] for link in links])
        print(f'Wrote {len(links)} links to {output_file}')
        for link in links:
            print(link)
    finally:
        driver.quit()


if __name__ == '__main__':
    main()
