"""stock-engine v35 (installed by Studio)
Builds a video from Pexels clips + narration + burned-in subtitles.
Usage: PLAN=<base64 json> python render_stock_video.py out.mp4
"""
import base64, json, os, random, re, subprocess, sys, tempfile, time
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
HF_ID = os.environ.get("HIGGSFIELD_KEY_ID", "").strip()
HF_SECRET = os.environ.get("HIGGSFIELD_KEY_SECRET", "").strip()
HF = bool(HF_ID and HF_SECRET)
HF_TTS = os.environ.get("HIGGSFIELD_TTS_MODEL", "").strip().strip("/") or "minimax/speech-2.8-hd"
HF_MUSIC = os.environ.get("HIGGSFIELD_MUSIC_MODEL", "").strip().strip("/") or "elevenlabs/music"
HF_VOICE = os.environ.get("HIGGSFIELD_VOICE_ID", "").strip()
PREMIUM = os.environ.get("PREMIUM_VOICE", "true") != "false"
VOICE_PROVIDER = plan.get("voice_provider", "elevenlabs")
AUDIO_PROVIDER = plan.get("audio_provider", "elevenlabs")
USE_HF_VOICE = PREMIUM and VOICE_PROVIDER == "higgsfield" and HF
USE_EL = EL_KEY and PREMIUM and not USE_HF_VOICE
USE_HF_AUDIO = AUDIO_PROVIDER == "higgsfield" and HF


def hf_generate(model, body, path):
    """Higgsfield async API: submit, poll status, download the audio output."""
    h = {"Authorization": f"Key {HF_ID}:{HF_SECRET}", "Content-Type": "application/json"}
    r = requests.post(f"https://platform.higgsfield.ai/{model}", headers=h, json=body, timeout=60)
    r.raise_for_status()
    j = r.json()
    url = j.get("status_url") or f"https://platform.higgsfield.ai/requests/{j['request_id']}/status"
    for _ in range(120):
        time.sleep(3)
        s = requests.get(url, headers=h, timeout=30).json()
        st = s.get("status")
        if st == "completed":
            out = s.get("audio") or (s.get("audios") or [None])[0] or s.get("video") or {}
            data = requests.get(out["url"], timeout=120).content
            open(path + ".src", "wb").write(data)
            run(["ffmpeg", "-y", "-i", path + ".src", path])
            return True
        if st in ("failed", "nsfw", "canceled"):
            raise RuntimeError(f"Higgsfield {st}: {s.get('error')}")
    raise RuntimeError("Higgsfield timed out")
EL_VOICE = str(plan.get("voice_id") or "").strip() or os.environ.get("ELEVENLABS_VOICE_ID", "").strip() or "pNInz6obpgDQGcFmaJgB"
EDGE_FEMALE = {"es": "es-MX-DaliaNeural", "en": "en-US-JennyNeural", "pt": "pt-BR-FranciscaNeural", "fr": "fr-FR-DeniseNeural"}
if plan.get("voice_gender") == "female":
    EDGE = EDGE_FEMALE


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).strip())


def speak(text, path):
    global USE_EL, USE_HF_VOICE
    if USE_HF_VOICE:
        try:
            body = {"text": text, "prompt": text}
            if HF_VOICE:
                body["voice_id"] = HF_VOICE
            hf_generate(HF_TTS, body, path)
            return
        except Exception as e:
            print("Higgsfield voice failed, trying next voice:", e)
            USE_HF_VOICE = False
            USE_EL = bool(EL_KEY and PREMIUM)
    if USE_EL:
        try:
            r = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE}",
                headers={"xi-api-key": EL_KEY, "Content-Type": "application/json"},
                # turbo v2.5: ~half the credits of multilingual v2, still studio-quality for narration
                json={"text": text, "model_id": "eleven_turbo_v2_5", "voice_settings": {"stability": 0.45, "similarity_boost": 0.8}},
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
# Cross-video memory: clips/photos shown in earlier videos are avoided when
# any fresh match exists, so each video looks new.
import atexit
HIST_PATH = "/tmp/used-media/used.json"
try:
    _hist_list = json.load(open(HIST_PATH))
except Exception:
    _hist_list = []
history = set(_hist_list)


def hkey(k):
    return k if isinstance(k, str) else f"{k[0]}:{k[1]}" if isinstance(k, tuple) else f"px:{k}"


def seen(k):
    return hkey(k) in history


@atexit.register
def _save_history():
    try:
        os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
        new = [hkey(k) for k in used if hkey(k) not in history]
        json.dump((_hist_list + new)[-4000:], open(HIST_PATH, "w"))
    except Exception as e:
        print("history save failed", e)

# Stock libraries have incomplete geographic metadata. Never infer anyone's
# nationality from appearance; use search context and explicit location clues.
FOREIGN_WORDS = {"india", "indian", "mumbai", "delhi", "bangalore", "london", "england", "united kingdom", "paris", "france", "berlin", "germany", "tokyo", "japan", "china", "beijing", "dubai", "uae", "brazil", "sao paulo", "mexico", "canada", "toronto", "australia", "sydney", "russia", "moscow", "pakistan", "indonesia", "italy", "rome", "spain", "madrid", "south africa", "africa", "europe", "asia"}
GENERIC_US = {"usa", "us", "american", "america", "united states"}
STATES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming"}
CITIES = {"los angeles", "las vegas", "chicago", "miami", "boston", "philadelphia", "atlanta", "seattle", "detroit", "phoenix", "san francisco", "houston", "denver", "dallas", "austin", "san diego", "san jose", "portland", "nashville", "orlando", "tampa", "baltimore", "pittsburgh", "cleveland", "minneapolis", "st louis", "new orleans", "salt lake city", "charlotte", "raleigh", "kansas city", "sacramento", "burlington", "manhattan", "brooklyn", "honolulu", "anchorage", "albuquerque", "tucson", "columbus", "indianapolis", "milwaukee", "memphis", "louisville", "richmond", "hartford", "providence", "newark", "oakland", "san antonio", "el paso", "omaha", "boise", "reno", "spokane", "silicon valley", "hollywood", "times square", "new england", "midwest"}
US_WORDS = GENERIC_US | STATES | CITIES
# A named place may also show close neighbours (e.g. Vermont -> New England / Boston).
NEAR = {"vermont": {"new england", "burlington", "boston", "new hampshire", "maine"}, "new hampshire": {"new england", "boston", "vermont", "maine"},
        "maine": {"new england", "portland"}, "massachusetts": {"boston", "new england"}, "boston": {"massachusetts", "new england"},
        "new york": {"manhattan", "brooklyn", "times square"}, "california": {"los angeles", "san francisco", "san diego", "hollywood", "silicon valley", "oakland", "sacramento", "san jose"},
        "texas": {"dallas", "houston", "austin", "san antonio", "el paso"}, "florida": {"miami", "orlando", "tampa"}, "washington": {"seattle", "spokane"}}
# Specific subjects must be present literally (or a true synonym); generic words like "car" alone never qualify.
ANCHORS = {"dealership": {"dealership", "dealer", "dealers", "showroom", "dealerships"}, "dealer": {"dealership", "dealer", "showroom"},
           "showroom": {"showroom", "dealership", "dealer"}, "hospital": {"hospital", "clinic", "emergency", "medical"},
           "supermarket": {"supermarket", "grocery", "groceries", "store"}, "grocery": {"grocery", "groceries", "supermarket"},
           "groceries": {"grocery", "groceries", "supermarket"}, "pharmacy": {"pharmacy", "drugstore", "pharmacist"},
           "restaurant": {"restaurant", "diner", "cafe"}, "factory": {"factory", "manufacturing", "plant", "assembly"},
           "school": {"school", "classroom", "students"}, "university": {"university", "college", "campus"},
           "homeless": {"homeless", "homelessness", "tent", "encampment"}, "gas": {"gas", "fuel", "pump", "gasoline"},
           "mortgage": {"mortgage", "house", "home", "realtor"}, "warehouse": {"warehouse", "logistics"}}
MED = {"doctor", "doctors", "physician", "surgeon", "nurse", "nurses", "patient", "patients", "hospital", "clinic", "medical", "healthcare", "health", "medicine", "stethoscope", "surgery"}
for _w in ("doctor", "doctors", "physician", "physicians", "surgeon", "surgeons", "nurse", "nurses", "patient", "patients", "clinic", "medical", "healthcare"):
    ANCHORS[_w] = MED
ANCHORS["taxi"] = {"taxi", "cab", "taxicab", "rideshare"}
# Dental scenes must show a dentist/dental office/teeth, never a generic doctor or money shot.
DENTAL = {"dentist", "dentists", "dental", "teeth", "tooth", "orthodontist", "braces", "hygienist", "dentistry", "smile"}
for _w in ("dentist", "dentists", "dental", "teeth", "tooth", "orthodontist", "braces", "hygienist", "dentistry"):
    ANCHORS[_w] = DENTAL
MED |= DENTAL
# Medical/insurance scenes never show street traffic unless the script is about ambulances.
MED_TRIGGERS = MED | {"insurance", "hospital", "pharmacy"}
MED_BLOCK = {"taxi", "cab", "taxicab", "traffic", "pedestrian", "pedestrians", "vehicle", "vehicles", "car", "cars", "street", "highway", "road", "money", "cash", "dollar", "dollars", "banknote", "banknotes"}
STOP = {"the", "a", "an", "of", "and", "in", "on", "for", "to", "with", "from", "usa", "us", "american", "america", "united", "states", "video", "photo", "footage", "cinematic", "slow", "motion", "drone", "aerial", "orbit", "360", "timelapse", "exterior", "interior", "people", "view", "shot", "close", "up"}
WORK = {"worker", "workers", "working", "work", "job", "jobs", "employee", "employees", "employment", "unemployment", "hiring", "interview", "office", "construction", "factory", "warehouse", "cashier", "retail", "staff", "team", "meeting", "laborer", "builder", "nurse", "doctor", "teacher", "chef", "waiter", "driver", "mechanic", "engineer", "business", "businessman", "businesswoman", "manager", "family", "shopper", "shopping", "customer", "store", "supermarket", "grocery", "kitchen", "home", "student", "students", "farmer", "farm", "trucker", "delivery", "patient", "clinic", "hospital"} | DENTAL


def has_word(w, text):
    return re.search(r"\b" + re.escape(w) + r"\b", text) is not None


def named_places(q):
    q = q.lower()
    found = {w for w in (STATES | CITIES) if has_word(w, q)}
    # "new york" contains "york"; drop places contained in longer matches
    return {w for w in found if not any(w != o and w in o for o in found)}


def extract_words(s):
    return set(re.findall(r"[a-z0-9]+", str(s).lower()))


def subject(q):
    place_words = set()
    for p in named_places(q):
        place_words |= set(p.split())
    return extract_words(q) - STOP - place_words


def search_terms(q):
    q = str(q or "").strip()[:85]
    if not q or not subject(q):
        return []
    has_us = any(has_word(w, q.lower()) for w in US_WORDS)
    return [q] if has_us else [q + " USA", q + " United States"]


def candidate_ok(query, metadata, location=""):
    text = str(metadata or "").lower().replace("-", " ")
    place = str(location or "").lower()
    both = text + " " + place
    if any(has_word(w, both) for w in FOREIGN_WORDS):
        return False
    # Every result came from a US-only search ("... USA"). Places still need an explicit US clue;
    # people/work scenes (rarely tagged "USA") pass when the result matches the work subject
    # and carries no foreign tag, so videos show real American workers instead of only dollar bills.
    if not any(has_word(w, both) for w in US_WORDS):
        if named_places(query) or not (subject(query) & WORK) or not (extract_words(both) & WORK):
            return False
    if place and not any(has_word(w, place) for w in US_WORDS):
        return False
    # Named state/city: the clip must show that place (or a close neighbour), never another US city.
    places = named_places(query)
    if places:
        ok = set(places)
        for p in places:
            ok |= NEAR.get(p, set())
        if not any(has_word(w, both) for w in ok):
            return False
    words = extract_words(both)
    terms = subject(query)
    qw = extract_words(query)
    if (qw & MED_TRIGGERS) and not (qw & {"ambulance", "paramedic", "emergency"}) and (words & MED_BLOCK):
        return False
    # Exact subject: anchor nouns (dealership, hospital...) must appear themselves or as a true synonym.
    for t in terms:
        if t in ANCHORS and not (ANCHORS[t] & words):
            return False
    if not terms:
        return True
    hit = len(terms & words)
    # Multi-word subjects need at least two matching words, so "car" alone can't stand in for "car dealership".
    return hit >= min(2, len(terms))



# ---- Visual AI check (CLIP, free, runs on the GitHub machine): looks at each thumbnail and scores how well
# it actually shows the scene, so a "cars" sentence never gets a photo camera just because of a bad tag.
_clip = None
def clip_model():
    global _clip
    if _clip is None:
        try:
            from transformers import CLIPModel, CLIPProcessor
            _clip = (CLIPModel.from_pretrained("openai/clip-vit-base-patch32").eval(), CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32"))
        except Exception as e:
            print("Visual AI check unavailable:", e); _clip = False
    return _clip


NEG = ["a camera", "a smartphone screen", "text on a page", "an abstract background", "a logo", "a cartoon illustration", "an empty sky"]


def clip_rank(query, items, thumb):
    """items: candidates already passing tag rules. Returns them best-first, dropping ones that don't show the subject."""
    m = clip_model()
    if not m or not items:
        return items
    try:
        import io, torch
        from PIL import Image
        model, proc = m
        subj = " ".join(sorted(subject(query))) or query
        prompts = [f"a photo of {query}", f"a photo of {subj}"] + [f"a photo of {n}" for n in NEG if not (extract_words(n) & subject(query))]
        imgs, keep = [], []
        for it in items[:10]:
            try:
                u = thumb(it)
                if not u: continue
                imgs.append(Image.open(io.BytesIO(requests.get(u, timeout=20).content)).convert("RGB")); keep.append(it)
            except Exception:
                continue
        if not imgs:
            return items
        with torch.no_grad():
            out = model(**proc(text=prompts, images=imgs, return_tensors="pt", padding=True))
            probs = out.logits_per_image.softmax(dim=1)
            sims = out.logits_per_image / model.logit_scale.exp()
        scored = []
        for k, it in enumerate(keep):
            on = float(probs[k][0] + probs[k][1]); sim = float(max(sims[k][0], sims[k][1]))
            if on >= 0.5 and sim >= 0.22:
                scored.append((-(sim + 0.05 * on), it))
            else:
                print(f"Visual AI rejected a result for '{query}' (match {sim:.2f})")
        scored.sort(key=lambda x: x[0])
        return [it for _, it in scored]
    except Exception as e:
        print("Visual AI check skipped:", e)
        return items


def find_photo(query):
    srcs = [f for f, k in ((find_unsplash, UNSPLASH), (find_pixabay_photo, PIXABAY)) if k]
    random.shuffle(srcs)
    for f in srcs:
        try:
            url, author = f(query)
        except Exception as e:
            print("Photo search error", e); continue
        if url:
            return url, author
    return None, None


def find_pixabay_photo(query):
    for q in search_terms(query):
        r = requests.get("https://pixabay.com/api/", params={"key": PIXABAY, "q": q[:100], "image_type": "photo", "per_page": 30,
                         "orientation": "vertical" if vertical else "horizontal", "safesearch": "true", "min_width": 1280,
                         "order": random.choice(["popular", "latest"])}, timeout=30)
        if r.status_code != 200:
            print("Pixabay photo error", r.status_code, r.text[:200]); continue
        hits = [h for h in r.json().get("hits", []) if ("pbi", h["id"]) not in used and candidate_ok(query, h.get("tags", ""))]
        hits.sort(key=lambda h: (seen(("pbi", h["id"])), random.random()))
        hits = clip_rank(query, hits, lambda h: h.get("webformatURL") or h.get("previewURL"))
        for h in hits:
            url = h.get("largeImageURL") or h.get("webformatURL")
            if not url: continue
            used.add(("pbi", h["id"]))
            return url, (h.get("user") or "Pixabay") + " (Pixabay)"
    return None, None


def find_unsplash(query):
    orient = "portrait" if vertical else "landscape"
    for q in search_terms(query):
        r = requests.get("https://api.unsplash.com/search/photos", headers={"Authorization": f"Client-ID {UNSPLASH}"},
                         params={"query": q, "orientation": orient, "per_page": 15, "content_filter": "high"}, timeout=30)
        if r.status_code != 200:
            print("Unsplash error", r.status_code, r.text[:200]); continue
        res = r.json().get("results", [])
        res.sort(key=lambda ph: (seen(("us", ph["id"])), random.random()))
        def _ok(ph):
            metadata = " ".join(str(ph.get(k) or "") for k in ("description", "alt_description", "slug")) + " " + " ".join(str(t.get("title") or "") for t in ph.get("tags", []))
            return ("us", ph["id"]) not in used and candidate_ok(query, metadata, ph.get("location", {}).get("name") if isinstance(ph.get("location"), dict) else "")
        res = clip_rank(query, [ph for ph in res if _ok(ph)], lambda ph: ph.get("urls", {}).get("small"))
        for ph in res:
            used.add(("us", ph["id"]))
            try:  # Unsplash guidelines: report the download
                requests.get(ph["links"]["download_location"], headers={"Authorization": f"Client-ID {UNSPLASH}"}, timeout=15)
            except Exception:
                pass
            return ph["urls"]["raw"] + f"&w={W * 2}&fit=max&q=85", ph.get("user", {}).get("name", "Unsplash")
    return None, None


def find_pixabay(query, need):
    if not PIXABAY:
        return None, None
    for q in search_terms(query):
        try:
            r = requests.get("https://pixabay.com/api/videos/", params={"key": PIXABAY, "q": q[:100], "per_page": 30,
                             "safesearch": "true", "order": random.choice(["popular", "latest"])}, timeout=30)
        except Exception as e:
            print("Pixabay error", e); continue
        if r.status_code != 200:
            print("Pixabay error", r.status_code, r.text[:200]); continue
        hits = []
        for v in r.json().get("hits", []):
            if ("pb", v["id"]) in used or not candidate_ok(query, v.get("tags", "")): continue
            vs = v.get("videos", {})
            opts = [f for f in (vs.get("large"), vs.get("medium"), vs.get("small")) if f and f.get("url") and f.get("width")]
            if not opts: continue
            f = opts[0]
            is_v = f["height"] > f["width"]
            hits.append((is_v != vertical, seen(("pb", v["id"])), v.get("duration", 0) < need, random.random(), v, f))
        hits.sort(key=lambda h: h[:4])
        hits = clip_rank(query, hits, lambda h: (h[4].get("videos", {}).get("medium") or {}).get("thumbnail") or (h[4].get("videos", {}).get("small") or {}).get("thumbnail"))
        if hits:
            v, f = hits[0][4], hits[0][5]
            used.add(("pb", v["id"]))
            return f["url"], (v.get("user") or "Pixabay") + " (Pixabay)"
    return None, None


def find_clip(query, need):
    link, author = find_pixabay(query, need)
    if link or not PEXELS:
        return link, author
    orient = "portrait" if vertical else "landscape"
    for q in search_terms(query):
        r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": PEXELS},
                         params={"query": q, "orientation": orient, "per_page": 15, "size": "medium"}, timeout=30)
        if r.status_code != 200:
            print("Pexels error", r.status_code, r.text[:200]); continue
        vids = [v for v in r.json().get("videos", []) if ("px", v["id"]) not in used and candidate_ok(query, v.get("url", "").split("/video/")[-1])]
        vids.sort(key=lambda v: (seen(("px", v["id"])), v.get("duration", 0) < need, random.random()))
        vids = clip_rank(query, vids, lambda v: v.get("image"))
        for v in vids:
            files = [f for f in v["video_files"] if f.get("file_type") == "video/mp4" and f.get("height")]
            if not files: continue
            target = H if vertical else W
            files.sort(key=lambda f: abs((f["height"] if vertical else f["width"]) - target))
            used.add(("px", v["id"]))
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
    # Semi-transparent tint: graphics sit on top of (darkened) footage, never a solid background.
    im = sm.resize((W, H), Image.BICUBIC).convert("RGBA")
    im.putalpha(95)
    return im


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
    return Image.merge("RGBA", (base, base, base.point(lambda v: min(255, v + 2)), Image.new("L", (W, H), 190)))


def object_image(desc, query):
    """Cut-out illustration of the item (OpenAI, transparent bg) or a polaroid photo as a free fallback."""
    if OPENAI and desc:
        try:
            r = requests.post("https://api.openai.com/v1/images/generations",
                              headers={"Authorization": f"Bearer {OPENAI}", "Content-Type": "application/json"},
                              json={"model": IMG_MODEL, "size": "1024x1024", "quality": "medium", "background": "transparent",
                                    "output_format": "png", "n": 1,
                                    "prompt": f"Photorealistic product photo of {desc}, as sold in the United States, isolated, centered, full object visible, "
                                              "American packaging if relevant, studio lighting, soft shadow, transparent background, no text, no logos."},
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
    var = v.get("_i", 0) % 3
    head = v["headline"].upper()
    if "bg" not in cache:
        cache["bg"] = radial(*BG.get(v.get("color") or "red", BG["red"]))
        cache["lab"] = label(head, 92 * U, W * 0.86)
        ws = head.split()
        cut = max(1, len(ws) // 2) if len(ws) > 3 else len(ws)
        cache["lines"] = [x for x in (" ".join(ws[:cut]), " ".join(ws[cut:])) if x]
        cache["big"] = fit(FB, max(cache["lines"], key=len), W * 0.74, 150 * U)
        cache["kick"] = fit(FSERIF, v.get("sub") or "", W * 0.6, 46 * U) if v.get("sub") else None
        cache["band"] = fit(FB, head, W * 0.84, 110 * U)
    img = cache["bg"].convert("RGBA")
    if var == 0:
        grid(img, t)
        paste_scaled(img, cache["lab"], W / 2, H / 2, 0.82 + 0.18 * ease(p * 1.4), ease(p * 2))
        return img
    if var == 1:
        # Editorial: left-aligned stacked headline with an orange rule
        grid(img, t, 35)
        d = ImageDraw.Draw(img)
        x0, f = W * 0.12, cache["big"]
        lines = cache["lines"]
        ytop = H / 2 - len(lines) * f.size * 0.6
        bh = len(lines) * f.size * 1.2 * ease(p * 1.6)
        d.rectangle([x0 - 40 * U, ytop, x0 - 26 * U, ytop + bh], fill=ACC)
        for k, ln in enumerate(lines):
            g = ease(p * 1.8 - 0.2 - k * 0.2)
            d.text((x0 - (1 - g) * 60 * U, ytop + k * f.size * 1.2), ln, font=f, fill=(255, 255, 255, int(255 * g)))
        if cache["kick"] is not None:
            g = ease(p * 2 - 1.0)
            d.text((x0, ytop + len(lines) * f.size * 1.2 + 20 * U), v["sub"], font=cache["kick"], fill=(235, 200, 180, int(255 * g)))
        return img
    # Band wipe: an orange band sweeps across with the headline
    grid(img, t, 25)
    d = ImageDraw.Draw(img)
    f = cache["band"]
    bh = f.size * 1.9
    g = ease(p * 1.7)
    d.rectangle([0, H / 2 - bh / 2, W * g, H / 2 + bh / 2], fill=ACC)
    d.rectangle([0, H / 2 + bh / 2, W * ease(p * 1.7 - 0.15), H / 2 + bh / 2 + 10 * U], fill=(20, 20, 20))
    ta = ease(p * 2 - 0.5)
    tw = f.getlength(head)
    d.text((W / 2 - tw / 2 + (1 - ta) * 80 * U, H / 2 - f.size * 0.62), head, font=f, fill=(255, 255, 255, int(255 * ta)))
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


ACC = (232, 93, 42, 255)
_VIG = {}


def vignette():
    if "v" not in _VIG:
        sm = Image.new("L", (64, 36))
        for y in range(36):
            for x in range(64):
                dd = math.hypot((x - 32) / 32, (y - 18) / 18)
                sm.putpixel((x, y), int(min(1.0, max(0.0, dd - 0.55) / 0.8) * 170))
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ov.putalpha(sm.resize((W, H), Image.BICUBIC))
        _VIG["v"] = ov
    return _VIG["v"]


def parse_num(s):
    import re as _re
    m = _re.search(r"\d[\d,]*(?:\.\d+)?", s or "")
    if not m:
        return None
    raw = m.group(0)
    dec = len(raw.split(".")[1]) if "." in raw else 0
    return s[:m.start()], float(raw.replace(",", "")), dec, s[m.end():]


def frame_stat(v, t, p, cache):
    if "bg" not in cache:
        cache["bg"] = radial(*BG.get(v.get("color") or "dark", BG["dark"]))
        cache["num"] = parse_num(v.get("value") or "")
        cache["f"] = fit(FB, (v.get("value") or "") + "0", W * 0.8, 260 * U)
        cache["lab"] = label(v["headline"].upper(), 54 * U, W * 0.7, bg=ACC)
        cache["sub"] = fit(FSERIF, v.get("sub") or "", W * 0.6, 48 * U) if v.get("sub") else None
    img = cache["bg"].convert("RGBA")
    grid(img, t, 30)
    d = ImageDraw.Draw(img)
    pn, g, a = cache["num"], ease(p * 1.3), ease(p * 2)
    txt = f"{pn[0]}{pn[1] * g:,.{pn[2]}f}{pn[3]}" if pn else (v.get("value") or "")
    f = cache["f"]
    tw = f.getlength(txt)
    y = H * 0.42
    d.text((W / 2 - tw / 2, y - f.size / 2 + (1 - a) * 40 * U), txt, font=f, fill=(255, 255, 255, int(255 * a)))
    lw = W * 0.3 * ease(p * 1.5 - 0.3)
    if lw > 0:
        uy = y + f.size * 0.62
        d.rectangle([W / 2 - lw / 2, uy, W / 2 + lw / 2, uy + 10 * U], fill=(60, 200, 90) if v.get("trend") != "down" else (220, 50, 50))
    paste_scaled(img, cache["lab"], W / 2, H * 0.73, 0.9 + 0.1 * ease(p * 2 - 0.6), ease(p * 2 - 0.6))
    if cache["sub"] is not None:
        sa = ease(p * 2 - 0.9)
        sw = cache["sub"].getlength(v["sub"])
        d.text((W / 2 - sw / 2, H * 0.82), v["sub"], font=cache["sub"], fill=(220, 220, 225, int(255 * sa)))
    return img


def frame_list(v, t, p, cache):
    items = (v.get("items") or [])[:4]
    head = v["headline"].upper()
    if "bg" not in cache:
        cache["bg"] = radial(*BG.get(v.get("color") or "dark", BG["dark"]))
        cache["hf"] = fit(FB, head, W * 0.8, 84 * U)
        cache["rows"] = [label(it, 52 * U, W * 0.62, fg=(20, 20, 20), bg=(245, 244, 238, 245)) for it in items]
        cache["nf"] = ImageFont.truetype(FB, int(46 * U))
    img = cache["bg"].convert("RGBA")
    grid(img, t, 28)
    d = ImageDraw.Draw(img)
    hf, a, x0 = cache["hf"], ease(p * 2), W * 0.12
    hy = H * 0.14
    d.text((x0, hy - (1 - a) * 30 * U), head, font=hf, fill=(255, 255, 255, int(255 * a)))
    d.rectangle([x0, hy + hf.size * 1.25, x0 + W * 0.12 * a, hy + hf.size * 1.25 + 10 * U], fill=ACC)
    rows = cache["rows"]
    top, gap = H * 0.36, H * 0.56 / max(1, len(rows))
    for k, row in enumerate(rows):
        g = ease(p * 1.8 - 0.25 - k * 0.18)
        if g <= 0:
            continue
        cy = top + gap * k + gap / 2
        r = 38 * U
        cx = x0 + r - (1 - g) * W * 0.15
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACC)
        num = str(k + 1)
        nw = cache["nf"].getlength(num)
        d.text((cx - nw / 2, cy - cache["nf"].size * 0.6), num, font=cache["nf"], fill=(255, 255, 255))
        paste_scaled(img, row, cx + r + 30 * U + row.width / 2, cy, 1, g)
        d = ImageDraw.Draw(img)
    return img


def frame_compare(v, t, p, cache):
    head = v["headline"].upper()
    if "bg" not in cache:
        cache["bg"] = radial(*BG.get(v.get("color") or "dark", BG["dark"]))
        cache["hf"] = fit(FB, head, W * 0.8, 80 * U)
        cache["L"] = label((v.get("left") or "BEFORE").upper(), 50 * U, W * 0.34, bg=(80, 80, 92, 240))
        cache["R"] = label((v.get("right") or "NOW").upper(), 50 * U, W * 0.34, bg=ACC)
        cache["vf"] = fit(FB, max(v.get("left_value") or "", v.get("right_value") or "", key=len), W * 0.34, 170 * U)
        cache["vs"] = ImageFont.truetype(FB, int(54 * U))
    img = cache["bg"].convert("RGBA")
    grid(img, t, 25)
    d = ImageDraw.Draw(img)
    a = ease(p * 2)
    hf = cache["hf"]
    hw = hf.getlength(head)
    d.text((W / 2 - hw / 2, H * 0.1 - (1 - a) * 30 * U), head, font=hf, fill=(255, 255, 255, int(255 * a)))
    y0, y1 = H * 0.3, H * 0.86
    for side, key, val, fill, delay in ((-1, "L", v.get("left_value") or "", (34, 34, 42), 0.1), (1, "R", v.get("right_value") or "", (70, 28, 14), 0.35)):
        g = ease(p * 1.8 - delay)
        if g <= 0:
            continue
        cx = W / 2 + side * W * 0.23 + side * (1 - g) * W * 0.4
        d.rounded_rectangle([cx - W * 0.2, y0, cx + W * 0.2, y1], radius=int(24 * U), fill=fill, outline=(255, 255, 255) if side > 0 else (120, 120, 130), width=max(2, int(3 * U)))
        paste_scaled(img, cache[key], cx, y0 + 70 * U, 1, g)
        d = ImageDraw.Draw(img)
        vw = cache["vf"].getlength(val)
        d.text((cx - vw / 2, (y0 + y1) / 2 - cache["vf"].size * 0.35), val, font=cache["vf"], fill=(255, 255, 255, int(255 * g)))
    gv = ease(p * 2 - 1.0)
    if gv > 0:
        r = 56 * U * gv
        d.ellipse([W / 2 - r, (y0 + y1) / 2 - r, W / 2 + r, (y0 + y1) / 2 + r], fill=(255, 255, 255))
        if gv > 0.6:
            sw = cache["vs"].getlength("VS")
            d.text((W / 2 - sw / 2, (y0 + y1) / 2 - cache["vs"].size * 0.6), "VS", font=cache["vs"], fill=(20, 20, 20))
    return img


def Fnt(path, size):
    return ImageFont.truetype(path, max(12, int(size)))


def wrap(text, f, maxw, maxl=4):
    lines, cur = [], ""
    for w_ in (text or "").split():
        t2 = (cur + " " + w_).strip()
        if not cur or f.getlength(t2) <= maxw:
            cur = t2
        else:
            lines.append(cur); cur = w_
    if cur:
        lines.append(cur)
    return lines[:maxl]


def ctext(d, text, f, cx, y, fill):
    d.text((cx - f.getlength(text) / 2, y), text, font=f, fill=fill)


def kv(s):
    if ":" in s:
        a_, b_ = s.split(":", 1)
        return a_.strip(), b_.strip()
    return s.strip(), ""


def base(v, cache, t, col="dark", alpha=28):
    if "base" not in cache:
        cache["base"] = radial(*BG.get(v.get("color") or col, BG[col]))
    img = cache["base"].convert("RGBA")
    grid(img, t, alpha)
    return img, ImageDraw.Draw(img)


def headline_tl(d, v, p, size=72):
    f = fit(FB, v["headline"].upper(), W * 0.8, size * U)
    a = ease(p * 2)
    d.text((W * 0.08, H * 0.09 - (1 - a) * 30 * U), v["headline"].upper(), font=f, fill=(255, 255, 255, int(255 * a)))
    d.rectangle([W * 0.08, H * 0.09 + f.size * 1.2, W * 0.08 + W * 0.1 * a, H * 0.09 + f.size * 1.2 + 9 * U], fill=ACC)


def frame_line(v, t, p, cache):
    img, d = base(v, cache, t, "blue", 35)
    headline_tl(d, v, p)
    up = v.get("trend") != "down"
    x0, x1, y0, y1 = W * 0.1, W * 0.88, H * 0.32, H * 0.86
    d.line([(x0, y1), (x1, y1)], fill=(255, 255, 255, 120), width=max(2, int(3 * U)))
    pts = []
    for k in range(61):
        s = k / 60
        f_ = s ** 1.5 if up else (1 - s) ** 1.5
        pts.append((x0 + (x1 - x0) * s, y1 - (y1 - y0) * (0.12 + 0.75 * f_ + 0.04 * math.sin(s * 19))))
    vis = pts[: max(2, int(61 * ease(p * 1.4)))]
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ov).polygon(vis + [(vis[-1][0], y1), (x0, y1)], fill=(232, 93, 42, 70))
    img.alpha_composite(ov)
    d = ImageDraw.Draw(img)
    d.line(vis, fill=ACC, width=max(4, int(8 * U)), joint="curve")
    ex, ey = vis[-1]
    r = 14 * U
    d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=(255, 255, 255))
    if v.get("value") and p > 0.6:
        if "lab" not in cache:
            cache["lab"] = label(v["value"], 80 * U, W * 0.4, bg=ACC)
        lab = cache["lab"]
        paste_scaled(img, lab, min(W - lab.width / 2 - 20 * U, ex), max(lab.height, ey - lab.height), 1, ease((p - 0.6) * 3))
    return img


def frame_stat_split(v, t, p, cache):
    img, d = base(v, cache, t, "dark", 25)
    if "f" not in cache:
        cache["num"] = parse_num(v.get("value") or "")
        cache["f"] = fit(FB, (v.get("value") or "") + "0", W * 0.42, 230 * U)
        cache["hf"] = Fnt(FB, 84 * U)
        cache["sf"] = Fnt(FSERIF, 46 * U)
    d.rectangle([0, 0, W * 0.48 * ease(p * 1.5), H], fill=ACC)
    lines = wrap(v["headline"].upper(), cache["hf"], W * 0.38)
    hf, la = cache["hf"], ease(p * 2 - 0.4)
    y = H / 2 - len(lines) * hf.size * 0.6
    for k, ln in enumerate(lines):
        d.text((W * 0.06, y + k * hf.size * 1.2), ln, font=hf, fill=(255, 255, 255, int(255 * la)))
    pn, g, a = cache["num"], ease(p * 1.3 - 0.2), ease(p * 2 - 0.3)
    txt = f"{pn[0]}{pn[1] * g:,.{pn[2]}f}{pn[3]}" if pn else (v.get("value") or "")
    ctext(d, txt, cache["f"], W * 0.74, H / 2 - cache["f"].size * 0.6, (255, 255, 255, int(255 * a)))
    if v.get("sub"):
        ctext(d, v["sub"], cache["sf"], W * 0.74, H / 2 + cache["f"].size * 0.6, (230, 200, 185, int(255 * ease(p * 2 - 1))))
    return img


def frame_grid(v, t, p, cache):
    img, d = base(v, cache, t, "dark", 25)
    items = (v.get("items") or [])[:4]
    hf = fit(FB, v["headline"].upper(), W * 0.84, 80 * U)
    ctext(d, v["headline"].upper(), hf, W / 2, H * 0.1, (255, 255, 255, int(255 * ease(p * 2))))
    n = max(1, len(items))
    gap = W * 0.025
    cw = (W * 0.84 - gap * (n - 1)) / n
    tf, nf = Fnt(FB, 46 * U), Fnt(FB, 50 * U)
    for k, it in enumerate(items):
        g = ease(p * 1.8 - 0.2 - k * 0.15)
        if g <= 0:
            continue
        x = W * 0.08 + k * (cw + gap)
        y0 = H * 0.32 + (1 - g) * H * 0.3
        y1 = y0 + H * 0.5
        d.rounded_rectangle([x, y0, x + cw, y1], radius=int(22 * U), fill=(246, 244, 238))
        r = 46 * U
        cx = x + cw / 2
        d.ellipse([cx - r, y0 + 40 * U, cx + r, y0 + 40 * U + 2 * r], fill=ACC)
        ctext(d, str(k + 1), nf, cx, y0 + 40 * U + r - nf.size * 0.6, (255, 255, 255))
        for j, ln in enumerate(wrap(it, tf, cw * 0.84, 3)):
            ctext(d, ln, tf, cx, y0 + 40 * U + 2 * r + 40 * U + j * tf.size * 1.2, (25, 25, 25))
    return img


def frame_bars(v, t, p, cache, rows=None, highlight_last=True):
    img, d = base(v, cache, t, "dark", 25)
    headline_tl(d, v, p)
    rows = rows if rows is not None else [kv(s) for s in (v.get("items") or [])[:5]]
    nums = [(parse_num(b) or ("", 0.0, 0, ""))[1] for _, b in rows]
    mx = max(nums + [1e-9])
    lf, vf = Fnt(FB, 48 * U), Fnt(FB, 56 * U)
    top, gap = H * 0.34, H * 0.56 / max(1, len(rows))
    bh = min(gap * 0.55, 90 * U)
    for k, ((lab, val), n) in enumerate(zip(rows, nums)):
        g = ease(p * 1.7 - 0.2 - k * 0.15)
        if g <= 0:
            continue
        cy = top + gap * k + gap / 2
        d.text((W * 0.08, cy - lf.size * 0.6), lab.upper()[:22], font=lf, fill=(255, 255, 255, int(255 * g)))
        hi = (k == len(rows) - 1) if highlight_last else (k == 0)
        L = W * 0.46 * (n / mx if mx else 0.5) * g
        x0 = W * 0.34
        d.rounded_rectangle([x0, cy - bh / 2, x0 + max(L, 6 * U), cy + bh / 2], radius=int(10 * U), fill=ACC if hi else (120, 120, 136))
        d.text((x0 + L + 24 * U, cy - vf.size * 0.6), val, font=vf, fill=(255, 255, 255, int(255 * g)))
    return img


def frame_hbars(v, t, p, cache):
    rows = [(v.get("left") or "Before", v.get("left_value") or ""), (v.get("right") or "Now", v.get("right_value") or "")]
    return frame_bars(v, t, p, cache, rows, True)


def frame_ranking(v, t, p, cache):
    return frame_bars(v, t, p, cache, None, False)


def frame_percent(v, t, p, cache):
    img, d = base(v, cache, t, "dark", 25)
    pn = parse_num(v.get("value") or "")
    pct = min(1.0, (pn[1] / 100.0) if pn else 0.5)
    g = ease(p * 1.3)
    cx, cy, r = W * 0.3, H * 0.52, H * 0.3
    wd = max(10, int(42 * U))
    box = [cx - r, cy - r, cx + r, cy + r]
    d.arc(box, 0, 360, fill=(70, 70, 82), width=wd)
    if g > 0.01:
        d.arc(box, -90, -90 + 360 * pct * g, fill=ACC, width=wd)
    big = Fnt(FB, 150 * U)
    txt = f"{pn[0]}{pn[1] * g:,.{pn[2]}f}{pn[3]}" if pn else (v.get("value") or "")
    ctext(d, txt, big, cx, cy - big.size * 0.6, (255, 255, 255))
    hf, a = Fnt(FB, 78 * U), ease(p * 2 - 0.4)
    lines = wrap(v["headline"].upper(), hf, W * 0.38)
    y = cy - len(lines) * hf.size * 0.6
    for k, ln in enumerate(lines):
        d.text((W * 0.56, y + k * hf.size * 1.2), ln, font=hf, fill=(255, 255, 255, int(255 * a)))
    if v.get("sub"):
        d.text((W * 0.56, y + len(lines) * hf.size * 1.2 + 20 * U), v["sub"], font=Fnt(FSERIF, 46 * U), fill=(230, 200, 185, int(255 * ease(p * 2 - 1))))
    return img


def frame_timeline(v, t, p, cache):
    img, d = base(v, cache, t, "blue", 30)
    headline_tl(d, v, p)
    items = [kv(s) for s in (v.get("items") or [])[:5]]
    y = H * 0.58
    x0, x1 = W * 0.1, W * 0.9
    d.line([(x0, y), (x0 + (x1 - x0) * ease(p * 1.4), y)], fill=(255, 255, 255), width=max(3, int(6 * U)))
    yf, vf = Fnt(FB, 56 * U), Fnt(FB, 66 * U)
    n = max(1, len(items))
    for k, (a_, b_) in enumerate(items):
        g = ease(p * 1.8 - 0.25 - k * 0.15)
        if g <= 0:
            continue
        x = x0 + (x1 - x0) * (k + 0.5) / n
        r = 22 * U * (0.5 + 0.5 * g)
        d.ellipse([x - r, y - r, x + r, y + r], fill=ACC, outline=(255, 255, 255), width=max(2, int(4 * U)))
        ctext(d, a_, yf, x, y - 110 * U - (1 - g) * 30 * U, (235, 200, 180, int(255 * g)))
        if b_:
            ctext(d, b_, vf, x, y + 50 * U + (1 - g) * 30 * U, (255, 255, 255, int(255 * g)))
    return img


def frame_quote(v, t, p, cache):
    img, d = base(v, cache, t, "dark", 18)
    qf = Fnt(FSERIF, 360 * U)
    d.text((W * 0.07, H * 0.02), "“", font=qf, fill=ACC)
    tf = Fnt(FSERIF, 76 * U)
    lines = wrap(v["headline"], tf, W * 0.74)
    y = H / 2 - len(lines) * tf.size * 0.65
    for k, ln in enumerate(lines):
        g = ease(p * 1.8 - k * 0.2)
        d.text((W * 0.14, y + k * tf.size * 1.3 + (1 - g) * 20 * U), ln, font=tf, fill=(255, 255, 255, int(255 * g)))
    if v.get("sub"):
        d.text((W * 0.14, y + len(lines) * tf.size * 1.3 + 30 * U), "— " + v["sub"], font=Fnt(FB, 46 * U), fill=ACC)
    return img


def frame_alert(v, t, p, cache):
    img, d = base(v, cache, t, "red", 30)
    bf = Fnt(FB, 64 * U)
    blink = 255 if int(t * 3) % 2 == 0 else 150
    lab = "BREAKING"
    lw = bf.getlength(lab) + 60 * U
    d.rectangle([W * 0.06, H * 0.5, W * 0.06 + lw, H * 0.5 + bf.size * 1.6], fill=(255, 255, 255, blink))
    d.text((W * 0.06 + 30 * U, H * 0.5 + bf.size * 0.25), lab, font=bf, fill=(180, 20, 20))
    g = ease(p * 1.6)
    hf = fit(FB, v["headline"].upper(), W * 0.84, 96 * U)
    by = H * 0.5 + bf.size * 1.6
    d.rectangle([0, by, W * g, by + hf.size * 1.8], fill=(15, 15, 18))
    ta = ease(p * 2 - 0.4)
    d.text((W * 0.06 + (1 - ta) * 60 * U, by + hf.size * 0.38), v["headline"].upper(), font=hf, fill=(255, 255, 255, int(255 * ta)))
    if v.get("sub"):
        sf = fit(FB, v["sub"], W * 0.84, 48 * U)
        sy = by + hf.size * 1.8
        d.rectangle([0, sy, W * ease(p * 1.6 - 0.2), sy + sf.size * 1.7], fill=ACC)
        d.text((W * 0.06, sy + sf.size * 0.3), v["sub"], font=sf, fill=(255, 255, 255, int(255 * ease(p * 2 - 0.8))))
    return img


def frame_receipt(v, t, p, cache):
    img, d = base(v, cache, t, "dark", 22)
    rows = [kv(s) for s in (v.get("items") or [])[:5]]
    if "paper" not in cache:
        pw = int(W * 0.34)
        lf, hf = Fnt(FB, 44 * U), Fnt(FB, 58 * U)
        rh = int(lf.size * 2.1)
        ph = int(hf.size * 2.6 + rh * (len(rows) + 1.6) + 60 * U)
        pap = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        pd_ = ImageDraw.Draw(pap)
        z = int(14 * U)
        poly = [(x, z if (x // z) % 2 else 0) for x in range(0, pw + z, z)] + [(x, ph - (z if (x // z) % 2 else 0)) for x in range(pw, -z, -z)]
        pd_.polygon(poly, fill=(248, 246, 240, 255))
        ctext(pd_, v["headline"].upper()[:20], hf, pw / 2, z + hf.size * 0.5, (20, 20, 20))
        y = z + hf.size * 2.2
        for a_, b_ in rows:
            pd_.text((40 * U, y), a_[:18], font=lf, fill=(40, 40, 40))
            pd_.text((pw - 40 * U - lf.getlength(b_), y), b_, font=lf, fill=(40, 40, 40))
            pd_.line([(40 * U, y + lf.size * 1.5), (pw - 40 * U, y + lf.size * 1.5)], fill=(190, 190, 190), width=2)
            y += rh
        if v.get("value"):
            pd_.text((40 * U, y + 10 * U), "TOTAL", font=hf, fill=(200, 50, 30))
            pd_.text((pw - 40 * U - hf.getlength(v["value"]), y + 10 * U), v["value"], font=hf, fill=(200, 50, 30))
        cache["paper"] = pap
    pap = cache["paper"]
    show = max(2, int(pap.height * ease(p * 1.3)))
    part = pap.crop((0, 0, pap.width, show))
    img.alpha_composite(part, (int(W / 2 - pap.width / 2), int(H / 2 - pap.height / 2)))
    return img


def frame_myth(v, t, p, cache):
    img, d = base(v, cache, t, "dark", 22)
    tag, tf = Fnt(FB, 52 * U), Fnt(FB, 70 * U)
    for k, (word, col, text) in enumerate((("MYTH", (200, 40, 40), v.get("sub") or ""), ("FACT", (40, 170, 80), v["headline"]))):
        g = ease(p * 1.8 - k * 0.6)
        if g <= 0:
            continue
        y = H * (0.25 + 0.32 * k)
        tw = tag.getlength(word) + 50 * U
        x = W * 0.08 - (1 - g) * 80 * U
        d.rounded_rectangle([x, y, x + tw, y + tag.size * 1.6], radius=int(10 * U), fill=col)
        d.text((x + 25 * U, y + tag.size * 0.25), word, font=tag, fill=(255, 255, 255))
        lines = wrap(text, tf, W * 0.6, 2)
        for j, ln in enumerate(lines):
            d.text((x + tw + 40 * U, y + j * tf.size * 1.2), ln, font=tf, fill=(255, 255, 255, int(255 * g)) if k else (190, 190, 195, int(255 * g)))
        if k == 0 and lines:
            s = ease(p * 2 - 0.6)
            ww = max(tf.getlength(ln) for ln in lines)
            for j in range(len(lines)):
                ly = y + j * tf.size * 1.2 + tf.size * 0.6
                d.line([(x + tw + 40 * U, ly), (x + tw + 40 * U + ww * s, ly)], fill=(220, 50, 50), width=max(4, int(8 * U)))
    return img


def frame_chapter(v, t, p, cache):
    img, d = base(v, cache, t, "dark", 20)
    num = (v.get("value") or "").strip()[:4] or "#"
    nf = Fnt(FB, 420 * U)
    g = ease(p * 1.5)
    d.text((W * 0.07, H / 2 - nf.size * 0.62 + (1 - g) * 60 * U), num, font=nf, fill=(232, 93, 42, int(255 * g)))
    x = W * 0.07 + nf.getlength(num) + 60 * U
    d.rectangle([x, H * 0.3, x + 8 * U, H * 0.3 + H * 0.4 * ease(p * 1.6 - 0.2)], fill=(255, 255, 255))
    hf = Fnt(FB, 88 * U)
    lines = wrap(v["headline"].upper(), hf, W - x - W * 0.08, 3)
    y = H / 2 - len(lines) * hf.size * 0.6
    for k, ln in enumerate(lines):
        a = ease(p * 2 - 0.5 - k * 0.15)
        d.text((x + 50 * U + (1 - a) * 40 * U, y + k * hf.size * 1.2), ln, font=hf, fill=(255, 255, 255, int(255 * a)))
    return img


VARIANTS = {"chart": [frame_chart, frame_line], "stat": [frame_stat, frame_stat_split],
            "list": [frame_list, frame_grid], "compare": [frame_compare, frame_hbars]}
_VC = {}


def render_visual(v, d, seg, i):
    typ = v.get("type")
    if typ in VARIANTS:
        n_ = _VC.get(typ, 0); _VC[typ] = n_ + 1
        fn = VARIANTS[typ][n_ % 2]
    else:
        fn = {"title": frame_title, "card": frame_card, "ranking": frame_ranking, "percent": frame_percent,
              "timeline": frame_timeline, "quote": frame_quote, "alert": frame_alert, "receipt": frame_receipt,
              "myth": frame_myth, "chapter": frame_chapter}.get(typ)
    if not fn:
        return False
    v = dict(v, _i=i)
    fdir = os.path.join(work, f"g{i}")
    os.makedirs(fdir, exist_ok=True)
    cache = {}
    anim = min(int(d * 30), 54)  # ~1.8s of build-up animation, then hold with a slow push-in
    try:
        for k in range(anim):
            fr = fn(v, k / 30, k / max(1, anim - 1), cache)
            if v.get("type") != "card":
                fr.alpha_composite(vignette())
            fr.save(os.path.join(fdir, f"f{k:03d}.png"))
    except Exception as e:
        print("Graphic failed, using footage:", e)
        return False
    hold = max(0.0, d - anim / 30)
    # Background = the scene's own footage, darkened, so viewers keep watching video under the graphic.
    bg = os.path.join(work, f"gb{i}.mp4")
    has_bg = False
    # Never a black screen: try the scene's subject, then the video's theme, then generic US money/city footage.
    tries = [] if v.get("_nobg") else [v.get("query")]
    # Theme fallbacks stay on-topic (no generic street shots that end up showing taxis).
    # Never generic money shots: fall back to real American people at work.
    tries += [plan.get("fallback_query"), random.choice(["american workers office USA", "american construction workers", "american warehouse workers", "american store employees", "american family kitchen"])]
    for q_ in [x for x in tries if x]:
        try:
            if make_footage(i, q_, d + 0.1, bg, False) or make_footage(i, q_, d + 0.1, bg, True):
                has_bg = True; break
        except Exception as e:
            print("Graphic background footage failed:", e)
    if has_bg:
        bg_in = ["-i", bg]
    else:
        bg_in = ["-f", "lavfi", "-i", f"color=c=0x101014:s={W}x{H}:r=30:d={d + 0.1:.2f}"]
    fc = (f"[0:v]scale={W}:{H},setsar=1,colorlevels=romax=0.55:gomax=0.55:bomax=0.55,gblur=sigma=2[b];"
          f"[1:v]format=rgba,tpad=stop_mode=clone:stop_duration={hold + 0.1:.2f}[g];"
          f"[b][g]overlay=0:0:format=auto,fps=30,format=yuv420p[o]")
    run(["ffmpeg", "-y", *bg_in, "-framerate", "30", "-i", os.path.join(fdir, "f%03d.png"),
         "-filter_complex", fc, "-map", "[o]", "-t", f"{d:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
    return True

def make_footage(i, q, d, seg, want_photo):
    """Fast-cut real footage for d seconds (clips first, photo as backup). Returns False if nothing fits."""
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,setsar=1"
    link, author = (None, None) if want_photo else find_clip(q, d)
    if not link and i < 3:
        for alt in (plan.get("fallback_query", ""),):
            if alt and not link:
                link, author = find_clip(alt, d)
    if link:
        n = 1 if d < 5 else min(4, math.ceil(d / 3.5))
        picks = [(link, author)]
        for _ in range(n - 1):
            l2, a2 = find_clip(q, d / n)
            if l2: picks.append((l2, a2))
        parts = []
        for k, (lk, au) in enumerate(picks):
            raw = os.path.join(work, f"r{i}_{k}_{int(d * 100)}.mp4")
            with requests.get(lk, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(raw, "wb") as f:
                    for chunk in r.iter_content(1 << 20): f.write(chunk)
            credits.add(au)
            pd = d / len(picks)
            F = max(1, int(pd * 30))
            mv = (i + k) % 4
            if mv == 0:
                zp = "zoompan=z='min(1+0.0011*on,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            elif mv == 1:
                zp = "zoompan=z='max(1.15-0.0011*on,1)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            elif mv == 2:
                zp = f"zoompan=z=1.12:x='(iw-iw/zoom)*on/{F}':y='ih/2-(ih/zoom/2)'"
            else:
                zp = f"zoompan=z=1.12:x='(iw-iw/zoom)*(1-on/{F})':y='ih/2-(ih/zoom/2)'"
            part = os.path.join(work, f"c{i}_{k}_{int(d * 100)}.mp4")
            run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", raw, "-t", f"{pd:.3f}", "-an", "-vf", vf + "," + zp + f":d=1:s={W}x{H}:fps=30,setsar=1",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", part])
            parts.append(part)
        if len(parts) == 1:
            os.replace(parts[0], seg)
        else:
            lst = os.path.join(work, f"c{i}_{int(d * 100)}.txt")
            with open(lst, "w") as f:
                f.writelines(f"file '{p_}'\n" for p_ in parts)
            run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-t", f"{d:.2f}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
        return True
    if UNSPLASH or PIXABAY:
        url, author = find_photo(q)
        if url:
            img = os.path.join(work, f"i{i}_{int(d * 100)}.jpg")
            open(img, "wb").write(requests.get(url, timeout=60).content)
            photo_credits.add(author)
            frames = int(d * 30) + 1
            z = "min(1+0.0012*on,1.25)" if i % 2 == 0 else "max(1.25-0.0012*on,1)"
            px = "iw/2-(iw/zoom/2)" if i % 3 else f"(iw-iw/zoom)*on/{frames}"
            kb = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
                  f"zoompan=z='{z}':x='{px}':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps=30,setsar=1")
            run(["ffmpeg", "-y", "-loop", "1", "-i", img, "-t", f"{d:.2f}", "-vf", kb,
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
            return True
    if want_photo:
        return make_footage(i, q, d, seg, False)
    return False


# ---- HeyGen reporter (Avatar III): ONE clip per video, filmed "on location" for this topic, repeated a few times ----
HG_KEY = os.environ.get("HEYGEN_API_KEY", "").strip()
HG_AVATAR = os.environ.get("HEYGEN_AVATAR_ID", "").strip()
HG_TYPE = os.environ.get("HEYGEN_AVATAR_TYPE", "").strip().lower()
if HG_AVATAR and not HG_TYPE:
    HG_TYPE = "avatar"  # a saved Avatar ID is a standard/Avatar III presenter, not a talking photo
reporter_seg = reporter_aud = None
reporter_d = 0.0
intro_seg = intro_aud = None
intro_d = 0.0
loop_clips = []  # [(seg, aud, dur, line), ...] — 2-3 DIFFERENT retention lines, rotated so repeats never sound identical
rep = plan.get("reporter") or {}
rep_lines = [str(x).strip()[:240] for x in (rep.get("lines") or []) if str(x).strip()][:3]
if rep.get("line") and str(rep["line"]).strip() not in rep_lines:
    rep_lines.insert(0, str(rep["line"]).strip()[:240])
if not rep_lines and rep.get("line"):
    rep_lines = [str(rep["line"]).strip()[:240]]
if HG_KEY and rep_lines and plan.get("reporter_on", True):
    try:
        hh = {"X-Api-Key": HG_KEY, "Content-Type": "application/json"}
        vid = os.environ.get("HEYGEN_VOICE_ID", "").strip()
        if not vid:
            want = "female" if plan.get("voice_gender") == "female" else "male"
            vs = requests.get("https://api.heygen.com/v2/voices", headers=hh, timeout=60).json().get("data", {}).get("voices", [])
            en = [v for v in vs if str(v.get("language", "")).lower().startswith("english")]
            pick = [v for v in en if str(v.get("gender", "")).lower() == want] or en or vs
            vid = pick[0]["voice_id"]
        if not HG_AVATAR:
            # no Avatar ID saved: rotate through HeyGen's stock Avatar III presenters of the narrator's gender
            want_g = "female" if plan.get("voice_gender") == "female" else "male"
            av = requests.get("https://api.heygen.com/v2/avatars", headers=hh, timeout=60).json().get("data", {}).get("avatars", [])
            av = [a for a in av if not a.get("premium")] or av
            pool = [a for a in av if str(a.get("gender", "")).lower() == want_g] or av
            # look like the sample reporter: upper-body presenter in a blazer / suit / jacket
            sharp = [a for a in pool if any(w in str(a.get("avatar_name", "")).lower() for w in ("blazer", "suit", "jacket", "business", "formal"))]
            HG_AVATAR = random.choice((sharp or pool)[:40])["avatar_id"]; HG_TYPE = "avatar"
        bg_url, _a = find_photo(str(rep.get("setting") or plan.get("fallback_query", "american office")))
        char = {"type": "talking_photo", "talking_photo_id": HG_AVATAR} if "photo" in HG_TYPE else {"type": "avatar", "avatar_id": HG_AVATAR, "avatar_style": "normal"}
        dim = {"width": 720, "height": 1280} if vertical else {"width": 1280, "height": 720}
        import time
        def hg_clip(line, name):
            vin = {"character": char, "voice": {"type": "text", "input_text": str(line)[:240], "voice_id": vid}}
            if bg_url:
                vin["background"] = {"type": "image", "url": bg_url}
            r = requests.post("https://api.heygen.com/v2/video/generate", headers=hh, json={"video_inputs": [vin], "dimension": dim}, timeout=60)
            r.raise_for_status()
            return r.json()["data"]["video_id"]
        def hg_wait(hg_id, name):
            hg_url = None
            for _ in range(120):  # up to ~20 min
                time.sleep(10)
                st = requests.get(f"https://api.heygen.com/v1/video_status.get?video_id={hg_id}", headers=hh, timeout=60).json().get("data", {})
                if st.get("status") == "completed":
                    hg_url = st.get("video_url"); break
                if st.get("status") == "failed":
                    raise RuntimeError(f"HeyGen failed: {st.get('error')}")
            if not hg_url:
                raise RuntimeError("HeyGen took too long")
            rawh = os.path.join(work, f"{name}_raw.mp4")
            open(rawh, "wb").write(requests.get(hg_url, timeout=180).content)
            seg = os.path.join(work, f"{name}.mp4")
            run(["ffmpeg", "-y", "-i", rawh, "-an", "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,setsar=1",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
            aud = os.path.join(work, f"{name}.wav")
            run(["ffmpeg", "-y", "-i", rawh, "-vn", "-ar", "44100", "-ac", "2", aud])
            return seg, aud, min(duration(seg), duration(aud))
        # per video: a unique 7s OPENER + 2-3 DIFFERENT retention lines rotated ~every minute (sounds natural, never the same words twice)
        intro_line = str(rep.get("intro") or "").strip()
        ids = [(hg_clip(ln, f"reporter{k}"), ln, f"reporter{k}") for k, ln in enumerate(rep_lines)]
        id_intro = hg_clip(intro_line, "intro") if intro_line else None
        for hg_id, ln, nm in ids:
            try:
                s, a, d = hg_wait(hg_id, nm)
                loop_clips.append((s, a, d, ln))
            except Exception as e:
                print(f"Loop clip skipped ({nm}):", e)
        if loop_clips:
            reporter_seg, reporter_aud, reporter_d, _ = loop_clips[0]
        if id_intro:
            try:
                intro_seg, intro_aud, intro_d = hg_wait(id_intro, "intro")
            except Exception as e:
                print("Intro clip skipped:", e)
        print(f"Reporter clips ready ({len(loop_clips)} loop lines, intro {intro_d:.1f}s) at: {rep.get('setting')}")
    except Exception as e:
        print("Reporter skipped:", e)
        reporter_seg = None

n_sc = len(plan["scenes"])

# ---- Real TikTok reaction (user-picked library): downloaded with yt-dlp, ~9s, composited over blurred footage ----
tk = plan.get("tiktok") or {}
tk_seg = tk_aud = None
tk_d = 0.0
tk_after = int(tk.get("after", -1) or -1)
if tk.get("url") and 0 <= tk_after < n_sc:
    try:
        rawt = os.path.join(work, "tiktok_raw.mp4")
        run(["yt-dlp", "-q", "--no-playlist", "-f", "best[ext=mp4]/best", "-o", rawt, str(tk["url"])])
        tk_d = max(4.0, min(9.0, duration(rawt) - 0.6))
        bgt = os.path.join(work, "tiktok_bg.mp4")
        sc_t = plan["scenes"][tk_after]
        have_bg = False
        try:
            have_bg = make_footage(900, sc_t.get("query", plan.get("fallback_query", "american city")), tk_d, bgt, False)
        except Exception as e:
            print("TikTok background skipped:", e)
        fh = int(H * 0.86) // 2 * 2
        tk_seg = os.path.join(work, "tiktok.mp4")
        if have_bg:
            fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=18:2,eq=brightness=-0.22[bg];"
                  f"[1:v]trim=0.4:{0.4 + tk_d:.2f},setpts=PTS-STARTPTS,scale=-2:{fh}[fg];"
                  f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,setsar=1[v]")
            run(["ffmpeg", "-y", "-i", bgt, "-i", rawt, "-filter_complex", fc, "-map", "[v]", "-t", f"{tk_d:.2f}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", tk_seg])
        else:
            fc = (f"[0:v]trim=0.4:{0.4 + tk_d:.2f},setpts=PTS-STARTPTS,split[a][b];"
                  f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2,eq=brightness=-0.3[bg];"
                  f"[b]scale=-2:{fh}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,setsar=1[v]")
            run(["ffmpeg", "-y", "-i", rawt, "-filter_complex", fc, "-map", "[v]", "-t", f"{tk_d:.2f}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", tk_seg])
        tk_aud = os.path.join(work, "tiktok.wav")
        run(["ffmpeg", "-y", "-ss", "0.4", "-t", f"{tk_d:.2f}", "-i", rawt, "-vn", "-af", "loudnorm=I=-16:TP=-1.5,apad",
             "-t", f"{tk_d:.2f}", "-ar", "44100", "-ac", "2", tk_aud])
        print(f"TikTok reaction ready ({tk_d:.1f}s) from @{tk.get('creator')}")
    except Exception as e:
        print("TikTok reaction skipped:", e)
        tk_seg = None
rep_at = set()
if reporter_seg:
    # The video OPENS with the HeyGen reporter (frame + captions + SFX on top),
    # then the same clip repeats mid-video and before the ending.
    rep_at = {0}  # opens the video; then repeats about once every minute (time-based, see loop)
last_rep = 0.0
rep_rot = 0  # rotates through the different retention lines so each repeat says something new

t0 = 0.0
starts = []
gfx_starts = []
for i, sc in enumerate(plan["scenes"]):
    if reporter_seg and i > 0 and i < n_sc - 1 and t0 - last_rep >= 60.0:
        rep_at.add(i)
    if i in rep_at:
        last_rep = t0
        # opener = unique intro clip (if made); repeats ROTATE through the 2-3 different retention lines
        use_intro = (i == 0 and intro_seg is not None)
        if use_intro:
            c_seg, c_aud, c_d, c_line = intro_seg, intro_aud, intro_d, str(rep.get("intro") or "")
        else:
            c_seg, c_aud, c_d, c_line = loop_clips[rep_rot % len(loop_clips)]
            rep_rot += 1
        segments.append(c_seg); audios.append(c_aud)
        rw = c_line.split()
        per_r = c_d / max(1, len(rw)); tr = t0
        for k in range(0, len(rw), 4):
            c = " ".join(rw[k:k + 4]); cd = per_r * len(c.split())
            events.append(f"Dialogue: 0,{ass_time(tr)},{ass_time(tr + cd)},Cap,,0,0,0,,{c.upper()}")
            tr += cd
        gfx_starts.append(t0)
        t0 += c_d
    starts.append(t0)
    text = sc["text"].strip()
    a = os.path.join(work, f"a{i}.mp3"); speak(text, a)
    d = duration(a) + 0.25
    seg = os.path.join(work, f"s{i}.mp4")
    q = sc.get("query", plan.get("fallback_query", "city"))
    vis = sc.get("visual") if i >= 3 else None  # the video ALWAYS opens on real footage
    done = False
    if vis:
        gfx_starts.append(t0)
        # Presentation graphics are short inserts (max ~2.8s); the rest of the scene is footage.
        vis = dict(vis, query=q)
        gd = min(d, 2.8)
        gseg = os.path.join(work, f"g{i}.mp4")
        if render_visual(vis, gd, gseg, i):
            rest = d - gd
            fseg = os.path.join(work, f"f{i}.mp4")
            if rest < 0.4:
                os.replace(gseg, seg); done = True
            elif make_footage(i, q, rest, fseg, False):
                lst = os.path.join(work, f"gf{i}.txt")
                with open(lst, "w") as f:
                    f.write(f"file '{gseg}'\nfile '{fseg}'\n")
                run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-t", f"{d:.2f}",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
                done = True
    if not done:
        done = make_footage(i, q, d, seg, UNSPLASH and i >= 3 and i % 3 == 2)
    if not done:
        # Named place with no exact match: show that same place (skyline, streets, homes), never another subject.
        for pl in sorted(named_places(q + " " + text)):
            for v in ("skyline", "city", "downtown", "homes", "night"):
                try:
                    if make_footage(i, f"{pl} {v}", d, seg, False):
                        done = True; break
                except Exception as e:
                    print("Place fallback skipped:", e)
            if done:
                break
    if not done:
        print("No matching US stock for scene", i, "— using a titled graphic")
        headline = " ".join(text.split()[:7]).upper()[:60]
        if not render_visual({"type": "title", "headline": headline, "color": "dark", "_nobg": True}, d, seg, i):
            raise RuntimeError(f"Could not render scene {i} without unrelated stock")
    pad = os.path.join(work, f"p{i}.wav")
    run(["ffmpeg", "-y", "-i", a, "-af", "apad=pad_dur=0.25", "-t", f"{d:.2f}", "-ar", "44100", "-ac", "2", pad])
    segments.append(seg); audios.append(pad)
    # subtitles: chunks of ~4 words timed by word count
    scene_words = text.split()
    chunks = [" ".join(scene_words[k:k + 4]) for k in range(0, len(scene_words), 4)] or [""]
    speak_d = d - 0.25
    per = speak_d / max(1, len(scene_words))
    t = t0
    for c in chunks:
        cd = per * len(c.split())
        events.append(f"Dialogue: 0,{ass_time(t)},{ass_time(t + cd)},Cap,,0,0,0,,{c.upper()}")
        t += cd
    t0 += d
    if tk_seg and i == tk_after:
        # Real TikTok reaction: vertical clip centered over the dimmed, blurred footage of this scene, original audio.
        segments.append(tk_seg); audios.append(tk_aud)
        gfx_starts.append(t0)
        events.append(f"Dialogue: 0,{ass_time(t0)},{ass_time(t0 + tk_d)},Cap,,0,0,0,,@{str(tk.get('creator', '')).upper()} ON TIKTOK")
        t0 += tk_d


with open(os.path.join(work, "v.txt"), "w") as f:
    f.writelines(f"file '{s}'\n" for s in segments)
with open(os.path.join(work, "a.txt"), "w") as f:
    f.writelines(f"file '{s}'\n" for s in audios)
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", os.path.join(work, "v.txt"), "-c", "copy", os.path.join(work, "video.mp4")])
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", os.path.join(work, "a.txt"), os.path.join(work, "voice.wav")])


# Background music + transition sound effects (ElevenLabs). Any failure keeps the plain voice track.
def el_audio(url, body, path):
    if USE_HF_AUDIO:
        try:
            prompt = body.get("prompt") or body.get("text") or ""
            secs = body.get("duration_seconds") or round(body.get("music_length_ms", 30000) / 1000)
            return hf_generate(HF_MUSIC, {"prompt": prompt, "duration": secs, "duration_seconds": secs}, path)
        except Exception as e:
            print("Higgsfield audio skipped:", e)
            return False
    try:
        r = requests.post(url, headers={"xi-api-key": EL_KEY, "Content-Type": "application/json"}, json=body, timeout=180)
        r.raise_for_status()
        open(path, "wb").write(r.content)
        return True
    except Exception as e:
        print("ElevenLabs audio skipped:", e)
        return False

if (USE_HF_AUDIO or EL_KEY) and plan.get("sfx", True):
    total = duration(os.path.join(work, "voice.wav"))
    rng = random.Random()
    # Every video gets its own sound identity: a music style + a matching SFX pack, picked at random.
    STYLES = [
        "modern trap-documentary beat, deep 808, crisp hi-hats, dark synth pads, building tension",
        "cinematic hybrid trailer pulse, ticking clock, low strings, big drums",
        "lo-fi hip hop with punchy kick, warm keys, investigative mood",
        "dark electronic news underscore, driving bassline, arpeggiated synths",
        "uplifting corporate funk groove, slap bass, claps, confident and catchy",
        "minimal piano and pizzicato strings, curious suspense, light percussion",
        "synthwave documentary groove, retro drums, pulsing bass",
        "gritty boom bap beat, vinyl crackle, serious street-level reportage",
        "epic orchestral tension with modern percussion hits",
        "future bass underscore, energetic, plucks and sidechained pads",
    ]
    PACKS = [
        {"cut": "fast cinematic air whoosh swoosh", "hit": "deep sub boom impact with reverb tail", "pop": "clean UI pop ding", "riser": "short tension riser into hit"},
        {"cut": "glitchy digital swipe transition", "hit": "heavy trailer braam hit", "pop": "soft bubble pop click", "riser": "reverse cymbal swell"},
        {"cut": "tape stop rewind transition", "hit": "punchy 808 drum drop", "pop": "cash register ka-ching", "riser": "noise sweep riser"},
        {"cut": "camera shutter flash whoosh", "hit": "orchestral timpani hit", "pop": "typewriter key ding", "riser": "string swell riser"},
        {"cut": "quick paper swipe swoosh", "hit": "news broadcast stinger sting", "pop": "notification chime", "riser": "rising synth sweep"},
    ]
    style = rng.choice(STYLES)
    pack = rng.choice(PACKS)
    mood = str(plan.get("music_mood") or "").strip()
    prompt = (mood + ", " if mood else "") + style
    print("Sound identity:", prompt, "|", pack["cut"])
    music = os.path.join(work, "music.mp3")
    have_music = el_audio("https://api.elevenlabs.io/v1/music", {"prompt": prompt + ", instrumental, no vocals, energetic intro, evolving sections, loopable", "music_length_ms": int(min(total, 300) * 1000) + 2000}, music) \
        or el_audio("https://api.elevenlabs.io/v1/sound-generation", {"text": prompt + ", instrumental background music loop, no vocals", "duration_seconds": 22, "loop": True, "prompt_influence": 0.5}, music)
    sfx = {}
    for k, dur in (("cut", 0.8), ("cut2", 0.8), ("hit", 1.5), ("riser", 2.0), ("pop", 0.6)):
        text = pack["cut"] + ", variation two" if k == "cut2" else pack[k]
        if k == "pop" and not gfx_starts:
            continue
        fp = os.path.join(work, f"sfx_{k}.mp3")
        if el_audio("https://api.elevenlabs.io/v1/sound-generation", {"text": "short " + text + ", sound effect", "duration_seconds": dur, "prompt_influence": 0.65}, fp):
            sfx[k] = fp
    inputs, filters, labels = ["-i", os.path.join(work, "voice.wav")], ["[0:a]asplit=2[vo][sc]"], ["[vo]"]
    n = 1
    if have_music:
        inputs += ["-stream_loop", "-1", "-i", music]
        # louder music that automatically ducks under the voice (sidechain), then swells in pauses
        filters.append(f"[{n}:a]atrim=0:{total:.2f},volume=0.32,afade=t=in:d=0.6,afade=t=out:st={max(0, total - 2):.2f}:d=2[mraw]")
        filters.append("[mraw][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=350[m]")
        labels.append("[m]"); n += 1
    else:
        filters[0] = "[0:a]anull[vo]"

    def place(key, times, vol, tag):
        global n
        for j, s_ in enumerate(times):
            inputs.extend(["-i", sfx[key]])
            ms = max(0, int(s_ * 1000))
            filters.append(f"[{n}:a]volume={vol},adelay={ms}|{ms}[{tag}{j}]"); labels.append(f"[{tag}{j}]"); n += 1

    if "hit" in sfx:
        place("hit", [0.0] + ([starts[len(starts) // 2]] if len(starts) > 6 else []), 0.55, "h")
    cuts = [s_ - 0.3 for j, s_ in enumerate(starts) if j > 0 and rng.random() < 0.55][:50]
    if "cut" in sfx:
        place("cut", cuts[0::2], rng.uniform(0.3, 0.45), "w")
    if "cut2" in sfx:
        place("cut2", cuts[1::2], rng.uniform(0.3, 0.45), "x")
    if "riser" in sfx and len(starts) > 4:
        place("riser", [max(0, starts[-1] - 2.0)], 0.35, "r")
    if "pop" in sfx:
        place("pop", gfx_starts[:40], 0.4, "p")
    if n > 1:
        filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0[out]")
        try:
            run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters), "-map", "[out]", "-ar", "44100", "-ac", "2", os.path.join(work, "mixed.wav")])
            os.replace(os.path.join(work, "mixed.wav"), os.path.join(work, "voice.wav"))
        except Exception as e:
            print("Audio mix skipped:", e)
    # Loud, clear YouTube audio: clean rumble, add voice presence, even out levels, then hit -14 LUFS with a safe peak ceiling.
    try:
        run(["ffmpeg", "-y", "-i", os.path.join(work, "voice.wav"), "-af",
             "highpass=f=80,equalizer=f=3000:t=q:w=1.2:g=3,acompressor=threshold=-20dB:ratio=3:attack=5:release=120:makeup=2,loudnorm=I=-14:TP=-1:LRA=9",
             "-ar", "48000", "-ac", "2", os.path.join(work, "loud.wav")])
        os.replace(os.path.join(work, "loud.wav"), os.path.join(work, "voice.wav"))
    except Exception as e:
        print("Loudness boost skipped:", e)

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
