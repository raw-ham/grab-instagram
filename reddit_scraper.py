"""Reddit keyword search scraper plus REST API service.

Reddit refuses its public ``*.json`` endpoints from this network (403/503), so
this module reads the same server-rendered HTML that reddit.com hands to a
logged-out browser:

* ``/search/?q=<keyword>``                       -> the result listing
* ``/r/<sub>/comments/<id>/<slug>/``             -> post body, score, ratio
* ``/svc/shreddit/comments/r/<sub>/<id>``        -> the comment tree

Plain HTML pages are guarded by a small "Prove your humanity" JS challenge.
That challenge is deterministic - the inline script simply repeats a literal
token twice and submits it - so it is cleared here over plain HTTP. No browser
and no Reddit account are required.

Run the API with::

    uvicorn reddit_scraper:app --host 0.0.0.0 --port 8002

or use the CLI::

    python reddit_scraper.py -k python -n 5 -m 10 --sort hot
"""

import argparse
import http.cookiejar
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from typing import Literal, Optional

# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
BASE_URL = os.getenv('REDDIT_BASE_URL', 'https://www.reddit.com').rstrip('/')
MAX_POSTS = int(os.getenv('REDDIT_MAX_POSTS', '25'))
MAX_COMMENTS = int(os.getenv('REDDIT_MAX_COMMENTS', '100'))
REQUEST_TIMEOUT = float(os.getenv('REDDIT_TIMEOUT', '30'))

SORTS = ('relevance', 'hot', 'top', 'new', 'comments')
TIME_FILTERS = ('hour', 'day', 'week', 'month', 'year', 'all')

USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/131.0.0.0 Safari/537.36'
)

# Reddit's JS challenge: the page carries a token and an inline script that
# doubles a literal string to build the "solution".
_CHALLENGE_TOKEN_RE = re.compile(r'name="jsc_token"\s+value="([^"]+)"')
_CHALLENGE_SOLUTION_RE = re.compile(r'await\(async e=>e\+e\)\("([^"]+)"\)')
_POST_HREF_RE = re.compile(r'^/r/([^/]+)/comments/([A-Za-z0-9]+)(?:/([^/?#]*))?/?')


class RedditError(RuntimeError):
    """Raised when Reddit could not be read or returned something unusable."""


def log(message):
    print(message, flush=True)


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
# Note: httpx/requests get a hard 403 from reddit.com on this network because of
# their TLS fingerprint, while the stdlib client is served normally. The stdlib
# opener is therefore used deliberately - do not swap it out casually.
_opener_lock = threading.Lock()
_opener = None


def get_opener():
    """One shared opener so the challenge cookie is only solved once."""
    global _opener
    if _opener is None:
        with _opener_lock:
            if _opener is None:
                opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
                )
                opener.addheaders = [
                    ('User-Agent', USER_AGENT),
                    ('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'),
                    ('Accept-Language', 'en-US,en;q=0.9'),
                    ('Accept-Encoding', 'identity'),
                ]
                _opener = opener
    return _opener


def _open(url):
    with get_opener().open(url, timeout=REQUEST_TIMEOUT) as response:
        return response.geturl(), response.read().decode('utf-8', 'replace')


def _clear_challenge(url, text):
    """Solve Reddit's JS challenge if the response was the challenge page.

    The page submits a GET back to the same path with the original query plus
    ``solution``/``js_challenge``/``jsc_token``/``jsc_orig_r``, which is what the
    inline script does in a browser.
    """
    for _ in range(3):
        if 'jsc_token' not in text:
            return text
        token = _CHALLENGE_TOKEN_RE.search(text)
        literal = _CHALLENGE_SOLUTION_RE.search(text)
        if not (token and literal):
            raise RedditError('Reddit returned a JS challenge that could not be parsed.')
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query.update({
            'solution': literal.group(1) * 2,
            'js_challenge': '1',
            'jsc_token': token.group(1),
            'jsc_orig_r': '',
        })
        retry = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
        url, text = _open(retry)
    if 'jsc_token' in text:
        raise RedditError('Reddit kept returning its JS challenge page.')
    return text


def fetch(path, params=None):
    """GET a reddit.com path and return its HTML with any challenge cleared."""
    url = path if path.startswith('http') else BASE_URL + path
    if params:
        url += ('&' if '?' in url else '?') + urlencode(params)
    try:
        final, text = _open(url)
    except urllib.error.HTTPError as exc:
        raise RedditError(f'Reddit returned HTTP {exc.code} for {url}') from exc
    except urllib.error.URLError as exc:
        raise RedditError(f'Request to {url} failed: {exc.reason}') from exc
    return _clear_challenge(final, text)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _clean(node):
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip() if node else ''


def _as_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def split_votes(score, ratio):
    """Best-effort up/down split from the net score and the upvote ratio.

    Reddit publishes only ``score`` (net) and ``upvote_ratio``. With
    ``up = ratio * total`` and ``score = up - down`` it follows that
    ``total = score / (2 * ratio - 1)``. When the ratio is missing or exactly
    0.5 the split is mathematically undecidable and ``downvotes`` is None.
    """
    if ratio is None or not (0 < ratio < 1) or abs(2 * ratio - 1) < 1e-9 or score <= 0:
        return max(score, 0), None
    total = score / (2 * ratio - 1)
    if total <= 0:
        return max(score, 0), None
    upvotes = round(ratio * total)
    return upvotes, max(upvotes - score, 0)


def parse_search(html, limit):
    """Return the first ``limit`` results of a search page, in page order."""
    soup = BeautifulSoup(html, 'html.parser')
    anchors = soup.select('a[data-testid="post-title"]')
    if not anchors:
        # Older/markup-variant fallback: any link straight to a comment thread.
        anchors = soup.select('a[href*="/comments/"]')

    posts, seen = [], set()
    for anchor in anchors:
        match = _POST_HREF_RE.match(anchor.get('href') or '')
        if not match:
            continue
        subreddit, short_id, slug = match.group(1), match.group(2), match.group(3) or ''
        post_id = f't3_{short_id}'
        if post_id in seen:
            continue
        seen.add(post_id)
        posts.append({
            'id': post_id,
            'subreddit': subreddit,
            'title': (anchor.get('aria-label') or _clean(anchor)).strip(),
            'permalink': f'{BASE_URL}/r/{subreddit}/comments/{short_id}/{slug}/',
        })
        if len(posts) >= limit:
            break
    return posts


def parse_post(html):
    """Read the ``shreddit-post`` element of a post detail page."""
    soup = BeautifulSoup(html, 'html.parser')
    node = soup.find('shreddit-post', attrs={'permalink': True}) or soup.find('shreddit-post')
    if node is None:
        return None

    post_id = node.get('id') or ''
    body_node = soup.find('div', id=f'{post_id}-post-rtjson-content') or \
        node.find('div', attrs={'slot': 'text-body'})
    score = _as_int(node.get('score'))
    ratio = node.get('upvote-ratio')
    ratio = float(ratio) if ratio else None
    upvotes, downvotes = split_votes(score, ratio)

    return {
        'id': post_id,
        'title': node.get('post-title') or '',
        'subreddit': node.get('subreddit-prefixed-name') or '',
        'author': f'u/{node.get("author")}' if node.get('author') else '',
        'permalink': BASE_URL + node.get('permalink', ''),
        'created_utc': node.get('created-timestamp') or '',
        'post_type': node.get('post-type') or '',
        'body': _clean(body_node),
        'score': score,
        'upvotes': upvotes,
        'downvotes': downvotes,
        'upvote_ratio': ratio,
        'num_comments': _as_int(node.get('comment-count')),
    }


def parse_comments(html, limit):
    """Return the ``limit`` highest scoring comments of a comment-tree page."""
    soup = BeautifulSoup(html, 'html.parser')
    comments = []
    for node in soup.find_all('shreddit-comment'):
        thing_id = node.get('thingid') or ''
        if not thing_id:
            continue
        body_node = soup.find('div', id=f'{thing_id}-comment-rtjson-content') or \
            node.find('div', attrs={'slot': 'comment'})
        # Media-only comments (image/gif replies) and deleted/removed ones render
        # no text. The API promises comment bodies, so they are skipped instead
        # of being returned as empty strings.
        body = _clean(body_node)
        if not body:
            continue
        comments.append({
            'id': thing_id,
            'author': f'u/{node.get("author")}' if node.get('author') else '',
            'score': _as_int(node.get('score')),
            'depth': _as_int(node.get('depth')),
            'created_utc': node.get('created') or '',
            'permalink': BASE_URL + (node.get('permalink') or ''),
            'body': body,
        })
    comments.sort(key=lambda c: c['score'], reverse=True)
    return comments[:limit]


# --------------------------------------------------------------------------- #
# scraping
# --------------------------------------------------------------------------- #
def search_posts(keyword, limit, sort='hot', time_filter=None):
    """Return ``limit`` search results for ``keyword``."""
    params = {'q': keyword, 'sort': sort}
    if time_filter:
        params['t'] = time_filter
    html = fetch('/search/', params=params)
    return parse_search(html, limit)


def fetch_post_detail(post):
    """Fill in body / votes / comment count for a search result."""
    html = fetch(post['permalink'])
    detail = parse_post(html)
    if detail is None:
        raise RedditError(f'Could not read the post page for {post["permalink"]}')
    return detail


def fetch_comments(subreddit, post_id, limit):
    """Return the top ``limit`` comments of a post."""
    short_id = post_id[3:] if post_id.startswith('t3_') else post_id
    html = fetch(
        f'/svc/shreddit/comments/r/{subreddit}/{short_id}',
        params={'seeker-session': 'true', 'render-mode': 'partial'},
    )
    return parse_comments(html, limit)


def scrape(keyword, n=5, m=10, sort='hot', time_filter=None):
    """Search Reddit and return the first ``n`` posts with their top ``m`` comments."""
    if sort not in SORTS:
        raise RedditError(f'Unsupported sort "{sort}"; choose one of {", ".join(SORTS)}.')
    if time_filter and time_filter not in TIME_FILTERS:
        raise RedditError(
            f'Unsupported time filter "{time_filter}"; choose one of {", ".join(TIME_FILTERS)}.'
        )
    n = max(1, min(int(n), MAX_POSTS))
    m = max(0, min(int(m), MAX_COMMENTS))

    started = time.time()
    log(f'Searching reddit for "{keyword}" (sort={sort}'
        f'{f", t={time_filter}" if time_filter else ""}), n={n}, m={m}...')
    results = search_posts(keyword, n, sort=sort, time_filter=time_filter)
    log(f'  {len(results)} search result(s) found.')

    posts = []
    for index, result in enumerate(results, start=1):
        try:
            detail = fetch_post_detail(result)
            detail['comments'] = fetch_comments(result['subreddit'], result['id'], m)
        except RedditError as exc:
            log(f'  [{index}/{len(results)}] {result["id"]} skipped: {exc}')
            continue
        posts.append(detail)
        log(f'  [{index}/{len(results)}] {detail["id"]} "{detail["title"][:60]}" '
            f'score={detail["score"]} comments={len(detail["comments"])}')

    return {
        'keyword': keyword,
        'sort': sort,
        'time_filter': time_filter,
        'n': n,
        'm': m,
        'count': len(posts),
        'posts': posts,
        'elapsed_seconds': round(time.time() - started, 2),
    }


# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #
app = FastAPI(title='Reddit keyword scraper', version='1.0.0')


@app.get('/health')
def health():
    return {
        'status': 'ok',
        'base_url': BASE_URL,
        'max_posts': MAX_POSTS,
        'max_comments': MAX_COMMENTS,
    }


@app.get('/search')
def search(
    keyword: str = Query(..., min_length=1, max_length=200, description='search term'),
    n: int = Query(5, ge=1, le=MAX_POSTS, description='number of posts'),
    m: int = Query(10, ge=0, le=MAX_COMMENTS, description='top comments per post'),
    sorttype: Literal['relevance', 'hot', 'top', 'new', 'comments'] = Query(
        'hot', description='Reddit search sort order'),
    t: Optional[Literal['hour', 'day', 'week', 'month', 'year', 'all']] = Query(
        None, description='time window, only meaningful with sorttype=top'),
):
    """Search Reddit and return post bodies, vote counts and top comments."""
    if not keyword.strip():
        raise HTTPException(status_code=422, detail='keyword must not be blank.')
    try:
        return scrape(keyword.strip(), n=n, m=m, sort=sorttype, time_filter=t)
    except RedditError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def load_settings(argv=None):
    parser = argparse.ArgumentParser(description='Reddit keyword search scraper.')
    parser.add_argument('-k', '--keyword', required=True, help='keyword to search for')
    parser.add_argument('-n', type=int, default=5, help='number of posts (default 5)')
    parser.add_argument('-m', type=int, default=10, help='top comments per post (default 10)')
    parser.add_argument('--sort', default='hot', choices=SORTS, help='search sort order')
    parser.add_argument('-t', '--time-filter', dest='time_filter', choices=TIME_FILTERS,
                        help='time window, only meaningful with --sort top')
    parser.add_argument('--json', action='store_true', help='print the raw JSON payload')
    return parser.parse_args(argv)


def main(argv=None):
    args = load_settings(argv)
    try:
        result = scrape(args.keyword, n=args.n, m=args.m,
                        sort=args.sort, time_filter=args.time_filter)
    except RedditError as exc:
        log(f'error: {exc}')
        return 1
    if args.json:
        log(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for index, post in enumerate(result['posts'], start=1):
            log('')
            log(f'[{index}] {post["title"]}')
            log(f'    {post["subreddit"]} | {post["author"]} | score {post["score"]} '
                f'(up {post["upvotes"]} / down {post["downvotes"]}) | '
                f'{post["num_comments"]} comments')
            log(f'    {post["permalink"]}')
            if post['body']:
                log(f'    body: {post["body"][:300]}')
            for comment in post['comments']:
                log(f'      - ({comment["score"]}) {comment["author"]}: {comment["body"][:180]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
