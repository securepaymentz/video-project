"""Upload the finished video to YouTube with the generated title, description and tags."""
import json, os, sys
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

video, meta_path = sys.argv[1], sys.argv[2]
meta = json.load(open(meta_path))
creds = Credentials(token=None, refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"].strip(),
    client_id=os.environ["YOUTUBE_CLIENT_ID"].strip(), client_secret=os.environ["YOUTUBE_CLIENT_SECRET"].strip(),
    token_uri="https://oauth2.googleapis.com/token", scopes=["https://www.googleapis.com/auth/youtube.upload"])
yt = build("youtube", "v3", credentials=creds)
tags, total = [], 0
for t in meta.get("tags") or []:
    t = str(t).replace("<", "").replace(">", "").strip()[:60]
    if t and total + len(t) + 2 <= 480:
        tags.append(t); total += len(t) + 2
body = {"snippet": {"title": (meta.get("title") or "New video")[:100], "description": (meta.get("description") or "")[:4900].replace("<", "").replace(">", ""),
                    "tags": tags, "categoryId": "25", "defaultLanguage": "en", "defaultAudioLanguage": "en"},
        "status": {"privacyStatus": os.environ.get("YOUTUBE_PRIVACY") or "public", "selfDeclaredMadeForKids": False}}
req = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(video, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True))
resp = None
while resp is None:
    status, resp = req.next_chunk()
    if status: print(f"Uploaded {int(status.progress() * 100)}%", flush=True)
print(f"Uploaded: https://youtu.be/{resp['id']}", flush=True)
