"""stock-engine v8 (installed by Studio)
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

# ---------- presentation graphics: title screens, charts, price cards ----------
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter
FB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FSERIF = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
OPENAI = os.environ.get("OPENAI_API_KEY", "").strip()
IMG_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "").strip() or "gpt-image-1-mini"
BG = {"red": ((125, 14, 14), (35, 0, 0)), "blue": ((14, 44, 150), (2, 8, 48)),
      "green": ((12, 95, 55), (2, 28, 14)), "dark": ((48, 48, 58), (8, 8, 12))}
U = min(W, H) / 1080.0


def fit(path, text, maxw, size):
    size = int(size)
    while size > 18:
        f = ImageFont.truetype(path, size)
        if f.getlength(text) <= maxw:
            return f
        size -= 4
    return ImageFont.truetype(path, size)


def ease(p):
    p = max(0.0, min(1.0, p))
    return 1 - (1 - p) ** 3


def radial(c1, c2):
    sm = Image.new("RGB", (48, 48))
    for y in range(48):
        for x in range(48):
            d = min(1.0, math.hypot(x - 24, y - 24) / 34)
            sm.putpixel((x, y), tuple(int(c1[k] * (1 - d) + c2[k] * d) for k in range(3)))
    return sm.resize((W, H), Image.BICUBIC)


def grid(img, t, alpha=80):
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    step = int(135 * U)
    off = int((t * 40) % step)
    for x in range(-step, W + step, step):
        pts = [(x + off + 4 * math.sin(y / 140 + t * 2), y) for y in range(0, H + 40, 40)]
        d.line(pts, fill=(255, 255, 255, alpha), width=max(2, int(3 * U)))
    for y in range(-step, H + step, step):
        pts = [(x, y + off + 4 * math.sin(x / 140 + t * 2)) for x in range(0, W + 40, 40)]
        d.line(pts, fill=(255, 255, 255, alpha), width=max(2, int(3 * U)))
    img.alpha_composite(ov)


def label(text, size, maxw, fg=(255, 255, 255), bg=(0, 0, 0, 235), path=FB):
    f = fit(path, text, maxw, size)
    l, t, r, b = f.getbbox(text)
    px, py = int(f.size * 0.35), int(f.size * 0.22)
    im = Image.new("RGBA", (r - l + 2 * px, b - t + 2 * py), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=int(f.size * 0.12), fill=bg)
    d.text((px - l, py - t), text, font=f, fill=fg)
    return im


def paste_scaled(img, piece, cx, cy, scale, alpha=1.0):
    if scale <= 0.01 or alpha <= 0.01:
        return
    pc = piece.resize((max(1, int(piece.width * scale)), max(1, int(piece.height * scale))), Image.LANCZOS)
    if alpha < 1:
        a = pc.getchannel("A").point(lambda v: int(v * alpha))
        pc.putalpha(a)
    img.alpha_composite(pc, (int(cx - pc.width / 2), int(cy - pc.height / 2)))


def paper():
    rnd = random.Random(7)
    sm = Image.new("L", (W // 8, H // 8))
    for y in range(sm.height):
        for x in range(sm.width):
            sm.putpixel((x, y), 222 + rnd.randint(-14, 12))
    base = sm.resize((W, H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))
    return Image.merge("RGBA", (base, base, base.point(lambda v: min(255, v + 2)), Image.new("L", (W, H), 255)))


def object_image(desc, query):
    """Cut-out illustration of the item (OpenAI, transparent bg) or a polaroid photo as a free fallback."""
    if OPENAI and desc:
        try:
            r = requests.post("https://api.openai.com/v1/images/generations",
                              headers={"Authorization": f"Bearer {OPENAI}", "Content-Type": "application/json"},
                              json={"model": IMG_MODEL, "size": "1024x1024", "quality": "medium", "background": "transparent",
                                    "output_format": "png", "n": 1,
                                    "prompt": f"Photorealistic product photo of {desc}, isolated, centered, full object visible, "
                                              "studio lighting, soft shadow, transparent background, no text, no logos."},
                              timeout=180)
            r.raise_for_status()
            path = os.path.join(work, f"obj{random.randint(0, 1 << 30)}.png")
            open(path, "wb").write(base64.b64decode(r.json()["data"][0]["b64_json"]))
            return Image.open(path).convert("RGBA")
        except Exception as e:
            print("OpenAI image failed, using a photo:", str(e)[:200])
    if UNSPLASH:
        url, author = find_photo(query or desc or plan.get("fallback_query", "city"))
        if url:
            photo_credits.add(author)
            ph = Image.open(__import__("io").BytesIO(requests.get(url, timeout=60).content)).convert("RGB")
            side = 900
            ph = ph.resize((side, int(side * ph.height / ph.width)), Image.LANCZOS)
            ph = ph.crop((0, 0, side, min(ph.height, side)))
            b = 40
            pol = Image.new("RGBA", (ph.width + 2 * b, ph.height + 3 * b), (255, 255, 255, 255))
            pol.paste(ph, (b, b))
            return pol.rotate(-4, expand=True, resample=Image.BICUBIC)
    return None


def frame_title(v, t, p, cache):
    if "bg" not in cache:
        cache["bg"] = radial(*BG.get(v.get("color") or "red", BG["red"]))
        cache["lab"] = label(v["headline"].upper(), 92 * U, W * 0.86)
    img = cache["bg"].convert("RGBA")
    grid(img, t)
    paste_scaled(img, cache["lab"], W / 2, H / 2, 0.82 + 0.18 * ease(p * 1.4), ease(p * 2))
    return img


def frame_chart(v, t, p, cache):
    up = v.get("trend") != "down"
    if "bg" not in cache:
        cache["bg"] = radial(*BG.get(v.get("color") or "blue", BG["blue"]))
        cache["lab"] = label(v["headline"].upper(), 58 * U, W * 0.6)
        cache["val"] = label(v.get("value") or "", 150 * U, W * 0.7, bg=(110, 110, 120, 170)) if v.get("value") else None
    img = cache["bg"].convert("RGBA")
    grid(img, t, 55)
    d = ImageDraw.Draw(img)
    x0, x1 = W * 0.18, W * 0.86
    y0, y1 = H * (0.3 if vertical else 0.12), H * (0.72 if vertical else 0.9)
    d.line([(x0, y0), (x0, y1), (x1, y1)], fill=(255, 255, 255, 255), width=max(3, int(4 * U)))
    n = 7
    bw = (x1 - x0) / n * 0.55
    tops = []
    for k in range(n):
        frac = 0.15 + 0.8 * ((k + 1) / n) ** 1.6
        if not up:
            frac = 0.15 + 0.8 * ((n - k) / n) ** 1.6
        g = ease(p * 1.6 - k * 0.12)
        bx = x0 + (x1 - x0) / n * (k + 0.5) - bw / 2
        top = y1 - (y1 - y0) * frac * g
        tops.append((bx + bw / 2, y1 - (y1 - y0) * frac))
        if g > 0:
            d.rectangle([bx, top, bx + bw, y1 - 2], fill=(225, 228, 245, 235))
    # yellow trend arrow drawn progressively
    pa = ease(p * 1.3 - 0.2)
    if pa > 0:
        pts = [(x0 + (x1 - x0) * s, (y1 - (y1 - y0) * (0.25 + 0.7 * (s ** 2 if up else (1 - s) ** 2)))) for s in [i / 40 for i in range(41)]]
        pts = [(x, yy - (y1 - y0) * 0.06) for x, yy in pts][: max(2, int(41 * pa))]
        col = (255, 220, 40, 255) if up else (255, 80, 60, 255)
        d.line(pts, fill=col, width=max(5, int(8 * U)), joint="curve")
        (ax, ay), (bx2, by2) = pts[-2], pts[-1]
        ang = math.atan2(by2 - ay, bx2 - ax)
        L = 34 * U
        for da in (2.6, -2.6):
            d.line([(bx2, by2), (bx2 + L * math.cos(ang + da), by2 + L * math.sin(ang + da))], fill=col, width=max(5, int(8 * U)))
    paste_scaled(img, cache["lab"], x0 + cache["lab"].width / 2 + 30 * U, y0 + (y1 - y0) * 0.3, 1, ease(p * 2.5))
    if cache["val"] is not None:
        paste_scaled(img, cache["val"], W / 2, y1 - (y1 - y0) * 0.15, 0.7 + 0.3 * ease(p * 1.5 - 0.5), ease(p * 2 - 0.8))
    return img


def frame_card(v, t, p, cache):
    up = v.get("trend") != "down"
    if "bg" not in cache:
        cache["bg"] = paper()
        cache["obj"] = object_image(v.get("object") or "", v.get("query") or "")
        cache["head"] = fit(FB, v["headline"].upper(), W * (0.8 if vertical else 0.45), 150 * U)
        sub = v.get("sub") or ""
        if sub:
            f = fit(FSERIF, sub, W * (0.78 if vertical else 0.42), 84 * U)
            l, tt, r, b = f.getbbox(sub)
            pad = int(30 * U)
            strip = Image.new("RGBA", (r - l + 2 * pad, b - tt + 2 * pad), (0, 0, 0, 0))
            dd = ImageDraw.Draw(strip)
            rnd = random.Random(3)
            sw, sh = strip.size
            jag = [(x, rnd.randint(0, 10)) for x in range(0, sw + 12, 12)] + [(x, sh - rnd.randint(0, 10)) for x in range(sw, -12, -12)]
            dd.polygon(jag, fill=(255, 255, 255, 255))
            dd.text((pad - l, pad - tt), sub, font=f, fill=(20, 20, 20))
            cache["sub"] = strip.rotate(-2, expand=True, resample=Image.BICUBIC)
        else:
            cache["sub"] = None
        cache["val"] = label(v.get("value") or "", 120 * U, W * 0.5, bg=(130, 130, 136, 230)) if v.get("value") else None
    img = cache["bg"].copy()
    d = ImageDraw.Draw(img)
    obj = cache["obj"]
    if vertical:
        oc, hc, vc = (W / 2, H * 0.36), (W / 2, H * 0.12), (W / 2, H * 0.6)
    else:
        oc, hc, vc = (W * 0.3, H * 0.6), (W * 0.7, H * 0.2), (W * 0.74, H * 0.72)
    if obj is not None:
        box = (W * 0.75, H * 0.36) if vertical else (W * 0.42, H * 0.62)
        sc = min(box[0] / obj.width, box[1] / obj.height)
        slide = (1 - ease(p * 1.5)) * W * 0.25
        paste_scaled(img, obj, oc[0] - slide, oc[1], sc, ease(p * 2))
    hf = cache["head"]
    a = ease(p * 2 - 0.2)
    if a > 0:
        tw = hf.getlength(v["headline"].upper())
        d.text((hc[0] - tw / 2, hc[1] - hf.size / 2 - (1 - a) * 30), v["headline"].upper(), font=hf, fill=(15, 15, 15, int(255 * a)))
    if cache["sub"] is not None:
        paste_scaled(img, cache["sub"], hc[0], hc[1] + hf.size * 1.05, 1, ease(p * 2 - 0.6))
    if cache["val"] is not None:
        g = ease(p * 2 - 1.0)
        paste_scaled(img, cache["val"], vc[0], vc[1], 0.8 + 0.2 * g, g)
        if g > 0:
            vw = cache["val"].width
            y = vc[1] + cache["val"].height / 2 + 16 * U
            d.rectangle([vc[0] - vw / 2, y, vc[0] - vw / 2 + vw * g, y + 14 * U], fill=(60, 200, 90) if up else (220, 50, 50))
    return img


def render_visual(v, d, seg, i):
    fn = {"title": frame_title, "chart": frame_chart, "card": frame_card}.get(v.get("type"))
    if not fn:
        return False
    fdir = os.path.join(work, f"g{i}")
    os.makedirs(fdir, exist_ok=True)
    cache = {}
    anim = min(int(d * 30), 45)  # ~1.5s of build-up animation, then hold with a slow push-in
    try:
        for k in range(anim):
            fn(v, k / 30, k / max(1, anim - 1), cache).convert("RGB").save(os.path.join(fdir, f"f{k:03d}.png"))
    except Exception as e:
        print("Graphic failed, using footage:", e)
        return False
    hold = max(0.0, d - anim / 30)
    run(["ffmpeg", "-y", "-framerate", "30", "-i", os.path.join(fdir, "f%03d.png"), "-t", f"{d:.2f}",
         "-vf", f"tpad=stop_mode=clone:stop_duration={hold + 0.1:.2f},scale={W}:{H},zoompan=z='min(zoom+0.0004,1.05)':d=1:s={W}x{H}:fps=30,setsar=1",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
    return True

t0 = 0.0
for i, sc in enumerate(plan["scenes"]):
    text = sc["text"].strip()
    a = os.path.join(work, f"a{i}.mp3"); speak(text, a)
    d = duration(a) + 0.25
    # Mix: first 2 scenes always video; afterwards every 3rd scene is a photo (~70% video)
    seg = os.path.join(work, f"s{i}.mp4")
    vis = sc.get("visual")
    if vis:
        vis = dict(vis, query=sc.get("query", ""))
    graphic = bool(vis) and render_visual(vis, d, seg, i)
    want_photo = UNSPLASH and i >= 2 and i % 3 == 2
    link, author = (None, None) if (want_photo or graphic) else find_clip(sc.get("query", plan.get("fallback_query", "city")), d)
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,setsar=1"
    if graphic:
        pass
    elif link:
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
Style: Hook,DejaVu Sans,{int(H * 0.075)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,5,2,8,40,40,{int(H * 0.06)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(events) + "\n"
open(os.path.join(work, "subs.ass"), "w", encoding="utf-8").write(ass)

# brand frame (orange border, channel name, flag) overlaid on the whole video when present
frame = str(plan.get("frame") or "classic")
ov = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"frame_{frame}.png")
fcolor = str(plan.get("frame_color") or "").lstrip("#")
if frame != "none" and os.path.exists(ov) and len(fcolor) == 6:
    # recolor the orange parts of the frame to the chosen color (keeps shading, text and flag)
    import colorsys
    from PIL import Image
    th, ts, _tv = colorsys.rgb_to_hsv(*(int(fcolor[i:i + 2], 16) / 255 for i in (0, 2, 4)))
    im = Image.open(ov).convert("RGBA")
    px = im.load()
    for yy in range(im.height):
        for xx in range(im.width):
            r, g, b, a = px[xx, yy]
            if a == 0:
                continue
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if s > 0.45 and v > 0.3 and 0.0 <= h <= 0.12:
                nr, ng, nb = colorsys.hsv_to_rgb(th, ts, v)
                px[xx, yy] = (int(nr * 255), int(ng * 255), int(nb * 255), a)
    ov = os.path.join(work, "frame_colored.png")
    im.save(ov)
if frame != "none" and os.path.exists(ov):
    run(["ffmpeg", "-y", "-i", os.path.join(work, "video.mp4"), "-i", os.path.join(work, "voice.wav"), "-i", ov,
         "-filter_complex", f"[0:v]subtitles={os.path.join(work, 'subs.ass')}[s];[2:v]scale={W}:{H}[fr];[s][fr]overlay=0:0[v]",
         "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out])
else:
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
