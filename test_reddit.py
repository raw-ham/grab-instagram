"""Tests for the Reddit scraper service.

Fast contract/unit tests run offline (the network layer is stubbed). The live
tests are skipped unless ``RUN_LIVE=1`` is set, e.g.

    RUN_LIVE=1 pytest -q test_reddit.py
"""
import os

import pytest
from fastapi.testclient import TestClient

import reddit_scraper as reddit

client = TestClient(reddit.app)


@pytest.fixture
def stub_scrape(monkeypatch):
    """Replace the network layer with a canned payload and record the call."""
    calls = {}

    def fake_scrape(keyword, n=5, m=10, sort='hot', time_filter=None):
        calls.update(keyword=keyword, n=n, m=m, sort=sort, time_filter=time_filter)
        return {
            'keyword': keyword,
            'sort': sort,
            'time_filter': time_filter,
            'n': n,
            'm': m,
            'count': 1,
            'posts': [{
                'id': 't3_abc123',
                'title': 'Example post',
                'subreddit': 'r/python',
                'body': 'hello world',
                'score': 10,
                'upvotes': 12,
                'downvotes': 2,
                'upvote_ratio': 0.85,
                'num_comments': 4,
                'comments': [{'id': 't1_x', 'score': 5, 'body': 'top comment'}],
            }],
            'elapsed_seconds': 0.1,
        }

    monkeypatch.setattr(reddit, 'scrape', fake_scrape)
    return calls


# --------------------------------------------------------------------------- #
# API contract
# --------------------------------------------------------------------------- #
def test_health():
    response = client.get('/health')
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok'
    assert body['max_posts'] >= 1


def test_search_happy_path(stub_scrape):
    response = client.get('/search', params={'keyword': 'python', 'n': 1, 'm': 2,
                                             'sorttype': 'top', 't': 'week'})
    assert response.status_code == 200
    body = response.json()
    assert body['count'] == 1
    post = body['posts'][0]
    assert post['body'] == 'hello world'
    assert post['upvotes'] == 12 and post['downvotes'] == 2
    assert post['comments'][0]['body'] == 'top comment'
    assert stub_scrape == {'keyword': 'python', 'n': 1, 'm': 2,
                           'sort': 'top', 'time_filter': 'week'}


def test_search_defaults(stub_scrape):
    assert client.get('/search', params={'keyword': 'python'}).status_code == 200
    assert stub_scrape['sort'] == 'hot'
    assert stub_scrape['time_filter'] is None


@pytest.mark.parametrize('sorttype', list(reddit.SORTS))
def test_search_accepts_every_sort(stub_scrape, sorttype):
    response = client.get('/search', params={'keyword': 'python', 'sorttype': sorttype})
    assert response.status_code == 200
    assert stub_scrape['sort'] == sorttype


def test_search_missing_keyword():
    assert client.get('/search').status_code == 422


def test_search_blank_keyword():
    assert client.get('/search', params={'keyword': '   '}).status_code == 422


@pytest.mark.parametrize('params', [
    {'keyword': 'python', 'sorttype': 'videos'},
    {'keyword': 'python', 'n': 0},
    {'keyword': 'python', 'm': -1},
    {'keyword': 'python', 'n': reddit.MAX_POSTS + 1},
    {'keyword': 'python', 't': 'decade'},
])
def test_search_rejects_bad_params(params):
    assert client.get('/search', params=params).status_code == 422


def test_search_maps_reddit_error_to_502(monkeypatch):
    def boom(*args, **kwargs):
        raise reddit.RedditError('reddit is unhappy')

    monkeypatch.setattr(reddit, 'scrape', boom)
    response = client.get('/search', params={'keyword': 'python'})
    assert response.status_code == 502
    assert 'reddit is unhappy' in response.json()['detail']


# --------------------------------------------------------------------------- #
# parsing units
# --------------------------------------------------------------------------- #
SEARCH_HTML = """
<html><body>
  <a data-testid="post-title" aria-label="First post"
     href="/r/python/comments/aaa111/first_post/">First post</a>
  <a data-testid="post-title" aria-label="Second post"
     href="/r/learnpython/comments/bbb222/second_post/">Second post</a>
  <a data-testid="post-title" aria-label="First post again"
     href="/r/python/comments/aaa111/first_post/">First post</a>
  <a href="/r/python/comments/aaa111/first_post/">not a titled result</a>
</body></html>
"""

POST_HTML = """
<html><body>
<shreddit-post id="t3_aaa111" permalink="/r/python/comments/aaa111/first_post/"
  post-title="First post" score="80" upvote-ratio="0.9" comment-count="12"
  subreddit-prefixed-name="r/python" author="alice"
  created-timestamp="2026-10-01T00:00:00+0000" post-type="text">
  <div id="t3_aaa111-post-rtjson-content"><p>the body text</p></div>
</shreddit-post>
</body></html>
"""

COMMENTS_HTML = """
<html><body>
  <shreddit-comment thingId="t1_low" author="bob" depth="0" score="3"
    permalink="/r/python/comments/aaa111/first_post/comment/low/">
    <div id="t1_low-comment-rtjson-content" slot="comment"><p>low scoring</p></div>
  </shreddit-comment>
  <shreddit-comment thingId="t1_high" author="carol" depth="0" score="42"
    permalink="/r/python/comments/aaa111/first_post/comment/high/">
    <div id="t1_high-comment-rtjson-content" slot="comment"><p>high scoring</p></div>
  </shreddit-comment>
  <shreddit-comment thingId="t1_mid" author="dave" depth="1" score="17"
    permalink="/r/python/comments/aaa111/first_post/comment/mid/">
    <div id="t1_mid-comment-rtjson-content" slot="comment"><p>middle</p></div>
  </shreddit-comment>
</body></html>
"""


def test_parse_search_keeps_page_order_and_dedupes():
    posts = reddit.parse_search(SEARCH_HTML, limit=5)
    assert [p['id'] for p in posts] == ['t3_aaa111', 't3_bbb222']
    assert posts[0]['title'] == 'First post'
    assert posts[0]['subreddit'] == 'python'
    assert posts[0]['permalink'].endswith('/r/python/comments/aaa111/first_post/')


def test_parse_search_respects_limit():
    assert len(reddit.parse_search(SEARCH_HTML, limit=1)) == 1


def test_parse_post_reads_body_and_votes():
    post = reddit.parse_post(POST_HTML)
    assert post['id'] == 't3_aaa111'
    assert post['title'] == 'First post'
    assert post['body'] == 'the body text'
    assert post['score'] == 80
    assert post['upvotes'] == 90 and post['downvotes'] == 10
    assert post['num_comments'] == 12
    assert post['author'] == 'u/alice'


def test_parse_comments_returns_highest_scores_first():
    comments = reddit.parse_comments(COMMENTS_HTML, limit=2)
    assert [c['id'] for c in comments] == ['t1_high', 't1_mid']
    assert comments[0]['body'] == 'high scoring'
    assert comments[0]['score'] == 42


def test_parse_comments_skips_comments_without_text():
    # A media-only reply (image body) and a deleted one carry no text; they must
    # not be returned as comments with an empty body.
    html = """
    <html><body>
      <shreddit-comment thingId="t1_media" author="bob" score="9">
        <div id="t1_media-comment-rtjson-content" slot="comment"><img src="x.png"></div>
      </shreddit-comment>
      <shreddit-comment thingId="t1_deleted" author="[deleted]" score="5">
        <div id="t1_deleted-comment-rtjson-content" slot="comment"></div>
      </shreddit-comment>
      <shreddit-comment thingId="t1_real" author="carol" score="3">
        <div id="t1_real-comment-rtjson-content" slot="comment"><p>actual text</p></div>
      </shreddit-comment>
    </body></html>
    """
    comments = reddit.parse_comments(html, limit=5)
    assert [c['id'] for c in comments] == ['t1_real']
    assert comments[0]['body'] == 'actual text'


def test_split_votes_derives_downvotes():
    # score 80 with a 0.9 ratio => 90 up, 10 down
    assert reddit.split_votes(80, 0.9) == (90, 10)
    # score 100 with a 0.8 ratio => 133 up, 33 down
    assert reddit.split_votes(100, 0.8) == (133, 33)


def test_split_votes_without_ratio():
    assert reddit.split_votes(25, None) == (25, None)
    assert reddit.split_votes(25, 0.5) == (25, None)


# --------------------------------------------------------------------------- #
# live tests (opt-in)
# --------------------------------------------------------------------------- #
live = pytest.mark.skipif(
    os.getenv('RUN_LIVE') != '1', reason='set RUN_LIVE=1 to hit reddit.com')


@live
def test_live_search_default_sort():
    response = client.get('/search', params={'keyword': 'python', 'n': 2, 'm': 3})
    assert response.status_code == 200
    body = response.json()
    assert body['count'] == 2
    assert all(p['body'] or p['post_type'] for p in body['posts'])
    assert all(len(p['comments']) <= 3 for p in body['posts'])


@live
def test_live_top_sort_has_comments():
    response = client.get('/search', params={'keyword': 'python', 'n': 1, 'm': 5,
                                             'sorttype': 'top', 't': 'month'})
    assert response.status_code == 200
    post = response.json()['posts'][0]
    scores = [c['score'] for c in post['comments']]
    assert scores == sorted(scores, reverse=True)
