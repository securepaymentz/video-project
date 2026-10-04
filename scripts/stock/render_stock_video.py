"""stock-engine v4 (installed by Studio)
Builds a video from Pexels clips + narration + burned-in subtitles.
Usage: PLAN=<base64 json> python render_stock_video.py out.mp4
"""
import base64, json, os, random, re, subprocess, sys, tempfile
import requests

out = sys.argv[1]
os.makedirs(os.path.dirname(out), exist_ok=True)
plan = json.loads(base64.b64decode(os.environ["PLAN"]).decode("utf-8"))
work = tempfile.mkdtemp()
vertical = plan.get("aspect", "9:16") == "9:16"
W, H = (1080, 1920) if vertical else (1920, 1080)
lang = plan.get("language", "es")
PEXELS = os.environ.get("PEXELS_API_KEY", "").strip()
PIXABAY = os.environ.get("PIXABAY_API_KEY", "").strip()
UNSPLASH = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
if not PEXELS and not UNSPLASH and not PIXABAY:
    sys.exit("Add a Pixabay, Pexels or Unsplash key in Studio and click 'Send keys to GitHub'.")

EDGE = {"es": "es-MX-JorgeNeural", "en": "en-US-GuyNeural", "pt": "pt-BR-AntonioNeural", "fr": "fr-FR-HenriNeural"}
EL_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
USE_EL = EL_KEY and os.environ.get("PREMIUM_VOICE", "true") != "false"
EL_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "").strip() or "pNInz6obpgDQGcFmaJgB"


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).strip())


def speak(text, path):
    global USE_EL
    if USE_EL:
        try:
            r = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE}",
                headers={"xi-api-key": EL_KEY, "Content-Type": "application/json"},
                json={"text": text, "model_id": "eleven_multilingual_v2", "voice_settings": {"stability": 0.45, "similarity_boost": 0.8}},
                timeout=120,
            )
            r.raise_for_status()
            open(path, "wb").write(r.content)
            return
        except Exception as e:
            print("ElevenLabs failed, using free voice:", e)
            USE_EL = False
    run(["edge-tts", "--voice", EDGE.get(lang, EDGE["en"]), "--rate", "+6%", "--text", text, "--write-media", path])


used = set()


def find_photo(query):
    orient = "portrait" if vertical else "landscape"
    for q in [query, " ".join(query.split()[:2]), plan.get("fallback_query", "city")]:
        r = requests.get("https://api.unsplash.com/search/photos", headers={"Authorization": f"Client-ID {UNSPLASH}"},
                         params={"query": q, "orientation": orient, "per_page": 15, "content_filter": "high"}, timeout=30)
        if r.status_code != 200:
            print("Unsplash error", r.status_code, r.text[:200]); continue
        for ph in r.json().get("results", []):
            if ph["id"] in used: continue
            used.add(ph["id"])
            try:  # Unsplash guidelines: report the download
                requests.get(ph["links"]["download_location"], headers={"Authorization": f"Client-ID {UNSPLASH}"}, timeout=15)
            except Exception:
                pass
            return ph["urls"]["raw"] + f"&w={W * 2}&fit=max&q=85", ph.get("user", {}).get("name", "Unsplash")
    return None, None


def find_pixabay(query, need):
    if not PIXABAY:
        return None, None
    for q in [query, " ".join(query.split()[:2]), plan.get("fallback_query", "city")]:
        try:
            r = requests.get("https://pixabay.com/api/videos/", params={"key": PIXABAY, "q": q[:100], "per_page": 30,
                             "safesearch": "true", "order": "popular"}, timeout=30)
        except Exception as e:
            print("Pixabay error", e); continue
        if r.status_code != 200:
            print("Pixabay error", r.status_code, r.text[:200]); continue
        hits = []
        for v in r.json().get("hits", []):
            if ("pb", v["id"]) in used: continue
            vs = v.get("videos", {})
            opts = [f for f in (vs.get("large"), vs.get("medium"), vs.get("small")) if f and f.get("url") and f.get("width")]
            if not opts: continue
            f = opts[0]
            is_v = f["height"] > f["width"]
            hits.append((is_v != vertical, v.get("duration", 0) < need, random.random(), v, f))
        hits.sort(key=lambda h: h[:3])
        if hits:
            _, _, _, v, f = hits[0]
            used.add(("pb", v["id"]))
            return f["url"], (v.get("user") or "Pixabay") + " (Pixabay)"
    return None, None


def find_clip(query, need):
    link, author = find_pixabay(query, need)
    if link or not PEXELS:
        return link, author
    orient = "portrait" if vertical else "landscape"
    for q in [query, " ".join(query.split()[:2]), plan.get("fallback_query", "city")]:
        r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": PEXELS},
                         params={"query": q, "orientation": orient, "per_page": 15, "size": "medium"}, timeout=30)
        if r.status_code != 200:
            print("Pexels error", r.status_code, r.text[:200]); continue
        vids = [v for v in r.json().get("videos", []) if v["id"] not in used]
        vids.sort(key=lambda v: (v.get("duration", 0) < need, random.random()))
        for v in vids:
            files = [f for f in v["video_files"] if f.get("file_type") == "video/mp4" and f.get("height")]
            if not files: continue
            target = H if vertical else W
            files.sort(key=lambda f: abs((f["height"] if vertical else f["width"]) - target))
            used.add(v["id"])
            return files[0]["link"], v.get("user", {}).get("name", "Pexels") + " (Pexels)"
    return None, None


def ass_time(t):
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


fs = 78 if vertical else 64
events = []
segments, audios, credits, photo_credits = [], [], set(), set()
t0 = 0.0
for i, sc in enumerate(plan["scenes"]):
    text = sc["text"].strip()
    a = os.path.join(work, f"a{i}.mp3"); speak(text, a)
    d = duration(a) + 0.25
    # Mix: first 2 scenes always video; afterwards every 3rd scene is a photo (~70% video)
    want_photo = UNSPLASH and i >= 2 and i % 3 == 2
    link, author = (None, None) if want_photo else find_clip(sc.get("query", plan.get("fallback_query", "city")), d)
    seg = os.path.join(work, f"s{i}.mp4")
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,setsar=1"
    if link:
        raw = os.path.join(work, f"r{i}.mp4")
        with requests.get(link, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(raw, "wb") as f:
                for chunk in r.iter_content(1 << 20): f.write(chunk)
        credits.add(author)
        zoom = f",zoompan=z='min(zoom+0.0007,1.08)':d=1:s={W}x{H}:fps=30"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", raw, "-t", f"{d:.2f}", "-an", "-vf", vf + zoom,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
    elif UNSPLASH and (photo := find_photo(sc.get("query", plan.get("fallback_query", "city"))))[0]:
        url, author = photo
        img = os.path.join(work, f"i{i}.jpg")
        open(img, "wb").write(requests.get(url, timeout=60).content)
        photo_credits.add(author)
        frames = int(d * 30) + 1
        zin = i % 2 == 0
        z = "min(1+0.0012*on,1.25)" if zin else "max(1.25-0.0012*on,1)"
        px = "iw/2-(iw/zoom/2)" if i % 3 else f"(iw-iw/zoom)*on/{frames}"
        kb = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
              f"zoompan=z='{z}':x='{px}':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps=30,setsar=1")
        run(["ffmpeg", "-y", "-loop", "1", "-i", img, "-t", f"{d:.2f}", "-vf", kb,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
    else:
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x111111:s={W}x{H}:r=30", "-t", f"{d:.2f}",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", seg])
    pad = os.path.join(work, f"p{i}.wav")
    run(["ffmpeg", "-y", "-i", a, "-af", "apad=pad_dur=0.25", "-t", f"{d:.2f}", "-ar", "44100", "-ac", "2", pad])
    segments.append(seg); audios.append(pad)
    # subtitles: chunks of ~4 words timed by word count
    words = text.split()
    chunks = [" ".join(words[k:k + 4]) for k in range(0, len(words), 4)] or [""]
    speak_d = d - 0.25
    per = speak_d / max(1, len(words))
    t = t0
    for c in chunks:
        cd = per * len(c.split())
        events.append(f"Dialogue: 0,{ass_time(t)},{ass_time(t + cd)},Cap,,0,0,0,,{c.upper()}")
        t += cd
    t0 += d

with open(os.path.join(work, "v.txt"), "w") as f:
    f.writelines(f"file '{s}'\n" for s in segments)
with open(os.path.join(work, "a.txt"), "w") as f:
    f.writelines(f"file '{s}'\n" for s in audios)
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", os.path.join(work, "v.txt"), "-c", "copy", os.path.join(work, "video.mp4")])
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", os.path.join(work, "a.txt"), os.path.join(work, "voice.wav")])

margin = int(H * (0.28 if vertical else 0.1))
ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,3,2,60,60,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(events) + "\n"
open(os.path.join(work, "subs.ass"), "w", encoding="utf-8").write(ass)

run(["ffmpeg", "-y", "-i", os.path.join(work, "video.mp4"), "-i", os.path.join(work, "voice.wav"),
     "-vf", f"subtitles={os.path.join(work, 'subs.ass')}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out])

desc = plan.get("description", "")
if credits:
    desc += "\n\nVideos: " + ", ".join(sorted(credits))
if photo_credits:
    desc += "\n\nPhotos: " + ", ".join(sorted(photo_credits)) + " on Unsplash"
meta = {"title": plan.get("title", "")[:100], "description": desc, "tags": plan.get("tags", [])}
json.dump(meta, open(os.path.join(os.path.dirname(out), "meta.json"), "w"), ensure_ascii=False)
print("Done:", out, f"{t0:.1f}s")
