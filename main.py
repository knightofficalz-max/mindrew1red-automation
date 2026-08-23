import os
import json
import subprocess
import requests
import re
import gdown
from openai import OpenAI
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ── Secrets ──────────────────────────────────────────────────────────────────
YT_CLIENT_ID     = os.environ['YT_CLIENT_ID']
YT_CLIENT_SECRET = os.environ['YT_CLIENT_SECRET']
YT_REFRESH_TOKEN = os.environ['YT_REFRESH_TOKEN']
HF_TOKEN         = os.environ['HF_TOKEN']
DRIVE_FOLDER_ID  = os.environ['DRIVE_FOLDER_ID']
DRIVE_API_KEY    = os.environ['DRIVE_API_KEY']
SLOT             = os.environ['SLOT']           # "morning" | "evening" | "night"

UPLOADED_FILE    = 'uploaded.txt'
SHORTS_MAX_SECS  = 60

# ── 1. Load already-uploaded file IDs ────────────────────────────────────────
if os.path.exists(UPLOADED_FILE):
    with open(UPLOADED_FILE, 'r') as f:
        uploaded = set(line.strip() for line in f if line.strip())
else:
    uploaded = set()

print(f"Slot: {SLOT} | Already uploaded: {len(uploaded)} videos")

# ── 2. List ALL files in Drive folder (handles 600+ via pagination) ───────────
def list_all_drive_files(folder_id, api_key):
    all_files = []
    page_token = None

    while True:
        params = {
            'q':        f"'{folder_id}' in parents",
            'key':      api_key,
            'fields':   'nextPageToken, files(id, name, mimeType, shortcutDetails)',
            'pageSize': 1000,
        }
        if page_token:
            params['pageToken'] = page_token

        resp = requests.get(
            "https://www.googleapis.com/drive/v3/files",
            params=params
        ).json()

        batch = resp.get('files', [])
        all_files.extend(batch)
        print(f"  Fetched {len(batch)} items (total so far: {len(all_files)})")

        page_token = resp.get('nextPageToken')
        if not page_token:
            break

    return all_files

print("Listing Drive folder (all pages)...")
all_files = list_all_drive_files(DRIVE_FOLDER_ID, DRIVE_API_KEY)

if not all_files:
    print("No files found in folder. Exiting.")
    exit()

print(f"Total items found: {len(all_files)}")

# ── 3. Resolve shortcuts + filter to videos only ─────────────────────────────
def resolve_file(f):
    mime = f.get('mimeType', '')
    if 'video/' in mime:
        return f['id'], f['name'], mime

    if mime == 'application/vnd.google-apps.shortcut':
        target_id = f.get('shortcutDetails', {}).get('targetId')
        if not target_id:
            return None
        target_resp = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{target_id}",
            params={'key': DRIVE_API_KEY, 'fields': 'id, name, mimeType'}
        ).json()
        target_mime = target_resp.get('mimeType', '')
        if 'video/' in target_mime:
            return target_id, target_resp.get('name', f['name']), target_mime

    return None

# ── 4. Numeric sort: 1.mp4 → 2.mp4 → ... → 600.mp4 ─────────────────────────
def numeric_key(f):
    """Extract number from filename — supports both '1.mp4' and 'Copy of IG-MZ (1).mp4' patterns."""
    name = f.get('name', '')
    # Pattern: Copy of IG-MZ (N).mp4
    match = re.search(r'\((\d+)\)', name)
    if match:
        return int(match.group(1))
    # Pattern: N.mp4 (plain number)
    match = re.match(r'^(\d+)', name)
    if match:
        return int(match.group(1))
    return float('inf')

all_files_sorted = sorted(all_files, key=numeric_key)

# ── 5. Find next video to upload (strict sequence, 1 per slot) ───────────────
target       = None
download_id  = None
video_name   = None

for f in all_files_sorted:
    file_key = f['id']  # stable ID used for uploaded.txt

    if file_key in uploaded:
        continue  # already done

    result = resolve_file(f)
    if result:
        download_id, video_name, _ = result
        target = f
        break

if not target:
    print("All videos uploaded or no videos found. Nothing to do.")
    exit()

print(f"Next video: {video_name}")

# ── 6. Download from Drive ────────────────────────────────────────────────────
safe_name  = re.sub(r'[^\w\-_\. ]', '_', video_name)
local_path = f"/tmp/{safe_name}"
print(f"Downloading (Drive ID: {download_id})...")
gdown.download(id=download_id, output=local_path, quiet=False)

if not os.path.exists(local_path):
    print("Download failed. Exiting without marking as done (will retry next slot).")
    exit(1)

# ── 7. Detect duration → Short or Regular ─────────────────────────────────────
print("Detecting duration...")
probe = subprocess.run(
    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
     '-of', 'default=noprint_wrappers=1:nokey=1', local_path],
    capture_output=True, text=True
)
duration_str = probe.stdout.strip()
duration     = float(duration_str) if duration_str else 999
is_short     = duration <= SHORTS_MAX_SECS
video_type   = "YouTube Short" if is_short else "Regular YouTube video"
print(f"Duration: {duration:.1f}s → {video_type}")

# ── 8. AI metadata (motivational channel prompt) ──────────────────────────────
print("Generating metadata with DeepSeek AI...")
ai = OpenAI(
    base_url="https://router.huggingface.co/v1",
    api_key=HF_TOKEN,
)

shorts_rule = "- Add #Shorts as the very last hashtag in the description" if is_short else ""

prompt = f"""You are a YouTube SEO expert for a motivational psychology channel called "Mind Rewired".
The channel helps people rewire their thinking for success, confidence, and mental strength.
Generate upload metadata for a {video_type}.

Filename: {video_name}

Respond ONLY in this exact JSON format. No markdown, no explanation, no extra text:
{{
  "title": "...",
  "description": "...",
  "tags": ["tag1", "tag2", "tag3"]
}}

Rules:
- title: powerful, emotionally resonant, keyword-rich, under 100 characters. Avoid generic titles like "Motivation Video". Use curiosity hooks, power words, or bold statements.
- description: 150-200 words. Start with a strong hook sentence. Include value the viewer gets. End with 5-8 relevant hashtags.
{shorts_rule}
- tags: 12-15 tags mixing broad (motivation, mindset, psychology) and specific terms
- Channel vibe: deep, calm, transformational — NOT hype or clickbait
"""

completion = ai.chat.completions.create(
    model="deepseek-ai/DeepSeek-V3",
    messages=[{"role": "user", "content": prompt}],
)

raw = completion.choices[0].message.content
raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
raw = raw.replace('```json', '').replace('```', '').strip()

meta        = json.loads(raw)
title       = meta['title']
description = meta['description']
tags        = meta['tags']

if is_short and '#Shorts' not in description:
    description += '\n\n#Shorts'

print(f"Title: {title}")

# ── 9. YouTube OAuth ───────────────────────────────────────────────────────────
print("Authenticating with YouTube...")
token_resp = requests.post('https://oauth2.googleapis.com/token', data={
    'client_id':     YT_CLIENT_ID,
    'client_secret': YT_CLIENT_SECRET,
    'refresh_token': YT_REFRESH_TOKEN,
    'grant_type':    'refresh_token'
}).json()

if 'access_token' not in token_resp:
    print(f"Token error: {token_resp}")
    exit(1)

creds   = Credentials(token=token_resp['access_token'])
youtube = build('youtube', 'v3', credentials=creds)

# ── 10. Upload to YouTube ──────────────────────────────────────────────────────
print("Uploading to YouTube...")
body = {
    'snippet': {
        'title':       title,
        'description': description,
        'tags':        tags,
        'categoryId':  '26'  # 26 = Howto & Style (good for motivation)
                              # 10=Music 22=People&Blogs 24=Entertainment 27=Education
    },
    'status': {
        'privacyStatus': 'public'
    }
}

insert_request = youtube.videos().insert(
    part='snippet,status',
    body=body,
    media_body=MediaFileUpload(local_path, chunksize=-1, resumable=True)
)

response = None
while response is None:
    status, response = insert_request.next_chunk()
    if status:
        print(f"  {int(status.progress() * 100)}% uploaded...")

video_id = response['id']
video_url = f"https://youtu.be/{video_id}"
print(f"Upload complete! {video_url}")

# ── 11. Log upload details for dashboard ──────────────────────────────────────
log_entry = {
    "file_id":    target['id'],
    "file_name":  video_name,
    "video_id":   video_id,
    "video_url":  video_url,
    "title":      title,
    "slot":       SLOT,
    "is_short":   is_short,
    "duration":   round(duration, 1),
    "uploaded_at": __import__('datetime').datetime.utcnow().isoformat() + "Z"
}

log_file = "upload_log.json"
logs = []
if os.path.exists(log_file):
    with open(log_file, 'r') as f:
        try:
            logs = json.load(f)
        except Exception:
            logs = []

logs.append(log_entry)
with open(log_file, 'w') as f:
    json.dump(logs, f, indent=2)

# ── 12. Mark as done ──────────────────────────────────────────────────────────
uploaded.add(target['id'])
with open(UPLOADED_FILE, 'w') as f:
    f.write('\n'.join(uploaded) + '\n')

print("Marked as done. Agent finished!")
