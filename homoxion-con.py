import argparse
import os
import sys
import json
import re
import logging
import requests
from dotenv import load_dotenv
from googleapiclient.discovery import build
from mutagen import File as MutagenFile
from supabase import create_client, Client
from postgrest.exceptions import APIError
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

load_dotenv()
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
GOOGLE_SEARCH_API_KEY = os.getenv('GOOGLE_SEARCH_API_KEY')
GOOGLE_CSE_ID = os.getenv('GOOGLE_CSE_ID')
SPOTIFY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
CACHE_TABLE = 'cache_music_info'
youtube = lambda: build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
yt_pattern = re.compile(r'(?:youtu\.be/|youtube\.com/watch\?v=)([\w-]{11})')
spotify_auth = SpotifyClientCredentials(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET)
spotify = spotipy.Spotify(auth_manager=spotify_auth)
spotify_pattern = re.compile(r'open\.spotify\.com/track/([\w]+)')
NONCOPY_KEYWORDS = [
    'no-copyright', 'copyright-free', 'royalty free',
    'creative commons', 'public domain'
]
def get_cache(key: str):
    try:
        resp = supabase.table(CACHE_TABLE).select('data').eq('key', key).maybe_single().execute()
        data = getattr(resp, 'data', None)
        return data.get('data') if data else None
    except APIError:
        return None
def set_cache(key: str, data: dict):
    supabase.table(CACHE_TABLE).upsert({'key': key, 'data': data}, on_conflict='key').execute()
def make_cache_key(query: str, file: str, no_cache: bool):
    return json.dumps({'query': query, 'file': file, 'no_cache': no_cache}, sort_keys=True)
def get_spotify_track_info(url: str):
    match = spotify_pattern.search(url)
    if not match:
        return {}
    track_id = match.group(1)
    data = spotify.track(track_id)
    return {
        'name': data.get('name'),
        'artists': [a['name'] for a in data.get('artists', [])],
        'album': data.get('album', {}).get('name'),
        'release_date': data.get('album', {}).get('release_date'),
        'external_urls': data.get('external_urls', {})
    }
def get_youtube_info(video_id: str):
    resp = youtube().videos().list(part='snippet,contentDetails,status', id=video_id).execute()
    if not resp.get('items'):
        return {}
    info = resp['items'][0]
    snippet = info['snippet']
    return {
        'title': snippet.get('title'),
        'description': snippet.get('description'),
        'author': snippet.get('channelTitle'),
        'publishedAt': snippet.get('publishedAt'),
        'license': info['status'].get('license'),
        'uploadStatus': info['status'].get('uploadStatus')
    }
def search_youtube(query: str):
    resp = youtube().search().list(q=query, part='snippet', type='video', maxResults=1).execute()
    items = resp.get('items', [])
    if not items:
        return {}
    vid = items[0]['id']['videoId']
    return get_youtube_info(vid)
def google_search_license(query: str):
    if not GOOGLE_SEARCH_API_KEY or not GOOGLE_CSE_ID:
        return []
    url = 'https://www.googleapis.com/customsearch/v1'
    params = {'key': GOOGLE_SEARCH_API_KEY, 'cx': GOOGLE_CSE_ID, 'q': f"{query} license"}
    return [{'title': i['title'], 'link': i['link']} for i in requests.get(url, params=params).json().get('items', [])]
def inspect_file_metadata(path: str):
    if not os.path.isfile(path):
        return {}
    audio = MutagenFile(path, easy=True)
    tags = dict(audio.tags) if audio and audio.tags else {}
    return tags
def contains_noncopy_terms(text: str):
    text_lower = text.lower()
    return any(term in text_lower for term in NONCOPY_KEYWORDS)
def determine_copyright(res: dict):
    yt = res.get('youtube') or res.get('youtube_link', {})
    title = yt.get('title', '')
    desc = yt.get('description', '')
    if contains_noncopy_terms(title) or contains_noncopy_terms(desc):
        return 'Non-Copyright (Free Use)'
    if yt.get('license') == 'creativeCommon':
        return 'Creative Commons'
    fm = res.get('file_metadata', {})
    if 'copyright' in fm:
        return 'Copyrighted'
    return 'Copyrighted'
def print_results(res: dict):
    print('\n=== Results ===')
    if 'spotify' in res:
        s = res['spotify']
        print(f"Spotify Track: {s['name']} by {', '.join(s['artists'])}")
    if 'youtube_link' in res or 'youtube' in res:
        y = res.get('youtube_link') or res.get('youtube')
        print(f"YouTube: {y.get('title')} by {y.get('author')}, Published: {y.get('publishedAt')}")
    if 'google' in res:
        print('Google license pages:')
        for g in res['google']:
            print(f"- {g['title']}: {g['link']}")
    if 'file_metadata' in res:
        print('File metadata:')
        for k, v in res['file_metadata'].items(): print(f"{k}: {v}")
    print(f"Copyright status: {res.get('copyright_status')}\n")
    print('Raw JSON output:')
    print(json.dumps(res, indent=2))
def main():
    parser = argparse.ArgumentParser(description='Advanced Music Copyright Info Tool')
    parser.add_argument('--query', help='Search terms or Spotify/YouTube URL')
    parser.add_argument('--file', help='Local audio file')
    parser.add_argument('--no-cache', action='store_true', help='Ignore cache')
    args = parser.parse_args()
    key = make_cache_key(args.query, args.file, args.no_cache)
    res = None if args.no_cache else get_cache(key)
    if res:
        logging.info('Loaded from cache')
    else:
        res = {}
        q = args.query or ''
        if spotify_pattern.search(q):
            res['spotify'] = get_spotify_track_info(q)
        elif yt_pattern.search(q):
            vid = yt_pattern.search(q).group(1)
            res['youtube_link'] = get_youtube_info(vid)
        elif q:
            res['youtube'] = search_youtube(q)
            res['google'] = google_search_license(q)
        if args.file:
            res['file_metadata'] = inspect_file_metadata(args.file)
        res['copyright_status'] = determine_copyright(res)
        set_cache(key, res)
    print_results(res)
if __name__ == '__main__':
    required = [YOUTUBE_API_KEY, SUPABASE_URL, SUPABASE_KEY, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET]
    if not all(required):
        logging.error('Missing required environment variables')
        sys.exit(1)
    main()
