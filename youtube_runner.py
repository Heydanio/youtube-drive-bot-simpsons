# youtube_runner.py — Publie 5 Shorts/jour depuis Google Drive (compte YT #2)
import base64, io, json, os, random, subprocess, sys, tempfile
from pathlib import Path
from typing import List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ----- PARAMS -----
PARIS_TZ = ZoneInfo("Europe/Paris")
SLOTS_HOURS   = [8, 11, 14, 17, 20]
MINUTES_GRID  = list(range(0, 60, 5))
GRACE_MINUTES = 20

USED_FILE     = Path("state/used.json")
SCHEDULE_FILE = Path("state/schedule.json")

# Env
FOLDER_IDS_YT  = [s.strip() for s in os.environ["GDRIVE_FOLDER_IDS_YT"].split(",") if s.strip()]
CLIENT_SECRETS = os.environ.get("CLIENT_SECRETS_FILE", "client_secrets.json")
CREDENTIALS    = os.environ.get("CREDENTIALS_FILE", "youtube_credentials.json")
YT_CATEGORY    = os.environ.get("YT_CATEGORY", "Entertainment")
YT_PRIVACY     = os.environ.get("YT_PRIVACY", "public")

DEFAULT_TAGS = [
    "shorts","humour","drôle","fun","fr","tendance","viral","meme","montage","clip",
    "gaming","stream","twitch","moments","compilation","edit","capcut","reaction","lol","wtf",
    "trend","bestof","france","entertainment","amusant","buzz","highlight","clutch","fails","win",
    "asmr","music","beat","challenge","ironie","parodie","sketch","storytime","live","popculture",
    "anime","manga","film","serie","geek","setup","tips","astuces","howto","inspiration"
]

# ----- TITRES + DESCRIPTIONS -----
DEFAULT_DESCRIPTIONS = [
    "😂 Les meilleurs moments des Simpsons ! N’oublie pas de liker 👍 et de t’abonner 🔔 #shorts",
    "😱 Springfield n’a pas fini de nous surprendre… Like + Abonne-toi pour + de clips Simpsons 💛",
    "🍩 Homer, Bart & toute la famille en 60 secondes ! Abonne-toi pour + de fun 🎬",
    "🔥 Moment culte des Simpsons ! Si t’aimes, lâche un like et partage 😉",
    "🎯 Un classique des Simpsons, version short ! Soutiens avec un 👍 et active la cloche 🔔",
    "💥 Springfield en folie ! Like + Abonne-toi pour + de vidéos exclusives Simpsons 🚀",
    "👨‍👩‍👧‍👦 La famille la plus drôle de la TV ! Aide-nous avec un like et rejoins la team 💛",
    "😂 Si tu ris, t’es obligé de liker 😏 et de t’abonner pour + de moments Simpsons 🎉",
    "📺 Springfield en 1 minute chrono ! Soutiens avec un like et abonne-toi 👊",
    "✨ Un moment culte des Simpsons à ne pas rater ! Like & Abonne-toi maintenant 💫",
]

def format_title(file_name: str) -> str:
    """
    Nettoie le nom du fichier pour un titre plus propre.
    Ex: "2025-07-04 - LE PROFESSEUR FRINK CRÉE UNE FEMME ROBOT IA ! 😱💔"
        -> "Simpsons Short - Le Professeur Frink crée une femme robot IA !"
    """
    stem = Path(file_name).stem
    # Retire les dates, ID ou crochets [xxx]
    cleaned = stem
    # Supprimer les parties style [HhVM9-9scog]
    if "[" in cleaned and "]" in cleaned:
        cleaned = cleaned.split("[")[0].strip()
    # Supprimer les dates en début "2025-07-04 - ..."
    if " - " in cleaned and cleaned[:10].count("-") == 2:
        cleaned = cleaned.split(" - ", 1)[1]
    # Reformer un titre
    return f"Simpsons Short - {cleaned}"[:95]

# ----- STATE -----
def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def _save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def load_used():      return _load_json(USED_FILE, {"used_ids": []})
def save_used(d):     _save_json(USED_FILE, d)
def load_schedule():  return _load_json(SCHEDULE_FILE, {"date": None, "slots": []})
def save_schedule(d): _save_json(SCHEDULE_FILE, d)

def ensure_today_schedule():
    today = datetime.now(PARIS_TZ).date().isoformat()
    sch = load_schedule()
    if sch.get("date") != today or not sch.get("slots"):
        random.seed()
        slots = []
        for h in SLOTS_HOURS:
            m = random.choice(MINUTES_GRID)
            slots.append({"hour": h, "minute": m, "posted": False})
        sch = {"date": today, "slots": slots}
        save_schedule(sch)
    slots_txt = ", ".join(f"{s['hour']:02d}:{s['minute']:02d}" for s in sch["slots"])
    print(f"📅 Planning du {today} (Europe/Paris) → {slots_txt}")
    return sch

def should_post_now(sch):
    now = datetime.now(PARIS_TZ)
    print(f"🫀 Passage cron: {now:%Y-%m-%d %H:%M:%S} (Europe/Paris)")
    today = now.date()
    for slot in sch["slots"]:
        if slot.get("posted"):
            continue
        slot_dt = datetime(year=today.year, month=today.month, day=today.day,
                           hour=slot["hour"], minute=slot["minute"], tzinfo=PARIS_TZ)
        if slot_dt <= now < (slot_dt + timedelta(minutes=GRACE_MINUTES)):
            delay = int((now - slot_dt).total_seconds() // 60)
            if delay > 0:
                print(f"⏱️ Créneau rattrapé (+{delay} min, tolérance {GRACE_MINUTES} min).")
            return slot
    return None

def mark_posted(sch, slot):
    slot["posted"] = True
    save_schedule(sch)

# ----- DRIVE -----
def drive_service():
    SA_JSON_B64 = os.environ.get("GDRIVE_SA_JSON_B64", None)
    if not SA_JSON_B64:
        raise SystemExit("GDRIVE_SA_JSON_B64 manquant pour lire Google Drive.")
    sa_json = json.loads(base64.b64decode(SA_JSON_B64).decode("utf-8"))
    creds = Credentials.from_service_account_info(sa_json, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def list_videos_in_folder(svc, folder_id: str) -> List[dict]:
    q = f"'{folder_id}' in parents and trashed=false"
    fields = "files(id,name,mimeType,size,modifiedTime),nextPageToken"
    page_token = None; out = []
    while True:
        resp = svc.files().list(q=q, spaces="drive", fields=f"nextPageToken,{fields}", pageToken=page_token).execute()
        out.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token: break
    return [f for f in out if f["name"].lower().endswith((".mp4",".mov",".m4v",".webm"))]

def list_all_videos(svc) -> List[dict]:
    allv = []
    for fid in FOLDER_IDS_YT:
        allv.extend(list_videos_in_folder(svc, fid))
    return allv

def pick_one(files: List[dict], used_ids: List[str]) -> dict | None:
    remaining = [f for f in files if f["id"] not in used_ids]
    if not remaining:
        used_ids.clear()
        remaining = files[:]
    random.shuffle(remaining)
    return remaining[0] if remaining else None

def download_file(svc, file_id: str, dest: Path):
    req = svc.files().get_media(fileId=file_id)
    fh = io.FileIO(dest, "wb")
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            print(f"Téléchargement {int(status.progress()*100)}%")

# ----- UPLOAD -----
def upload_youtube(local_path: Path, title: str, description: str, tags: list[str]):
    cmd = [
        "youtube-upload",
        "--client-secrets", CLIENT_SECRETS,
        "--credentials-file", CREDENTIALS,
        "--title", title,
        "--category", YT_CATEGORY,     # nom de catégorie OK (ex: Entertainment)
        "--description", description,
        "--tags", ",".join(tags),
        "--privacy", YT_PRIVACY,
        str(local_path),
    ]
    print("RUN:", " ".join(cmd))
    subprocess.run(cmd, check=True)

def main():
    sch = ensure_today_schedule()
    slot = should_post_now(sch)
    if not slot and os.environ.get("FORCE_POST") == "1":
        slot = {"hour": 99, "minute": 99, "posted": False}
    if not slot:
        now = datetime.now(PARIS_TZ)
        print(f"⏳ {now:%Y-%m-%d %H:%M} (Paris) — pas l'heure tirée. Prochain passage…")
        return

    used = load_used()
    svc = drive_service()
    files = list_all_videos(svc)
    if not files:
        print("Aucune vidéo trouvée.")
        return

    chosen = pick_one(files, used["used_ids"])
    print(f"🎯 Vidéo: {chosen['name']} ({chosen['id']})")

    tmpdir = Path(tempfile.mkdtemp())
    local = tmpdir / chosen["name"]
    print("⬇️ Téléchargement…"); download_file(svc, chosen["id"], local)

    title = format_title(chosen["name"])
    desc  = random.choice(DEFAULT_DESCRIPTIONS)
    tags  = DEFAULT_TAGS

    print(f"📝 Titre: {title}")
    print(f"📝 Description: {desc}")

    try:
        upload_youtube(local, title, desc, tags)
        used["used_ids"].append(chosen["id"]); save_used(used)
        mark_posted(sch, slot)
        print("✅ Upload OK — état/plan du jour mis à jour.")
    except subprocess.CalledProcessError as e:
        print("❌ Upload échec:", e)

if __name__ == "__main__":
    random.seed()
    main()
