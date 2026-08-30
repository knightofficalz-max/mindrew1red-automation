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

# ── Secrets ───────────────────────────────────────────────────────────────────
YT_CLIENT_ID     = os.environ['YT_CLIENT_ID']
YT_CLIENT_SECRET = os.environ['YT_CLIENT_SECRET']
YT_REFRESH_TOKEN = os.environ['YT_REFRESH_TOKEN']
HF_TOKEN         = os.environ['HF_TOKEN']
DRIVE_FOLDER_ID  = os.environ['DRIVE_FOLDER_ID']
DRIVE_API_KEY    = os.environ['DRIVE_API_KEY']
SLOT             = os.environ['SLOT']
MANAGER_URL      = os.environ.get('MANAGER_URL', '')   # Wasmer app URL
API_SECRET       = os.environ.get('API_SECRET', '')    # shared secret

UPLOADED_FILE    = 'uploaded.txt'
FONT_PATH        = 'font.otf'
LOGO_PATH        = 'logo.png'
SHORTS_MAX_SECS  = 60

# ── 1. Load uploaded ──────────────────────────────────────────────────────────
if os.path.exists(UPLOADED_FILE):
    with open(UPLOADED_FILE, 'r') as f:
        uploaded = set(line.strip() for line in f if line.strip())
else:
    uploaded = set()

print(f"Slot: {SLOT} | Already uploaded: {len(uploaded)}")

# ── 2. Fetch settings from Manager (social links, prompt) ─────────────────────
settings = {}
if MANAGER_URL:
    try:
        r = requests.get(f"{MANAGER_URL}/api/settings", headers={'x-api-secret': API_SECRET}, timeout=10)
        if r.ok:
            settings = r.json()
            print("✅ Settings loaded from manager")
    except Exception as e:
        print(f"⚠️ Could not fetch settings: {e}")

desc_prompt_extra = settings.get('desc_prompt', '')

# ── 3. List ALL Drive files (pagination) ──────────────────────────────────────
def list_all_drive_files(folder_id, api_key):
    all_files  = []
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
        resp = requests.get("https://www.googleapis.com/drive/v3/files", params=params).json()
        batch = resp.get('files', [])
        all_files.extend(batch)
        print(f"  Fetched {len(batch)} items (total: {len(all_files)})")
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return all_files

print("Listing Drive folder...")
all_files = list_all_drive_files(DRIVE_FOLDER_ID, DRIVE_API_KEY)
if not all_files:
    print("No files found. Exiting.")
    exit()

# ── 4. Resolve shortcuts + filter mp4 ────────────────────────────────────────
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

# ── 5. Numeric sort — supports "Copy of IG-MZ (N).mp4" and "N.mp4" ───────────
def numeric_key(f):
    name = f.get('name', '')
    match = re.search(r'\((\d+)\)', name)
    if match:
        return int(match.group(1))
    match = re.match(r'^(\d+)', name)
    if match:
        return int(match.group(1))
    return float('inf')

all_files_sorted = sorted(all_files, key=numeric_key)

# ── 6. Find next video ────────────────────────────────────────────────────────
target = download_id = video_name = None
for f in all_files_sorted:
    if f['id'] in uploaded:
        continue
    result = resolve_file(f)
    if result:
        download_id, video_name, _ = result
        target = f
        break

if not target:
    print("All videos uploaded. Nothing to do.")
    exit()

print(f"Next video: {video_name}")

# ── 7. Download ───────────────────────────────────────────────────────────────
safe_name  = re.sub(r'[^\w\-_\. ]', '_', video_name)
local_path = f"/tmp/{safe_name}"
print(f"Downloading...")
gdown.download(id=download_id, output=local_path, quiet=False)
if not os.path.exists(local_path):
    print("Download failed. Exiting.")
    exit(1)

# ── 8. Detect duration ────────────────────────────────────────────────────────
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

# ── 9. ffmpeg overlay — logo + glass text ────────────────────────────────────
overlaid_path = f"/tmp/overlaid_{safe_name}"

has_logo = os.path.exists(LOGO_PATH)
has_font = os.path.exists(FONT_PATH)

if has_logo and has_font:
    print("Adding logo + glass text overlay...")
    # Get video dimensions
    probe2 = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', 'stream=width,height',
         '-of', 'csv=s=x:p=0', local_path],
        capture_output=True, text=True
    )
    dims = probe2.stdout.strip().split('x')
    vid_w = int(dims[0]) if len(dims)==2 else 1080
    vid_h = int(dims[1]) if len(dims)==2 else 1920

    logo_size   = int(vid_w * 0.12)   # 12% of width
    logo_x      = vid_w - logo_size - 20
    logo_y      = 20
    text_y      = vid_h - 80
    font_size   = int(vid_w * 0.045)
    text        = "@MindRewired009"

    # Glass box behind text
    glass_h  = int(font_size * 1.8)
    glass_w  = int(vid_w * 0.65)
    glass_x  = int((vid_w - glass_w) / 2)
    glass_y  = text_y - int(glass_h * 0.3)

    vf = (
        f"[0:v]scale={vid_w}:{vid_h}[base];"
        # Logo: scale + circle mask
        f"[1:v]scale={logo_size}:{logo_size}[logo_scaled];"
        # Glass box
        f"[base]drawbox=x={glass_x}:y={glass_y}:w={glass_w}:h={glass_h}:"
        f"color=white@0.15:t=fill[with_glass];"
        # Logo overlay
        f"[with_glass][logo_scaled]overlay={logo_x}:{logo_y}[with_logo];"
        # Text overlay
        f"[with_logo]drawtext=text='{text}':fontfile={FONT_PATH}:"
        f"fontsize={font_size}:fontcolor=white:shadowcolor=black@0.6:shadowx=2:shadowy=2:"
        f"x=(w-text_w)/2:y={text_y}[out]"
    )

    result = subprocess.run([
        'ffmpeg', '-y',
        '-i', local_path,
        '-i', LOGO_PATH,
        '-filter_complex', vf,
        '-map', '[out]',
        '-map', '0:a?',
        '-c:v', 'libx264', '-crf', '23', '-preset', 'fast',
        '-c:a', 'aac',
        overlaid_path
    ], capture_output=True, text=True)

    if result.returncode == 0 and os.path.exists(overlaid_path):
        print("✅ Overlay added!")
        upload_path = overlaid_path
    else:
        print(f"⚠️ Overlay failed: {result.stderr[-300:]}")
        upload_path = local_path
else:
    print("⚠️ Logo/font not found — skipping overlay")
    upload_path = local_path

# ── 10. AI Metadata ───────────────────────────────────────────────────────────
print("Generating metadata with DeepSeek AI...")
ai = OpenAI(base_url="https://router.huggingface.co/v1", api_key=HF_TOKEN)

shorts_rule = "- Add #Shorts as the very last hashtag" if is_short else ""
custom_prompt = f"\n\nAdditional instructions:\n{desc_prompt_extra}" if desc_prompt_extra else ""

prompt = f"""You are a YouTube SEO expert for "Mind Rewired" — a motivational psychology channel.
The channel helps people rewire thinking for success, confidence, and mental strength.
Generate metadata for a {video_type}.

Filename: {video_name}

Respond ONLY in this exact JSON format. No markdown, no extra text:
{{
  "title": "...",
  "description": "...",
  "tags": ["tag1", "tag2"]
}}

Rules:
- title: powerful, emotionally resonant, under 100 chars. No generic titles. Use curiosity hooks.
- description: 150-200 words. Strong hook. Value for viewer. End with 5-8 hashtags.
{shorts_rule}
- tags: 12-15 tags (broad + specific)
- Vibe: deep, calm, transformational — NOT hype{custom_prompt}
"""

completion  = ai.chat.completions.create(
    model="deepseek-ai/DeepSeek-V3",
    messages=[{"role": "user", "content": prompt}],
)
raw  = completion.choices[0].message.content
raw  = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
raw  = raw.replace('```json', '').replace('```', '').strip()
meta = json.loads(raw)

title       = meta['title']
description = meta['description']
tags        = meta['tags']
if is_short and '#Shorts' not in description:
    description += '\n\n#Shorts'

print(f"Title: {title}")

# ── 11. YouTube OAuth ─────────────────────────────────────────────────────────
print("Authenticating YouTube...")
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

# ── 12. Upload to YouTube ─────────────────────────────────────────────────────
print("Uploading to YouTube...")
body = {
    'snippet': {
        'title':       title,
        'description': description,
        'tags':        tags,
        'categoryId':  '26'
    },
    'status': { 'privacyStatus': 'public' }
}

insert_request = youtube.videos().insert(
    part='snippet,status',
    body=body,
    media_body=MediaFileUpload(upload_path, chunksize=-1, resumable=True)
)

response = None
while response is None:
    status, response = insert_request.next_chunk()
    if status:
        print(f"  {int(status.progress()*100)}% uploaded...")

video_id  = response['id']
video_url = f"https://youtu.be/{video_id}"
print(f"✅ Upload complete! {video_url}")

# ── 13. Log to Turso via Manager API ─────────────────────────────────────────
if MANAGER_URL:
    try:
        log_data = {
            "file_id":     target['id'],
            "file_name":   video_name,
            "video_id":    video_id,
            "video_url":   video_url,
            "title":       title,
            "description": description,
            "slot":        SLOT,
            "is_short":    is_short,
            "duration":    round(duration, 1),
            "uploaded_at": __import__('datetime').datetime.utcnow().isoformat() + "Z"
        }
        r = requests.post(
            f"{MANAGER_URL}/api/uploads",
            json=log_data,
            headers={'x-api-secret': API_SECRET},
            timeout=15
        )
        print(f"✅ Logged to Turso: {r.status_code}")
    except Exception as e:
        print(f"⚠️ Turso log failed: {e}")

# ── 14. Mark as done ─────────────────────────────────────────────────────────
uploaded.add(target['id'])
with open(UPLOADED_FILE, 'w') as f:
    f.write('\n'.join(uploaded) + '\n')

print("Done! ✅")
