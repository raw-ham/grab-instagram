"""Tests for the FastAPI service in grab_in_server.py.

These run without Instagram: the scraping call is replaced with a stub, so they
check the API contract (routing, validation, status codes, JSON shape) quickly.
Set RUN_LIVE=1 to also run the real end-to-end test at the bottom.
"""
import os

import pytest
from fastapi.testclient import TestClient

import grab_in_server


@pytest.fixture
def client():
    return TestClient(grab_in_server.app)


@pytest.fixture
def stub_scraper(monkeypatch):
    """Replace the scraping call and record the arguments it was given."""
    calls = []

    def fake_fetch_links(keyword, limit, result_type, save_csv=False):
        calls.append({
            'keyword': keyword,
            'limit': limit,
            'result_type': result_type,
            'save_csv': save_csv,
        })
        return [f'https://www.instagram.com/reel/CODE{i}/' for i in range(1, 4)]

    monkeypatch.setattr(grab_in_server, 'fetch_links', fake_fetch_links)
    return calls


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
def test_health_reports_ok(client):
    response = client.get('/health')
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok'
    assert body['busy'] is False
    assert body['max_limit'] == grab_in_server.MAX_LIMIT


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_search_returns_links_json(client, stub_scraper):
    response = client.get('/search', params={'keyword': 'gym', 'limit': 3})
    assert response.status_code == 200
    body = response.json()

    assert body['keyword'] == 'gym'
    assert body['limit'] == 3
    assert body['count'] == 3
    assert body['links'] == [
        'https://www.instagram.com/reel/CODE1/',
        'https://www.instagram.com/reel/CODE2/',
        'https://www.instagram.com/reel/CODE3/',
    ]
    assert isinstance(body['elapsed_seconds'], float)


def test_search_passes_arguments_through(client, stub_scraper):
    client.get('/search', params={'keyword': 'php bug', 'limit': 7,
                                  'type': 'posts', 'save_csv': 'true'})
    assert stub_scraper == [{
        'keyword': 'php bug',
        'limit': 7,
        'result_type': 'posts',
        'save_csv': True,
    }]


def test_search_defaults_limit_and_type(client, stub_scraper):
    client.get('/search', params={'keyword': 'gym'})
    assert stub_scraper[0]['limit'] == 5
    assert stub_scraper[0]['result_type'] == 'both'
    assert stub_scraper[0]['save_csv'] is False


@pytest.mark.parametrize('result_type', ['reels', 'posts', 'both'])
def test_search_accepts_every_type(client, stub_scraper, result_type):
    response = client.get('/search', params={'keyword': 'gym', 'type': result_type})
    assert response.status_code == 200
    assert response.json()['type'] == result_type


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def test_missing_keyword_is_rejected(client):
    assert client.get('/search').status_code == 422


def test_blank_keyword_is_rejected(client):
    assert client.get('/search', params={'keyword': ''}).status_code == 422


def test_keyword_longer_than_100_chars_is_rejected(client):
    assert client.get('/search', params={'keyword': 'x' * 101}).status_code == 422


@pytest.mark.parametrize('limit', [0, -1, grab_in_server.MAX_LIMIT + 1, 9999])
def test_out_of_range_limit_is_rejected(client, limit):
    response = client.get('/search', params={'keyword': 'gym', 'limit': limit})
    assert response.status_code == 422


def test_limit_upper_bound_is_accepted(client, stub_scraper):
    response = client.get('/search', params={'keyword': 'gym', 'limit': grab_in_server.MAX_LIMIT})
    assert response.status_code == 200


def test_unknown_type_is_rejected(client):
    assert client.get('/search', params={'keyword': 'gym', 'type': 'videos'}).status_code == 422


# --------------------------------------------------------------------------- #
# failure mapping
# --------------------------------------------------------------------------- #
def test_no_links_maps_to_404(client, monkeypatch):
    monkeypatch.setattr(grab_in_server, 'fetch_links', lambda *a, **k: [])
    response = client.get('/search', params={'keyword': 'blackoctober'})
    assert response.status_code == 404
    assert 'No links found' in response.json()['detail']


def test_scraper_runtime_error_maps_to_503(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('Instagram served a security challenge')

    monkeypatch.setattr(grab_in_server, 'fetch_links', boom)
    response = client.get('/search', params={'keyword': 'gym'})
    assert response.status_code == 503
    assert 'security challenge' in response.json()['detail']


def test_driver_failure_maps_to_502(client, monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionError('chromedriver died')

    monkeypatch.setattr(grab_in_server, 'fetch_links', boom)
    response = client.get('/search', params={'keyword': 'gym'})
    assert response.status_code == 502
    assert 'chromedriver died' in response.json()['detail']


def test_missing_credentials_file_maps_to_500(client, monkeypatch, tmp_path):
    monkeypatch.setattr(grab_in_server, 'CREDENTIALS_FILE', tmp_path / 'nope.json')
    response = client.get('/search', params={'keyword': 'gym'})
    assert response.status_code == 500
    assert 'not found' in response.json()['detail']


# --------------------------------------------------------------------------- #
# failing fast beats hanging forever
# --------------------------------------------------------------------------- #
def test_credentials_helper_rejects_incomplete_file(monkeypatch, tmp_path):
    creds = tmp_path / 'creds.json'
    creds.write_text('{"username": "someone"}', encoding='utf-8')
    monkeypatch.setattr(grab_in_server, 'CREDENTIALS_FILE', creds)
    with pytest.raises(grab_in_server.HTTPException) as excinfo:
        grab_in_server.load_credentials()
    assert excinfo.value.status_code == 500
    assert 'username' in excinfo.value.detail


def test_credentials_helper_reads_real_file(monkeypatch, tmp_path):
    creds = tmp_path / 'creds.json'
    creds.write_text('{"username": "u", "password": "p", "two_factor_code": "123456"}',
                     encoding='utf-8')
    monkeypatch.setattr(grab_in_server, 'CREDENTIALS_FILE', creds)
    assert grab_in_server.load_credentials() == ('u', 'p', '123456')


# --------------------------------------------------------------------------- #
# live end-to-end (opt-in: hits Instagram, needs a logged-in profile)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.getenv('RUN_LIVE') != '1', reason='set RUN_LIVE=1 to hit Instagram')
def test_live_search_returns_reel_links(client):
    response = client.get('/search', params={'keyword': 'gym', 'limit': 2, 'type': 'reels'})
    assert response.status_code == 200
    links = response.json()['links']
    assert len(links) == 2
    assert all('instagram.com/reel/' in link for link in links)
