import os
import sys
import json
import re
import argparse
import asyncio
import httpx
import yaml
from dotenv import load_dotenv
from googleapiclient.discovery import build
from mutagen import File as MutagenFile
try:
    from transformers import pipeline
    nlp = pipeline('text-classification', 'distilbert-base-uncased-finetuned-sst-2-english')
except Exception:
    nlp = None
from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials
from supabase import create_client

# Config

def load_config(path):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}

# Clients & Cache

def init_clients():
    load_dotenv()
    youtube = lambda: build('youtube', 'v3', developerKey=os.getenv('YOUTUBE_API_KEY'))
    sp_auth = SpotifyClientCredentials(
        client_id=os.getenv('SPOTIFY_CLIENT_ID'),
        client_secret=os.getenv('SPOTIFY_CLIENT_SECRET')
    )
    spotify = Spotify(auth_manager=sp_auth)
    http = httpx.AsyncClient(timeout=10, follow_redirects=True)
    sup_url, sup_key = os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY')
    supabase = create_client(sup_url, sup_key) if sup_url and sup_key else None
    return youtube, spotify, http, supabase


def get_cache(supabase, key):
    if not supabase: return None
    try:
        resp = supabase.table('cache_music_info').select('data').eq('key', key).maybe_single().execute()
        data = getattr(resp, 'data', None)
        return data.get('data') if data else None
    except:
        return None


def set_cache(supabase, key, data):
    if not supabase: return
    supabase.table('cache_music_info').upsert({'key': key, 'data': data}, on_conflict='key').execute()


def make_cache_key(res_type, query, file):
    return json.dumps({'type': res_type, 'query': query, 'file': file}, sort_keys=True)

# Patterns
LYRIC_SITES = ["https://freemusicarchive.org/search/?quicksearch={q}"]
SPOTIFY_RE = re.compile(r'open\.spotify\.com/track/([\w]+)')
NONCOPY = ['no-copyright','copyright-free','royalty free','creative commons','public domain']

# Google CSE
async def google_search_license(http, api_key, cse_id, query):
    try:
        url = 'https://www.googleapis.com/customsearch/v1'
        params = {'key': api_key, 'cx': cse_id, 'q': f"{query} license"}
        r = await http.get(url, params=params)
        items = r.json().get('items', [])
        return [{'title': i['title'], 'link': i['link']} for i in items]
    except Exception:
        return []

# Scrape CC
async def scrape_license(http, query):
    tasks = [http.get(url.format(q=query)) for url in LYRIC_SITES]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return any(isinstance(r, httpx.Response) and 'Creative Commons' in r.text for r in results)

# YouTube
async def search_youtube(youtube, query):
    resp = youtube().search().list(q=query, part='snippet', type='video', maxResults=1).execute()
    items = resp.get('items', [])
    if not items:
        return {}
    vid = items[0]['id']['videoId']
    return await get_youtube_info(youtube, vid)

async def get_youtube_info(youtube, vid):
    data = youtube().videos().list(part='snippet,status', id=vid).execute().get('items', [{}])[0]
    s = data.get('snippet', {})
    st = data.get('status', {})
    return {
        'Title': s.get('title'),
        'Description': s.get('description'),
        'Author': s.get('channelTitle'),
        'Published At': s.get('publishedAt'),
        'License': st.get('license'),
        'Upload Status': st.get('uploadStatus')
    }

# Spotify

def get_spotify_info(spotify, url):
    m = SPOTIFY_RE.search(url)
    if not m:
        return {}
    d = spotify.track(m.group(1))
    return {
        'Name': d.get('name'),
        'Artists': ', '.join(a.get('name') for a in d.get('artists', [])),
        'Album': d.get('album', {}).get('name'),
        'Release Date': d.get('album', {}).get('release_date'),
        'External URL': d.get('external_urls', {}).get('spotify')
    }

# File metadata

def inspect_metadata(path):
    if not os.path.isfile(path):
        return {}
    t = MutagenFile(path, easy=True)
    return dict(t.tags) if t and t.tags else {}

# NLP

def nlp_classify(text):
    if not nlp or not text:
        return []
    try:
        return nlp(text)
    except:
        return []

# Determine status

def determine_status(yt, meta):
    text = ' '.join([yt.get('Title', ''), yt.get('Description', '')] + list(meta.values()))
    if any(term in text.lower() for term in NONCOPY):
        return 'Non-Copyright'
    if yt.get('License') == 'creativeCommon':
        return 'Creative Commons'
    return 'Copyrighted'

# Display

def display(res):
    print("\n=== Results ===")
    if 'spotify' in res:
        sp = res['spotify']
        print(f"Spotify: {sp['Name']} by {sp['Artists']}")
        print(f"  Album: {sp['Album']} ({sp['Release Date']})")
        print(f"  URL: {sp['External URL']}\n")

    if 'youtube' in res:
        yt = res['youtube']
        print(f"YouTube: {yt['Title']} by {yt['Author']}")
        print(f"  Published: {yt['Published At']}")
        print(f"  License: {yt['License']}, Upload Status: {yt['Upload Status']}\n")

    if 'google' in res:
        print("Google License Pages:")
        for g in res['google']:
            print(f"  - {g['title']}: {g['link']}")
        print()

    if 'scraped_cc' in res:
        print(f"Creative Commons on lyric site: {res['scraped_cc']}\n")

    if 'file_metadata' in res:
        print("File Metadata Tags:")
        for k, v in res['file_metadata'].items():
            print(f"  {k}: {v}")
        print()

    if 'nlp' in res:
        print("NLP Classification of Description:")
        for item in res['nlp']:
            print(f"  {item.get('label')} ({item.get('score'):.2f})")
        print()

    if 'copyright_status' in res:
        print(f"Final Copyright Status: {res['copyright_status']}\n")

# Main

async def run(args):
    youtube, spotify, http, supabase = init_clients()
    cache_key = make_cache_key(args.command, getattr(args, 'query', ''), getattr(args, 'file', ''))
    res = get_cache(supabase, cache_key) if supabase else None
    if res:
        display(res)
        await http.aclose()
        return

    res = {}
    if args.command == 'youtube':
        res['youtube'] = await search_youtube(youtube, args.query)
        res['google'] = await google_search_license(http, os.getenv('GOOGLE_SEARCH_API_KEY'), os.getenv('GOOGLE_CSE_ID'), args.query)
    if args.command == 'spotify':
        res['spotify'] = get_spotify_info(spotify, args.query)
    if args.command in ('youtube', 'spotify', 'bulk'):
        res['scraped_cc'] = await scrape_license(http, args.query)
    if args.command == 'file':
        res['file_metadata'] = inspect_metadata(args.file)

    if 'youtube' in res:
        res['nlp'] = nlp_classify(res['youtube'].get('Description', ''))

    if 'youtube' in res or 'file_metadata' in res:
        res['copyright_status'] = determine_status(res.get('youtube', {}), res.get('file_metadata', {}))

    display(res)
    set_cache(supabase, cache_key, res)
    await http.aclose()

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--config', default='config.yaml')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('youtube').add_argument('--query', required=True)
    sub.add_parser('spotify').add_argument('--query', required=True)
    sub.add_parser('bulk').add_argument('--query', required=True)
    sub.add_parser('file').add_argument('--file', required=True)
    args = p.parse_args()
    asyncio.run(run(args))
