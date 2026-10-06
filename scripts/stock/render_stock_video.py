"""stock-engine v87 (installed by Studio)
Builds a video from Pexels clips + narration + burned-in subtitles.
Usage: PLAN=<base64 json> python render_stock_video.py out.mp4
"""
import base64, json, os, random, re, subprocess, sys, tempfile, time
import requests

out = sys.argv[1]
os.makedirs(os.path.dirname(out), exist_ok=True)
if os.environ.get("PLAN"):
    plan = json.loads(base64.b64decode(os.environ["PLAN"]).decode("utf-8"))
else:
    with open(os.environ["PLAN_FILE"], "r", encoding="utf-8") as f:
        plan = json.load(f)
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
HF_KEY = os.environ.get("HIGGSFIELD_API_KEY", "").strip()
HF = bool(HF_KEY)
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
    h = {"Authorization": f"Key {HF_KEY}", "Content-Type": "application/json"}
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
# US brands & chains: if the script names one, the clip MUST show that brand (its name, logo, products or stores) —
# never a generic restaurant, construction site or street. "mcdonald's" tokenizes to "mcdonald".
BRANDS = {
    "mcdonald": {"mcdonald", "mcdonalds", "big mac", "mcnuggets", "golden arches", "mcflurry", "happymeal", "happy meal"},
    "burgerking": {"burger king", "burgerking", "whopper"},
    "wendys": {"wendys", "wendy's"}, "wendy": {"wendys", "wendy's"},
    "tacobell": {"taco bell", "tacobell"}, "kfc": {"kfc", "kentucky fried chicken"},
    "chickfila": {"chick fil a", "chickfila", "chick fil"}, "popeyes": {"popeyes", "popeye's"},
    "starbucks": {"starbucks", "frappuccino"}, "dunkin": {"dunkin", "dunkin donuts"},
    "subway": {"subway sandwich", "subway restaurant", "subway"},
    "dominos": {"dominos", "domino's"}, "pizzahut": {"pizza hut", "pizzahut"},
    "chipotle": {"chipotle"}, "fiveguys": {"five guys", "fiveguys"},
    "walmart": {"walmart", "wal mart"}, "target": {"target store", "target"},
    "costco": {"costco"}, "samsclub": {"sam's club", "sams club", "samsclub"},
    "homedepot": {"home depot", "homedepot"}, "lowes": {"lowes", "lowe's"},
    "amazon": {"amazon"}, "tesla": {"tesla", "cybertruck", "model y", "model 3"},
    "ford": {"ford", "f-150", "f150", "mustang"}, "apple": {"apple", "iphone", "macbook", "ipad"},
    "netflix": {"netflix"}, "disney": {"disney", "disneyland", "disney world"},
    "mcdonalds": {"mcdonald", "mcdonalds", "big mac", "mcnuggets", "golden arches"},
}
for _b, _syn in BRANDS.items():
    ANCHORS[_b] = _syn
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


def stem(w):
    """eggs->egg, prices->price, boxes->box, groceries->grocery — so a tag 'egg' matches the word 'eggs'."""
    if len(w) > 4 and w.endswith("ies"): return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ches", "shes", "xes", "sses")): return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")): return w[:-1]
    return w


def stems(s):
    return {stem(w) for w in extract_words(s)}


# Words too vague to prove a scene (a "line" or "building" can be anything) — never used as the must-see subject.
WEAK = {"building", "buildings", "line", "people", "person", "man", "woman", "men", "women", "room", "area", "street", "city", "scene",
        "place", "thing", "things", "background", "life", "day", "time", "price", "prices", "cost", "costs", "rising", "high", "new",
        "old", "big", "small", "american", "americans", "usa", "us", "america", "united", "states", "exterior", "interior", "closeup"}
# The ONE concrete thing the current scene must visibly show (set per scene from the plan's "subject").
SCENE_MUST = set()
SCENE_SUBJECT = ""


def set_scene_subject(subj):
    global SCENE_MUST, SCENE_SUBJECT
    SCENE_SUBJECT = str(subj or "").strip().lower()[:40]
    SCENE_MUST = {stem(w) for w in extract_words(SCENE_SUBJECT) if w not in WEAK and w not in STOP and len(w) > 2}
    for p in named_places(SCENE_SUBJECT):  # places are verified by the place rule
        SCENE_MUST -= {stem(w) for w in p.split()}


def must_ok(text):
    """True when the result's tags/description mention the scene's subject (any of its specific words, plural-safe)."""
    if not SCENE_MUST:
        return True
    st = stems(text)
    flat = re.sub(r"[^a-z0-9]", "", text.lower())
    return bool(SCENE_MUST & st) or any(len(m) > 4 and m in flat for m in SCENE_MUST)


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
    if has_us:
        return [q]
    out = [q + " USA", q + " United States"]
    if SCENE_MUST and not named_places(q):
        out.append(q)  # still filtered: foreign tags rejected, subject must be shown
    return out


CARTOON_BAD = ["anime", "animation", "animated", "cartoon", "toon", "manga", "illustration", "illustrated",
               "3d render", "3d animation", "cgi", "vector", "drawing", "clipart", "clip art", "digital art",
               "ai generated", "render", "figurine", "doll", "plush", "mascot", "character design", "pixar"]

# Absolute bans: adult/sexual content and confusing close-ups of bodies — never allowed in any scene.
NSFW_BAD = ["nude", "naked", "nudity", "nsfw", "sexy", "lingerie", "bikini", "underwear", "topless", "erotic",
            "sensual", "breast", "buttocks", "porn", "fetish", "strip", "seductive", "intimate"]
# People words: used to reject people shots when the scene is about an OBJECT (cars, eggs, prices...).
PEOPLE_WORDS = {"people", "person", "man", "woman", "men", "women", "boy", "girl", "child", "children", "kid",
                "kids", "baby", "couple", "crowd", "pedestrian", "pedestrians", "face", "portrait", "hands",
                "hand", "family", "model", "tourist", "tourists", "shopper", "shoppers", "customer", "customers"}


def candidate_ok(query, metadata, location=""):
    text = str(metadata or "").lower().replace("-", " ")
    place = str(location or "").lower()
    both = text + " " + place
    # REAL FOOTAGE ONLY: never cartoons, anime, illustrations, 3D renders or AI-looking art.
    if any(w in both for w in CARTOON_BAD):
        return False
    # Never spiders, insects or creepy-crawlies.
    if re.search(r"\b(spiders?|arachnids?|tarantulas?|insects?|bugs?|cockroach(es)?|scorpions?|worms?|ants?|beetles?|centipedes?)\b", both):
        return False
    # Never any adult/sexual content, whatever the scene.
    if any(w in both for w in NSFW_BAD):
        return False
    if any(has_word(w, both) for w in FOREIGN_WORDS):
        return False
    # UNIVERSAL SUBJECT RULE: whatever the sentence talks about (eggs, beef, dentist, McDonald's, tires...)
    # must be in the result — otherwise it is rejected, no matter how "close" it looks.
    if not must_ok(both):
        return False
    subject_hit = bool(SCENE_MUST)
    # OBJECT RULE: when the scene is about a thing (cars, eggs, houses, phones...) and NOT about
    # people at work, reject clips whose tags are about people — "cars" must show only cars.
    if subject_hit and not (SCENE_MUST & {stem(w) for w in WORK}) and not (SCENE_MUST & stems("people person crowd family")):
        if extract_words(both) & PEOPLE_WORDS and not (extract_words(both) & set(SCENE_MUST)):
            return False
        # Even if the object is tagged, a clip dominated by people tags is confusing — reject it.
        people_hits = len(extract_words(both) & PEOPLE_WORDS)
        if people_hits >= 2:
            return False
    # Detect a named brand FIRST, before any other gate can kill the query.
    qtext = str(query).lower().replace("'", "").replace("-", "").replace(" ", "")
    query_brand = next((b for b in BRANDS if b in qtext), None)
    # Every result came from a US-only search ("... USA"). Places still need an explicit US clue;
    # people/work scenes (rarely tagged "USA") pass when the result matches the work subject
    # and carries no foreign tag, so videos show real American workers instead of only dollar bills.
    if not any(has_word(w, both) for w in US_WORDS):
        subj = subject(query)
        words0 = extract_words(both)
        strong = subject_hit or bool(subj & WORK and words0 & WORK) or query_brand is not None or any(t in ANCHORS and ANCHORS[t] & words0 for t in subj)
        if named_places(query) or not strong:
            return False
    if place and not any(has_word(w, place) for w in US_WORDS):
        return False
    # Named state/city: the clip must show THAT place — never another US city, never a "neighbour".
    places = named_places(query)
    if places and not any(has_word(w, both) for w in places):
        return False
    words = extract_words(both)
    terms = subject(query)
    qw = extract_words(query)
    if (qw & MED_TRIGGERS) and not (qw & {"ambulance", "paramedic", "emergency"}) and (words & MED_BLOCK):
        return False
    # Named brand (McDonald's, Walmart, Tesla...): the result must literally mention the brand or its
    # products — a brand scene can NEVER be filled with a generic restaurant/store/construction clip.
    qtext = str(query).lower().replace("'", "").replace("-", "").replace(" ", "")
    brand_hit = False
    for b, syn in BRANDS.items():
        if b in qtext:
            if not any(s.replace(" ", "") in both.replace("-", " ").replace(" ", "") or has_word(s, both) for s in syn):
                return False
            brand_hit = True
    # A verified brand IS the subject — accept it without further word overlap.
    if brand_hit:
        return True
    # Exact subject: anchor nouns (dealership, hospital...) must appear themselves or as a true synonym.
    # When the scene subject was verified, only anchors inside the subject itself can veto
    # (extra helper words like "grocery" in "eggs carton grocery" must not reject a real eggs clip).
    anchor_terms = (extract_words(SCENE_SUBJECT) - STOP) if subject_hit else terms
    for t in anchor_terms:
        if t in ANCHORS and not (ANCHORS[t] & words):
            return False
    # Anchor terms that passed above are satisfied — accept.
    if any(t in ANCHORS for t in terms):
        return True
    # A named place was literally verified above — the place IS the scene, accept it.
    if places:
        return True
    if not terms or subject_hit:
        return True
    hit = len({stem(t) for t in terms} & {stem(w) for w in words})
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


NEG = ["a camera", "a smartphone screen", "text on a page", "an abstract background", "a logo", "a cartoon illustration", "an anime drawing", "a 3d cartoon render", "an empty sky", "a person posing for the camera", "a crowd of people", "nudity or sexual content"]


def clip_rank(query, items, thumb):
    """items: candidates already passing tag rules. Returns them best-first, dropping ones that don't show the subject."""
    m = clip_model()
    if not m or not items:
        return items
    try:
        import io, torch
        from PIL import Image
        model, proc = m
        subj = SCENE_SUBJECT or " ".join(sorted(subject(query))) or query
        prompts = [f"a real photograph of {query}", f"a real photograph of {subj}",
                   f"a cartoon, anime or 3d render of {subj}"] + [f"a photo of {n}" for n in NEG if not (extract_words(n) & subject(query))]
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
            cartoon = float(probs[k][2])
            if cartoon > max(float(probs[k][0]), float(probs[k][1])) * 0.8:
                print(f"Visual AI rejected a cartoon/illustration for '{query}'"); continue
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


# Each presentation preset gets its own backdrop so templates never look alike.
PV = int(plan.get("presentation_variant") or 0) % 3
PV_BG = [
    {"blue": ((18, 52, 160), (2, 8, 44)), "dark": ((22, 40, 110), (2, 6, 30))},     # prices: deep navy
    {"blue": ((70, 46, 30), (10, 8, 8)), "dark": ((62, 60, 66), (6, 6, 8))},        # comparisons: graphite + amber
    {"blue": ((18, 70, 72), (2, 14, 18)), "dark": ((58, 22, 30), (10, 4, 8))},      # informative: ink teal / burgundy
][PV]


def radial(c1, c2):
    for k_, v_ in BG.items():
        if (c1, c2) == v_ and k_ in PV_BG:
            c1, c2 = PV_BG[k_]
    S = 192
    sm = Image.new("RGB", (S, S))
    cx, cy = (S * 0.5, S * 0.5) if PV == 0 else ((S * 0.25, S * 0.3) if PV == 1 else (S * 0.7, S * 0.2))
    px = sm.load()
    for y in range(S):
        for x in range(S):
            d = min(1.0, math.hypot(x - cx, y - cy) / (S * 0.75))
            d = d * d * (3 - 2 * d)
            px[x, y] = tuple(int(c1[k] * (1 - d) + c2[k] * d) for k in range(3))
    im = sm.resize((W, H), Image.LANCZOS)
    # Fine grain removes gradient banding for a cleaner, premium look.
    try:
        im = Image.blend(im, Image.effect_noise((W, H), 14).convert("RGB"), 0.035)
    except Exception:
        pass
    im = im.convert("RGBA")
    # Semi-transparent tint: graphics sit on top of (darkened) footage, never a solid background.
    im.putalpha(105)
    return im


def grid(img, t, alpha=80):
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    if PV == 0:
        # prices: drifting dot matrix
        step = int(54 * U)
        off = int((t * 18) % step)
        r = max(1.5, 2.6 * U)
        for y in range(-step, H + step, step):
            for x in range(-step, W + step, step):
                a = int(alpha * (0.55 + 0.45 * math.sin(x / 260 + y / 310 + t * 1.2)))
                d.ellipse([x + off - r, y - r, x + off + r, y + r], fill=(170, 200, 255, max(0, a)))
    elif PV == 1:
        # comparisons: slow diagonal light bands
        step = int(220 * U)
        off = (t * 30) % step
        for k in range(-H // step - 2, W // step + 3):
            x = k * step + off
            d.polygon([(x, H), (x + 60 * U, H), (x + 60 * U + H * 0.6, 0), (x + H * 0.6, 0)], fill=(255, 190, 120, int(alpha * 0.35)))
    else:
        # informative: editorial ruled lines with a margin rule
        step = int(64 * U)
        off = int((t * 10) % step)
        for y in range(-step, H + step, step):
            d.line([(0, y + off), (W, y + off)], fill=(255, 255, 255, int(alpha * 0.6)), width=max(1, int(1.5 * U)))
        d.line([(W * 0.055, 0), (W * 0.055, H)], fill=(230, 120, 100, int(alpha * 1.4)), width=max(2, int(3 * U)))
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
    # Only render explicit labeled observations, never synthetic seven-bar market history.
    rows = [kv(s) for s in (v.get("items") or [])[:5]]
    rows = [(lab, val) for lab, val in rows if parse_num(val)]
    if len(rows) >= 2:
        return frame_bars(v, t, p, cache, rows, False)
    return frame_stat(v, t, p, cache) if v.get("value") else frame_title(v, t, p, cache)

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
    txt = (v.get("value") or "") if v.get("_opening") else (f"{pn[0]}{pn[1] * g:,.{pn[2]}f}{pn[3]}" if pn else (v.get("value") or ""))
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


def fit_lines(text, path, size, maxw, maxl=3, minsize=18):
    # Shrink font, then wrap, so a label never exceeds its column width.
    s = size
    while True:
        f = Fnt(path, s)
        words = (text or "").split()
        if s <= minsize or (len(wrap(text, f, maxw, 99)) <= maxl and all(f.getlength(w_) <= maxw for w_ in words)):
            lines = wrap(text, f, maxw, maxl)
            if len(wrap(text, f, maxw, 99)) > maxl and lines:
                ln = lines[-1]
                while ln and f.getlength(ln + "…") > maxw:
                    ln = ln[:-1]
                lines[-1] = ln.rstrip() + "…"
            return f, lines
        s = int(s * 0.9)


def ctext_block(d, text, path, size, cx, y, maxw, fill, maxl=3, up=False):
    f, lines = fit_lines(text, path, size, maxw, maxl)
    lh = f.size * 1.15
    y0 = y - (len(lines) - 1) * lh if up else y
    for j, ln in enumerate(lines):
        ctext(d, ln, f, cx, y0 + j * lh, fill)


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
    # The schema has no measured time-series points; use labeled evidence bars instead.
    return frame_chart(v, t, p, cache)

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
        colw = (x1 - x0) / n * 0.9
        if "tl_vs" not in cache:
            bs = 66 * U if n <= 3 else 48 * U
            cache["tl_vs"] = min([fit_lines(bb, FB, bs, colw, 3)[0].size for _, bb in items if bb] or [bs])
        ctext_block(d, a_, FB, 56 * U, x, y - 110 * U - (1 - g) * 30 * U, colw, (235, 200, 180, int(255 * g)), 2, up=True)
        if b_:
            ctext_block(d, b_, FB, cache["tl_vs"], x, y + 50 * U + (1 - g) * 30 * U, colw, (255, 255, 255, int(255 * g)), 3)
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


def broadcast_text(d, text, box, size, fill, maxlines=2):
    # Bound both dimensions, even for long unbroken names.
    x, y, bw, bh = box
    f, lines = fit_lines(str(text or ""), FB, size, bw, maxlines, minsize=max(12, int(18 * U)))
    while f.size * 1.2 * len(lines) > bh and f.size > 10:
        f, lines = fit_lines(str(text or ""), FB, int(f.size * 0.9), bw, maxlines, minsize=10)
    for j, line in enumerate(lines):
        while line and f.getlength(line) > bw:
            line = line[:-2].rstrip() + "…" if len(line) > 2 else ""
        d.text((x, y + j * f.size * 1.2), line, font=f, fill=fill)


def frame_broadcast(v, t, p, cache):
    # Crisp native type over the exact-subject media, no invented logo or map.
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    accent = [(75, 210, 220, 255), (255, 169, 105, 255), (240, 111, 125, 255)][PV]
    ink, white, muted = (9, 18, 27, 220), (255, 255, 255, 255), (220, 233, 240, 255)
    x, bw = W * 0.075, W * (0.85 if vertical else 0.56)
    top, bottom = H * 0.12, H * (0.68 if vertical else 0.78)
    d.rectangle([x - W * 0.025, top - H * 0.025, x + bw + W * 0.025, bottom], fill=ink)
    # Subtle movement without repeating the other presets' grid backgrounds.
    bar = bw * (0.75 + 0.05 * math.sin(t * 1.4))
    d.rectangle([x, top, x + bar, top + 5 * U], fill=accent)
    broadcast_text(d, v.get("headline", ""), (x, top + H * 0.025, bw, H * 0.12), 70 * U, white)
    value_y = H * (0.29 if vertical else 0.34)
    broadcast_text(d, v.get("value", ""), (x, value_y, bw, H * 0.12), 154 * U, accent, 1)
    broadcast_text(d, v.get("sub", ""), (x, value_y + H * 0.125, bw, H * 0.045), 38 * U, muted, 1)
    detail_y = value_y + H * 0.19
    if v.get("change"):
        trend = v.get("trend")
        if trend in ("up", "down"):
            ay, aw = detail_y + 28 * U, 26 * U
            pts = [(x, ay + aw), (x + aw / 2, ay), (x + aw, ay + aw)]
            if trend == "down": pts = [(xx, 2 * ay + aw - yy) for xx, yy in pts]
            d.polygon(pts, fill=accent)
        dx = x + 42 * U if trend in ("up", "down") else x
        broadcast_text(d, v["change"], (dx, detail_y, bw - (dx - x), H * 0.055), 60 * U, white, 1)
        broadcast_text(d, v.get("period", ""), (x, detail_y + H * 0.06, bw, H * 0.045), 34 * U, muted, 1)
    footer = " · ".join(s for s in [v.get("location"), ("Source: " + v["source"]) if v.get("source") else None] if s)
    broadcast_text(d, footer, (x, bottom - H * 0.065, bw, H * 0.055), 28 * U, muted)
    return img


VARIANTS = {"chart": [frame_chart, frame_line], "stat": [frame_stat, frame_stat_split],
            "list": [frame_list, frame_grid], "compare": [frame_compare, frame_hbars]}
_VC = {}


def render_visual(v, d, seg, i):
    typ = v.get("type")
    if typ in VARIANTS:
        n_ = _VC.get(typ, 0); _VC[typ] = n_ + 1
        fn = VARIANTS[typ][(n_ + int(plan.get("presentation_variant") or 0)) % 2]
    else:
        fn = {"title": frame_title, "card": frame_card, "ranking": frame_ranking, "percent": frame_percent,
              "timeline": frame_timeline, "quote": frame_quote, "alert": frame_alert, "receipt": frame_receipt,
              "myth": frame_myth, "chapter": frame_chapter, "broadcast": frame_broadcast}.get(typ)
    if not fn:
        return False
    v = dict(v, _i=i, _opening=(i == 0))
    fdir = os.path.join(work, f"g{i}")
    os.makedirs(fdir, exist_ok=True)
    cache = {}
    # Animate the background for the whole spoken scene, not a frozen final PNG.
    graphic_fps = 10
    anim = max(1, math.ceil(d * graphic_fps))
    try:
        for k in range(anim):
            t = k / graphic_fps
            p = min(1.0, (0.55 if i == 0 else 0.08) + t / 1.4)
            fr = fn(v, t, p, cache)
            if v.get("type") != "card":
                fr.alpha_composite(vignette())
            fr.save(os.path.join(fdir, f"f{k:03d}.png"))
    except Exception as e:
        print("Graphic failed, using footage:", e)
        return False
    hold = max(0.0, d - anim / graphic_fps)
    # Background = the scene's own footage, darkened, so viewers keep watching video under the graphic.
    bg = os.path.join(work, f"gb{i}.mp4")
    has_bg = False
    # A paid AI photo background (presentation scenes) is used as-is; no stock search.
    if v.get("_aibg") and os.path.exists(v["_aibg"]):
        bg = v["_aibg"]; has_bg = True
    # Selected backgrounds always take priority, including full-scene presentations.
    # Never fill a missing background with another scene's image or generic footage.
    if not has_bg and SCENE_CLIPS:
        try:
            has_bg = make_scene_footage(i, v.get("query"), d + 0.1, bg)
        except Exception as e:
            print("Selected presentation background unavailable:", e)
    tries = [] if (MY_CLIPS or has_bg or v.get("_nobg")) else [v.get("query"), SCENE_SUBJECT]
    for q_ in [x for x in tries if x]:
        try:
            if make_footage(i, q_, d + 0.1, bg, False) or make_footage(i, q_, d + 0.1, bg, True):
                has_bg = True; break
        except Exception as e:
            print("Graphic background footage failed:", e)
    if has_bg:
        bg_in = ["-i", bg]
    else:
        print(f"Presentation {i}: no matching background available; no unrelated photo or unapproved AI purchase")
        bg_in = ["-f", "lavfi", "-i", f"color=c=0x101014:s={W}x{H}:r=30:d={d + 0.1:.2f}"]
    fc = (f"[0:v]scale={W}:{H},setsar=1,colorlevels=romax=0.72:gomax=0.72:bomax=0.72[b];"
          f"[1:v]format=rgba,tpad=stop_mode=clone:stop_duration={hold + 0.1:.2f}[g];"
          f"[b][g]overlay=0:0:format=auto,fps=30,format=yuv420p[o]")
    run(["ffmpeg", "-y", *bg_in, "-framerate", str(graphic_fps), "-i", os.path.join(fdir, "f%03d.png"),
         "-filter_complex", fc, "-map", "[o]", "-t", f"{d:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
    return True

MY_CLIPS = [c for c in (plan.get("my_clips") or []) if isinstance(c, dict) and c.get("url")]
CUR_CLIP = None
SCENE_CLIPS = []


def download_retry(url, path, tries=5):
    """Download with retries + backoff (handles 429 rate limits and flaky connections)."""
    last = None
    for k in range(tries):
        try:
            with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0 (StudioRenderer)"}) as r:
                if r.status_code in (429, 500, 502, 503, 504):
                    ra = r.headers.get("Retry-After")
                    wait = float(ra) if ra and ra.replace(".", "").isdigit() else 3 * (2 ** k)
                    print(f"Download {r.status_code}, retry {k + 1}/{tries} in {wait:.0f}s")
                    time.sleep(min(wait, 60)); last = Exception(f"HTTP {r.status_code}"); continue
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 20): f.write(chunk)
                return True
        except Exception as e:
            last = e; print(f"Download error, retry {k + 1}/{tries}:", e); time.sleep(3 * (2 ** k))
    raise last or Exception("download failed")


def use_my_clip(i, d, seg, clip):
    """Render the creator's hand-picked clip/photo for this scene (no stock search at all)."""
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,setsar=1"
    ext = "jpg" if clip.get("kind") == "photo" else "mp4"
    raw = os.path.join(work, f"my{i}_{int(d * 100)}.{ext}")
    download_retry(clip["url"], raw)
    time.sleep(0.4)
    if ext == "jpg":
        photo_credits.add(clip.get("author") or "Stock")
        frames = int(d * 30) + 1
        z = "min(1+0.0012*on,1.25)" if i % 2 == 0 else "max(1.25-0.0012*on,1)"
        kb = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps=30,setsar=1")
        run(["ffmpeg", "-y", "-loop", "1", "-i", raw, "-t", f"{d:.2f}", "-vf", kb, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
    else:
        credits.add(clip.get("author") or "Stock")
        # different start point each time the same clip is reused, so repeats look fresh
        try: L = duration(raw)
        except Exception: L = 0
        ss = (i * 3.7) % max(0.1, L - d) if L > d + 1 else 0
        zp = "zoompan=z='min(1+0.0011*on,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'" if i % 2 == 0 else "zoompan=z='max(1.15-0.0011*on,1)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        run(["ffmpeg", "-y", "-ss", f"{ss:.2f}", "-stream_loop", "-1", "-i", raw, "-t", f"{d:.3f}", "-an", "-vf", vf + "," + zp + f":d=1:s={W}x{H}:fps=30,setsar=1",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
    return True


def make_scene_footage(i, q, d, seg, want_photo=False):
    """Cut between all distinct approved images assigned to this spoken scene."""
    if not MY_CLIPS:
        return make_footage(i, q, d, seg, want_photo)
    chosen = SCENE_CLIPS or ([CUR_CLIP] if CUR_CLIP else [])
    if not chosen:
        return False
    if len(chosen) == 1:
        try:
            return use_my_clip(i, d, seg, chosen[0])
        except Exception as e:
            print("Approved clip unavailable after retries; using presentation instead:", e)
            return False
    parts = []
    for k, clip in enumerate(chosen):
        part = os.path.join(work, f"selected{i}_{k}_{int(d * 100)}.mp4")
        try:
            use_my_clip(i * 100 + k, d / len(chosen), part, clip)
            parts.append(part)
        except Exception as e:
            print("Skipping unavailable clip:", e)
    if not parts:
        return False
    if len(parts) == 1:
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", parts[0], "-t", f"{d:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
        return True
    lst = os.path.join(work, f"selected{i}_{int(d * 100)}.txt")
    with open(lst, "w") as f:
        f.writelines(f"file '{p}'\n" for p in parts)
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-t", f"{d:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
    return True


def make_footage(i, q, d, seg, want_photo):
    """Fast-cut real footage for d seconds (clips first, photo as backup). Returns False if nothing fits."""
    if MY_CLIPS:
        clip = CUR_CLIP if (CUR_CLIP and i < 900) else None
        if not clip:
            print("No approved clip assigned to scene", i)
            return False
        try:
            return use_my_clip(i, d, seg, clip)
        except Exception as e:
            print("Picked clip failed; not substituting unrelated footage:", e)
            return False
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,setsar=1"
    link, author = (None, None) if want_photo else find_clip(q, d)
    if not link and i < 3:
        # The opening must be real motion: try every simpler wording for a VIDEO
        # before ever considering a still photo for the first scenes.
        for alt in (plan.get("fallback_query", ""), SCENE_SUBJECT, plan.get("title", "")):
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


# ---- Lovable AI opener: ONE unique 5s cinematic clip of the EXACT topic, generated per video ----
LV_KEY = os.environ.get("LOVABLE_API_KEY", "").strip()
reporter_seg = reporter_aud = None
reporter_d = 0.0
intro_seg = intro_aud = None
intro_d = 0.0
reporter_prompt = str(plan.get("reporter_prompt") or "").strip()
hook_line = str(plan.get("hook") or "").strip()
# Reporter/AI opener disabled: videos open directly with the first moving stock clip (scene-0 swap below).

n_sc = len(plan["scenes"])

# ---- Real TikTok reaction (user-picked library): downloaded with yt-dlp, up to ~30s, composited over blurred footage ----
tk = plan.get("tiktok") or {}
tk_seg = tk_aud = None
tk_d = 0.0
tk_after = int(tk.get("after", -1) or -1)
if tk.get("url") and 0 <= tk_after < n_sc:
    try:
        rawt = os.path.join(work, "tiktok_raw.mp4")
        run(["yt-dlp", "-q", "--no-playlist", "-f", "best[ext=mp4]/best", "-o", rawt, str(tk["url"])])
        tk_d = max(4.0, min(30.0, duration(rawt) - 0.6))
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
    # The video OPENS with the Lovable AI cinematic opener (frame + SFX on top, no captions — no spoken line).
    rep_at = {0}

AI_IMAGE_BLOCKED = False

def fresh_category_image(prompt, image):
    # Store tiny visual fingerprints, never image files; reject repeated-looking output without paying for a retry.
    import hashlib
    gray = image.convert("L").resize((9, 8))
    pixels = list(gray.getdata())
    bits = sum((pixels[y * 9 + x] > pixels[y * 9 + x + 1]) << (y * 8 + x) for y in range(8) for x in range(8))
    old = history | {hkey(k) for k in used}
    fingerprints = [int(k[7:], 16) for k in old if k.startswith("aihash:")]
    if any((bits ^ prior).bit_count() <= 4 for prior in fingerprints):
        raise RuntimeError("Generated photo looks too similar to an earlier photo; keeping approved media instead.")
    used.add("aihash:" + format(bits, "016x"))
    return image

def gen_ai_image(prompt, size):
    global AI_IMAGE_BLOCKED
    unique = bool(plan.get("category_images") or plan.get("ai_presentations"))
    if unique:
        if AI_IMAGE_BLOCKED or not OPENAI:
            raise RuntimeError("ChatGPT photos unavailable; keeping approved clips and free presentations.")
        import hashlib
        angles = ["eye-level medium shot", "close-up of the exact subject", "wide establishing photograph", "three-quarter angle photograph", "overhead detail photograph", "low-angle detail photograph"]
        lights = ["soft natural daylight", "realistic warm indoor light", "neutral diffused light", "natural side lighting"]
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        offset = int(hashlib.sha256(str(plan.get("image_run_id", "")).encode()).hexdigest()[:8], 16) % 24
        known = history | {hkey(k) for k in used}
        choices = [(offset + n) % 24 for n in range(24)]
        choice = next((n for n in choices if f"aicomposition:{digest}:{n}" not in known), offset)
        prompt += ". Photorealistic, looks like a real camera photograph, never cartoon, anime, illustration or 3D render. Composition: " + angles[choice % 6] + "; " + lights[choice // 6] + ". Keep the exact subject and US setting unchanged. Illustrative editorial photo, not evidence of a real news event."
        used.add(f"aicomposition:{digest}:{choice}")
        size = "1024x1024"
    # Owner's own OpenAI (ChatGPT API) key first: billed to their OpenAI account, cheapest mini model.
    if OPENAI:
        try:
            ro = requests.post("https://api.openai.com/v1/images/generations",
                               headers={"Authorization": f"Bearer {OPENAI}", "Content-Type": "application/json"},
                               json={"model": IMG_MODEL, "prompt": prompt, "size": size, "quality": "low", "n": 1}, timeout=300)
            ro.raise_for_status()
            import io
            image = Image.open(io.BytesIO(base64.b64decode(ro.json()["data"][0]["b64_json"]))).convert("RGB")
            return fresh_category_image(prompt, image) if unique else image
        except Exception as e:
            if unique:
                if isinstance(e, requests.HTTPError): AI_IMAGE_BLOCKED = True
                raise RuntimeError("ChatGPT photo skipped; no paid provider fallback: " + str(e)[:200]) from e
            print("OpenAI key image failed, using Lovable AI:", str(e)[:200])
    key = os.environ.get("LOVABLE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("AI image key is missing")
    url = "https://ai.gateway.lovable.dev/v1/images/generations"
    body = {"model": "openai/gpt-image-2.5-sunburst", "prompt": prompt, "size": size, "quality": "low", "stream": True, "partial_images": 1}
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=body, stream=True)
    if not r.ok:
        raise RuntimeError("AI image failed (" + str(r.status_code) + "): " + r.text[:300])
    saw, image = False, None
    try:
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"): continue
            try: ev = json.loads(line[5:].strip())
            except (ValueError, TypeError): continue
            if not isinstance(ev, dict): continue
            k = ev.get("type", "")
            if k == "error":
                raise RuntimeError("AI image failed: " + str((ev.get("error") or {}).get("message")))
            if k in ("image_generation.partial_image", "image_generation.completed"):
                saw = True
                if k == "image_generation.completed": image = ev.get("b64_json")
    finally:
        r.close()
    if not saw:
        body2 = {k: v for k, v in body.items() if k not in ("stream", "partial_images")}
        r2 = requests.post(url, headers=headers, json=body2)
        if not r2.ok: raise RuntimeError("AI image failed (" + str(r2.status_code) + ")")
        image = (r2.json().get("data") or [{}])[0].get("b64_json")
    if not image:
        raise RuntimeError("AI image stream ended without an image")
    import io
    return Image.open(io.BytesIO(base64.b64decode(image))).convert("RGB")

AI_SPENT = 0  # paid AI pictures used so far (presentation backgrounds + full-screen cards share one budget)

t0 = 0.0
card_spans = []
starts = []
gfx_starts = []
for i, sc in enumerate(plan["scenes"]):
    if i in rep_at:
        segments.append(intro_seg); audios.append(intro_aud)
        gfx_starts.append(t0)
        t0 += intro_d
    starts.append(t0)
    text = sc["text"].strip()
    a = os.path.join(work, f"a{i}.mp3"); speak(text, a)
    # Scene length always follows the voice: the narrator talks the whole time,
    # presentations stay only while their narration plays (no silent holds),
    # so the video matches the length the owner asked for.
    d = duration(a) + 0.25
    seg = os.path.join(work, f"s{i}.mp4")
    q = sc.get("query", plan.get("fallback_query", "city"))
    set_scene_subject(sc.get("subject") or "")
    CUR_CLIP = sc.get("clip") if isinstance(sc.get("clip"), dict) and sc["clip"].get("url") else None
    SCENE_CLIPS = [c for c in sc.get("clips", []) if isinstance(c, dict) and c.get("url")] or ([CUR_CLIP] if CUR_CLIP else [])
    presentation_only = bool(sc.get("presentationOnly"))
    vis = sc.get("visual")
    done = False
    if vis:
        gfx_starts.append(t0)
        # Keep the scene's own selected media behind readable presentation text.
        vis = dict(vis, query=q)
        # Paid AI photo as the presentation background (switch "AI presentation backgrounds"):
        # a real-looking photo of the scene's subject behind the text, instead of a plain color.
        if presentation_only and not SCENE_CLIPS and plan.get("ai_presentations") and AI_SPENT < plan.get("image_budget", 20):
            try:
                subject_ = str(sc.get("subject") or sc.get("query") or "").strip()
                _img = gen_ai_image("Photorealistic cinematic editorial photo, real lighting, 35mm, United States setting, wide composition "
                                    "with calm darker space for overlaid text. Show literally and only: " + subject_ +
                                    ". Context sentence: " + str(sc.get("text", ""))[:220] +
                                    ". No text, letters, numbers, signs, logos, cartoons, CGI, nudity or insects. "
                                    "If the subject is an object, show only the object with no people.", "1536x1024")
                _ip = os.path.join(work, f"aibg{i}.png"); _img.convert("RGB").save(_ip)
                _frames = int((d + 0.1) * 30) + 1
                _kb = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
                       f"zoompan=z='min(zoom+0.0006,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={_frames}:s={W}x{H}:fps=30,setsar=1")
                _bp = os.path.join(work, f"aibg{i}.mp4")
                run(["ffmpeg", "-y", "-loop", "1", "-i", _ip, "-t", f"{d + 0.1:.2f}", "-vf", _kb,
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", _bp])
                vis["_aibg"] = _bp
                AI_SPENT += 1
            except Exception as e:
                print("AI presentation background skipped:", e)
        # Keep the graphic and its approved background throughout this spoken explanation.
        gd = d
        gseg = os.path.join(work, f"g{i}.mp4")
        if render_visual(vis, gd, gseg, i):
            rest = d - gd
            fseg = os.path.join(work, f"f{i}.mp4")
            if rest < 0.4:
                os.replace(gseg, seg); done = True
            elif make_scene_footage(i, q, rest, fseg):
                lst = os.path.join(work, f"gf{i}.txt")
                with open(lst, "w") as f:
                    f.write(f"file '{gseg}'\nfile '{fseg}'\n")
                run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-t", f"{d:.2f}",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
                done = True
    if presentation_only and not done:
        raise RuntimeError(f"Could not render presentation for scene {i}; refusing to use unrelated footage")
    if not done:
        done = make_scene_footage(i, q, d, seg, UNSPLASH and i >= 3 and i % 3 == 2)
    if not done and SCENE_SUBJECT and SCENE_SUBJECT != q.lower():
        # Same subject, simpler search (e.g. just "eggs") — clip first, then a photo with slow motion.
        try:
            done = make_footage(i, SCENE_SUBJECT, d, seg, False)
        except Exception as e:
            print("Subject fallback skipped:", e)
    if not done:
        set_scene_subject("")  # place fallback: the place itself is the subject
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
    run(["ffmpeg", "-y", "-i", a, "-af", f"apad=whole_dur={d:.2f}", "-t", f"{d:.2f}", "-ar", "44100", "-ac", "2", pad])
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
    card_spans.append((i, t0, d, bool(vis) or presentation_only))
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
# Re-encode (not -c copy): segments come from different sources (HeyGen, photos, stock clips)
# with different timebases, and a stream-copy concat can produce broken timestamps that make
# the joined video shorter than the sum of its scenes.
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", os.path.join(work, "v.txt"),
     "-vf", "fps=30,setsar=1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
     os.path.join(work, "video.mp4")])
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", os.path.join(work, "a.txt"), os.path.join(work, "voice.wav")])
print(f"Joined video: {duration(os.path.join(work, 'video.mp4')):.1f}s, voice: {duration(os.path.join(work, 'voice.wav')):.1f}s, expected {t0:.1f}s")


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
    # Generate only ~60s of seamless loopable music and loop it under the whole video (saves ~80% of music cost).
    have_music = el_audio("https://api.elevenlabs.io/v1/music", {"prompt": prompt + ", instrumental, no vocals, seamless loopable background track, engaging rhythmic pulse, subtle percussion fills and evolving accents every 8 seconds, controlled energy, no big drops or endings", "music_length_ms": int(min(total, 60) * 1000)}, music) \
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
        filters.append(f"[{n}:a]atrim=0:{total:.2f},loudnorm=I=-20:TP=-3:LRA=7,volume=0.38,afade=t=in:d=0.6,afade=t=out:st={max(0, total - 2):.2f}:d=2[mraw]")
        filters.append("[mraw][sc]sidechaincompress=threshold=0.035:ratio=4:attack=15:release=220[m]")
        labels.append("[m]"); n += 1
    else:
        filters[0] = "[0:a]anull[vo]"

    def place(key, times, vol, tag):
        global n
        for j, s_ in enumerate(times):
            inputs.extend(["-i", sfx[key]])
            ms = max(0, int(s_ * 1000))
            filters.append(f"[{n}:a]loudnorm=I=-23:TP=-4:LRA=5,volume={vol},afade=t=in:d=0.015,afade=t=out:st={max(0, duration(sfx[key]) - 0.08):.3f}:d=0.08,adelay={ms}|{ms}[{tag}{j}]"); labels.append(f"[{tag}{j}]"); n += 1

    if "hit" in sfx:
        place("hit", [0.0] + ([starts[len(starts) // 2]] if len(starts) > 6 else []), 0.55, "h")
    # Cue actual scene changes, not arbitrary moments; avoid a barrage on short scenes.
    cuts = []
    for s_ in starts[1:]:
        cue = max(0.0, s_ - 0.25)
        if cue >= 2.0 and cue < total - 0.8 and (not cuts or cue - cuts[-1] >= 4.0):
            cuts.append(cue)
        if len(cuts) >= 50:
            break
    if "cut" in sfx:
        place("cut", cuts[0::2], rng.uniform(0.32, 0.42), "w")
    if "cut2" in sfx:
        place("cut2", cuts[1::2], rng.uniform(0.32, 0.42), "x")
    if "riser" in sfx and len(starts) > 4:
        place("riser", [max(0, starts[-1] - 2.0)], 0.25, "r")
    if "pop" in sfx:
        place("pop", [s_ for s_ in gfx_starts[:40] if all(abs(s_ - cue) > 1.2 for cue in cuts)], 0.25, "p")
    if n > 1:
        filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0,alimiter=limit=0.89:level=0:latency=1,atrim=0:{total:.2f}[out]")
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
# Overlays written straight onto the footage (no white card):
#  - AI pictures (switch on): up to 20 photoreal 1K pictures spread across the video, shown full screen for
#    10s each with the headline and the scene's key number written on the image. With the owner's OpenAI key
#    (mini, low quality) all 20 cost ~$0.12; without it Lovable AI is used (~2-3 cents each).
#  - Data callouts (free): numbers from the narration ($, %, big figures) written big over the footage for 6s.
card_inputs, card_filters = [], []
import re as _re
from PIL import ImageOps, ImageFilter
NUM_RE = _re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|billion|trillion|k|K))?|\b\d+(?:\.\d+)?\s?(?:%|percent)|\b\d{1,3}(?:,\d{3})+\b")
def key_number(text):
    m = NUM_RE.search(str(text or ""))
    return m.group(0).replace("percent", "%").replace(" %", "%").strip() if m else ""
def fit_font(dr, txt, size, maxw):
    f = ImageFont.truetype(FB, size)
    while size > 16 and dr.textlength(txt, font=f) > maxw:
        size -= 2; f = ImageFont.truetype(FB, size)
    return f, size
def write_text(im, head, num):
    dr = ImageDraw.Draw(im); w, h = im.size; m = int(w * 0.05)
    y = h - m
    if head:
        f, fs = fit_font(dr, head, int(h * 0.075), w - 2 * m); y -= fs
        dr.text((m, y), head, font=f, fill=(255, 255, 255, 255), stroke_width=max(2, fs // 14), stroke_fill=(0, 0, 0, 230))
    if num:
        f, fs = fit_font(dr, num, int(h * 0.16), w - 2 * m); y -= fs + int(h * 0.02)
        dr.text((m, y), num, font=f, fill=(255, 196, 0, 255), stroke_width=max(3, fs // 14), stroke_fill=(0, 0, 0, 230))
def free_slot(s0, s1):
    return all(s1 + 0.3 < a or s0 > b + 0.3 for a, b, _k in card_filters)
if plan.get("ai_cards"):
    pool = [c for c in card_spans if c[0] >= 2 and not c[3] and c[2] >= 3.5]
    # Only as many pictures as the video needs: about 1 per 30s, never more than 20.
    # A 45s short gets 1-2; a 10min video gets up to 20.
    MAX_AI_CARDS = max(0, min(plan.get("image_budget", 20), int((t0 - 5) // 30)) - AI_SPENT)
    # Evenly spread the pictures across the whole video; scenes with a key number are preferred.
    def _spread(lst, k):
        if k <= 0 or not lst: return []
        if len(lst) <= k: return list(lst)
        step = len(lst) / k
        return [lst[min(len(lst) - 1, int(j * step))] for j in range(k)]
    pool.sort(key=lambda c: c[0])
    with_num = [c for c in pool if key_number(plan["scenes"][c[0]].get("text"))]
    without_num = [c for c in pool if not key_number(plan["scenes"][c[0]].get("text"))]
    n_num = min(len(with_num), MAX_AI_CARDS)
    picks = _spread(with_num, n_num) + _spread(without_num, MAX_AI_CARDS - n_num)
    picks = sorted(picks, key=lambda c: c[0])[:MAX_AI_CARDS]
    for n, (i, st, d, _g) in enumerate(picks):
        s0 = st + 0.5; s1 = min(s0 + 10.0, t0 - 0.5)
        if s1 - s0 < 4 or not free_slot(s0, s1): continue  # checked BEFORE paying for the picture
        sc = plan["scenes"][i]
        subject = str(sc.get("subject") or sc.get("query") or "").strip()
        try:
            img = gen_ai_image("Photorealistic cinematic editorial photo, real lighting, 35mm, United States setting, wide composition "
                               "with calm darker space in the lower-left for a headline. Show literally and only: " + subject +
                               ". Context sentence: " + str(sc.get("text", ""))[:220] +
                               ". No text, letters, numbers, signs, logos, cartoons, CGI, nudity or insects. "
                               "If the subject is an object, show only the object with no people.", "1536x1024")
        except Exception as e:
            print("AI picture skipped:", e); continue
        img = ImageOps.fit(img, (W, H), method=Image.Resampling.LANCZOS).convert("RGBA")
        shade = Image.new("RGBA", (W, H), (0, 0, 0, 0)); sd = ImageDraw.Draw(shade)
        for yy in range(H // 2, H):
            sd.line([(0, yy), (W, yy)], fill=(0, 0, 0, int(170 * (yy - H / 2) / (H / 2))))
        img = Image.alpha_composite(img, shade)
        write_text(img, " ".join(subject.upper().split()[:5]), key_number(sc.get("text")))
        cp = os.path.join(work, f"aicard{n}.png"); img.save(cp)
        card_inputs.append(cp); card_filters.append((s0, s1, "full")); AI_SPENT += 1
    print("AI pictures:", len(card_inputs))
data_n = 0
if plan.get("data_callouts", True):
    last_end = -99.0
    for (i, st, d, g) in card_spans:
        if i < 1 or g or d < 3: continue
        sc = plan["scenes"][i]; num = key_number(sc.get("text"))
        if not num: continue
        s0 = st + 0.4; s1 = min(st + d - 0.2, s0 + 6.0)
        if s1 - s0 < 2.5 or s0 < last_end + 8 or not free_slot(s0, s1): continue
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im); m = int(W * 0.05)
        f, fs = fit_font(dr, num, int(H * 0.14), W // 2)
        dr.text((m, int(H * 0.12)), num, font=f, fill=(255, 196, 0, 255), stroke_width=max(3, fs // 12), stroke_fill=(0, 0, 0, 235))
        lab = " ".join(str(sc.get("subject") or sc.get("query") or "").upper().split()[:5])
        if lab:
            f2, fs2 = fit_font(dr, lab, int(H * 0.05), W // 2)
            dr.text((m, int(H * 0.12) + fs + int(H * 0.02)), lab, font=f2, fill=(255, 255, 255, 255), stroke_width=max(2, fs2 // 12), stroke_fill=(0, 0, 0, 235))
        cp = os.path.join(work, f"data{data_n}.png"); im.save(cp); data_n += 1
        card_inputs.append(cp); card_filters.append((s0, s1, "text")); last_end = s1
    print("Data callouts:", data_n)
# Animated topic icons: small photorealistic cut-outs of the exact thing spoken (egg, gas pump...).
# Owner's OpenAI key only, low quality, max 10 per video, same subject reused for free; skipped on any error.
icon_n = 0
if plan.get("topic_icons") and OPENAI:
    icon_cache, icon_paid, last_end = {}, 0, -99.0
    for (i, st, d, g) in card_spans:
        if icon_paid >= 10 or AI_IMAGE_BLOCKED: break
        if i < 1 or d < 2.5: continue
        sc = plan["scenes"][i]
        subject = " ".join(str(sc.get("subject") or sc.get("query") or "").split()[:4]).strip()
        if not subject: continue
        s0 = st + 0.3; s1 = min(st + d - 0.2, s0 + 4.0)
        if s1 - s0 < 2 or s0 < last_end + 5: continue
        key = subject.lower()
        if key not in icon_cache:
            try:
                import io
                ro = requests.post("https://api.openai.com/v1/images/generations",
                                   headers={"Authorization": f"Bearer {OPENAI}", "Content-Type": "application/json"},
                                   json={"model": IMG_MODEL, "prompt": "Single photorealistic product-style cut-out of exactly: " + subject +
                                         ". Real camera photo look, centered, soft studio light, isolated object, transparent background. "
                                         "No text, letters, logos, people, cartoons, anime, illustration or 3D render.",
                                         "size": "1024x1024", "quality": "low", "background": "transparent", "output_format": "png", "n": 1}, timeout=300)
                ro.raise_for_status()
                icon_cache[key] = Image.open(io.BytesIO(base64.b64decode(ro.json()["data"][0]["b64_json"]))).convert("RGBA")
                icon_paid += 1; AI_SPENT += 1
            except Exception as e:
                print("Topic icon skipped:", str(e)[:200])
                if isinstance(e, requests.HTTPError): AI_IMAGE_BLOCKED = True
                continue
        ic = icon_cache[key]; sz = int(min(W, H) * 0.26)
        ic = ic.resize((sz, sz), Image.Resampling.LANCZOS)
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        glow = Image.new("RGBA", (sz, sz), (0, 0, 0, 0)); ImageDraw.Draw(glow).ellipse([sz * 0.08, sz * 0.08, sz * 0.92, sz * 0.92], fill=(255, 255, 255, 60))
        x, y = W - sz - int(W * 0.05), int(H * 0.08)
        im.alpha_composite(glow, (x, y)); im.alpha_composite(ic, (x, y))
        cp = os.path.join(work, f"icon{icon_n}.png"); im.save(cp); icon_n += 1
        card_inputs.append(cp); card_filters.append((s0, s1, "icon")); last_end = s1
print("Topic icons:", icon_n)

def card_chain(src, first_idx):
    # full-screen AI pictures fade in/out; data callouts fade over the footage
    chain, cur = [], src
    for k, (s0, s1, kind) in enumerate(card_filters):
        nxt = f"c{k}"
        chain.append(f"[{first_idx + k}:v]format=rgba,fade=t=in:st={s0:.2f}:d=0.4:alpha=1,fade=t=out:st={s1 - 0.4:.2f}:d=0.4:alpha=1[o{k}]")
        chain.append(f"[{cur}][o{k}]overlay=0:0:enable='between(t,{s0:.2f},{s1:.2f})'[{nxt}]")
        cur = nxt
    return chain, cur

if frame != "none" and os.path.exists(ov):
    cc, last = card_chain("s", 3)
    run(["ffmpeg", "-y", "-i", os.path.join(work, "video.mp4"), "-i", os.path.join(work, "voice.wav"), "-loop", "1", "-i", ov,
         *sum([["-loop", "1", "-i", c] for c in card_inputs], []),
         "-filter_complex", ";".join([f"[0:v]subtitles={os.path.join(work, 'subs.ass')}[s]"] + cc + [f"[2:v]scale={W}:{H}[fr];[{last}][fr]overlay=0:0:eof_action=pass[v]"]),
         "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{t0:.2f}", "-shortest", "-movflags", "+faststart", out])
else:
    cc, last = card_chain("s", 2)
    run(["ffmpeg", "-y", "-i", os.path.join(work, "video.mp4"), "-i", os.path.join(work, "voice.wav"),
         *sum([["-loop", "1", "-i", c] for c in card_inputs], []),
         "-filter_complex", ";".join([f"[0:v]subtitles={os.path.join(work, 'subs.ass')}[s]"] + cc + [f"[{last}]null[v]"]), "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{t0:.2f}", "-shortest", "-movflags", "+faststart", out])

# Never ship a broken file: the finished video must be about as long as the narration.
_dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", out],
                            capture_output=True, text=True).stdout.strip() or 0)
print(f"Final video length: {_dur:.1f}s (expected {t0:.1f}s)")
if _dur < max(3.0, t0 * 0.8):
    raise SystemExit(f"Final video is too short ({_dur:.1f}s of {t0:.1f}s) - render failed")

desc = plan.get("description", "")
credit_lines = []
if credits:
    credit_lines.append("Videos: " + ", ".join(sorted(credits)))
if photo_credits:
    credit_lines.append("Photos: " + ", ".join(sorted(photo_credits)) + " on Unsplash")
if credit_lines:
    disclosure = "For entertainment and informational purposes only."
    before, separator, after = desc.partition(disclosure)
    if separator:
        ending = separator + after
        credits_text = "\n\n" + "\n".join(credit_lines)
        remaining = max(0, 1000 - len(ending) - len(credits_text) - 2)
        desc = before[:remaining].rstrip() + credits_text + "\n\n" + ending
    else:
        desc = desc[:max(0, 1000 - len("\n\n".join(credit_lines)) - 2)].rstrip() + "\n\n" + "\n\n".join(credit_lines)
meta = {"title": plan.get("title", "")[:100], "description": desc, "tags": plan.get("tags", [])}
json.dump(meta, open(os.path.join(os.path.dirname(out), "meta.json"), "w"), ensure_ascii=False)

# Generate one topic-specific 1K thumbnail from the approved title, not a generic scene.
def make_thumbnail():
    key = os.environ.get("LOVABLE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("AI image key is missing; thumbnail was not generated")
    prompt = ("Create a photorealistic, editorial YouTube thumbnail for a US documentary. "
              "Show the literal subject and location of this video with an instantly legible visual story, "
              "one clear focal point, expressive real lighting and strong contrast. No unrelated objects, "
              "no cartoons, nudity, insects, invented numbers or misleading claims. "
              "No documents, bills, receipts, signs, writing, letters, or numbers anywhere in the image; "
              "leave the left third dark and visually quiet for a short title overlay. Video title: " + str(plan.get("title", ""))[:100] +
              ". Opening scene: " + str((plan.get("scenes") or [{}])[0].get("text", ""))[:200])
    url = "https://ai.gateway.lovable.dev/v1/images/generations"
    body = {"model": "openai/gpt-image-2.5-sunburst", "prompt": prompt,
            "size": "1536x1024", "quality": "low", "stream": True, "partial_images": 1}
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    def request(payload, streamed):
        response = requests.post(url, headers=headers, json=payload, stream=streamed)
        if not response.ok:
            try: message = response.json().get("error", {}).get("message") or response.text[:300]
            except Exception: message = response.text[:300]
            raise RuntimeError("Thumbnail generation failed (" + str(response.status_code) + "): " + str(message))
        return response
    response = request(body, True)
    saw_event, finished, image = False, False, None
    try:
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"): continue
            try: event = json.loads(line[5:].strip())
            except (ValueError, TypeError): continue
            if not isinstance(event, dict): continue
            kind = event.get("type", "")
            if kind == "error":
                raise RuntimeError("Thumbnail generation failed: " + str((event.get("error") or {}).get("message") or "Image generation denied"))
            if kind in ("image_generation.partial_image", "image_generation.completed"):
                saw_event = True
                if kind == "image_generation.completed":
                    finished, image = True, event.get("b64_json")
    finally:
        response.close()
    if not saw_event:
        replay = request({k: v for k, v in body.items() if k not in ("stream", "partial_images")}, False)
        image = (replay.json().get("data") or [{}])[0].get("b64_json")
        finished = bool(image)
    if not finished or not image:
        raise RuntimeError("Thumbnail stream ended without a completed image")
    import io
    from PIL import ImageOps
    picture = Image.open(io.BytesIO(base64.b64decode(image))).convert("RGB")
    picture = ImageOps.fit(picture, (1280, 720), method=Image.Resampling.LANCZOS)
    title_words = str(plan.get("title", "")).split()[:6]
    headline = " ".join(title_words).upper()
    if headline:
        overlay = Image.new("RGBA", picture.size, (0, 0, 0, 0))
        shade = ImageDraw.Draw(overlay)
        shade.rectangle((0, 0, 690, 720), fill=(0, 0, 0, 175))
        font = ImageFont.truetype(FB, 72)
        lines, line = [], ""
        for word in headline.split():
            candidate = (line + " " + word).strip()
            if line and shade.textlength(candidate, font=font) > 580:
                lines.append(line); line = word
            else: line = candidate
        if line: lines.append(line)
        while len(lines) > 4:
            title_words.pop()
            headline = " ".join(title_words).upper()
            lines, line = [], ""
            for word in headline.split():
                candidate = (line + " " + word).strip()
                if line and shade.textlength(candidate, font=font) > 580:
                    lines.append(line); line = word
                else: line = candidate
            if line: lines.append(line)
        for i, text in enumerate(lines):
            shade.text((55, 200 + i * 95), text, font=font, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))
        picture = Image.alpha_composite(picture.convert("RGBA"), overlay).convert("RGB")
    picture.save(os.path.join(os.path.dirname(out), "thumbnail.jpg"), quality=90, optimize=True)

make_thumbnail()
print("Done:", out, f"{t0:.1f}s")
