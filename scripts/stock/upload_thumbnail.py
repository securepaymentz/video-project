"""Attach the video's generated thumbnail after the existing uploader returns its video ID."""
import os, re, sys
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

video_id = re.search(r"https://youtu\.be/([A-Za-z0-9_-]+)", open(sys.argv[1]).read())
if not video_id:
    print("No uploaded video ID found; thumbnail not attached")
    sys.exit(0)
thumbnail = sys.argv[2]
if not os.path.isfile(thumbnail):
    print("Generated thumbnail missing; YouTube keeps its automatic thumbnail")
    sys.exit(0)
try:
    credentials = Credentials(token=None, refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"], client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token", scopes=["https://www.googleapis.com/auth/youtube.upload"])
    youtube = build("youtube", "v3", credentials=credentials)
    youtube.thumbnails().set(videoId=video_id.group(1), media_body=MediaFileUpload(thumbnail, mimetype="image/jpeg")).execute()
    print("Thumbnail attached to uploaded video")
except Exception as e:
    print(f"::warning::Custom thumbnail not attached (video is still published): {e}")
    sys.exit(0)
