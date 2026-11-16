"""Angel Tree bag check, powered by Jev (TypeSafe System One).

Run:  TYPESAFE_API_KEY=... python3 app.py      then open http://localhost:8000
      python3 app.py --dry-run                  builds the Jev request without sending it

A volunteer pastes a child's tag and adds photos of the bag. The goal is three strong
gifts per child; if the bag has fewer, the volunteer goes shopping. Jev counts the strong
gifts and answers one yes/no question per wish, and plain code below turns those
probabilities into a verdict and a shopping list.
"""

import argparse
import base64
import html
import io
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageOps

try:  # iPhone photos are HEIC; Pillow opens them once pillow-heif is installed
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
YES, NO = 0.7, 0.3  # TypeSafe's suggested starting thresholds for a Noul
MAX_SIDE, MIN_SIDE = 1568, 280  # Jev image limits, per side
GOAL = 3  # strong gifts each child should get
SMALL_LEGO = 150  # boxed LEGO sets under this many pieces count as half a strong gift

DEFAULT_RULES = """Gifts are new, in original packaging or with store tags.
Gifts are unwrapped."""

PHOTO_NOTES = ("The photos show one child's gift bag, laid out by a volunteer. Several photos may show the same items "
               "from different angles. Only the gift items count. Ignore background things such as furniture, bottles, "
               "curtains, bags and other household items, and boxes on chairs, on the floor or cut off at the edge "
               "of the photo, which are not part of the bag.")

STRONG = {
    "is": "A real, full-size gift that clearly matches one of the child's wishes, or is clearly built around their "
          "favorite, and suits their age. A wish can be covered by more than one strong gift, such as two LEGO sets.",
    "is_not": "Any gift that matches none of the child's wishes or favorites, however nice it is (a LEGO set "
              "is not strong for a child who did not ask for LEGO). Also small stocking stuffers, toys made for much "
              "younger children, clothing the child did not ask for, and anything in the background.",
    "leave_out": f"Small LEGO sets, with fewer than {SMALL_LEGO} pieces printed on the box (usually a palm-size "
                 "box), are counted separately, so leave them out of this count. LEGO polybags count as nothing.",
    "examples_strong": ["a 300-piece LEGO set for a child who wants LEGO sets",
                        "a Roblox gift card for a child who wants one",
                        "a LEGO Ninjago set for a child whose favorite is Ninjago"],
    "examples_not_strong": ["LEGO set for a child whose tag never mentions LEGO", "small LEGO set under 150 pieces (counted separately)",
                            "LEGO polybag with a few dozen pieces", "tricycle for a 12-year-old",
                            "baby shape sorter for a 12-year-old", "a card game for a child who only wished for a bike"],
}

TOO_YOUNG = {
    "true": {"meaning": "At least one gift item is made for babies, toddlers or preschoolers",
             "examples": ["baby rattle", "shape sorter", "toddler ride-on", "preschool character playset",
                          "chunky wooden blocks"]},
    "false": {"meaning": "Every gift suits the child or is enjoyed at any age. An age mark like 4+ or 8+ is only a "
                         "minimum age, so older children enjoy those toys too",
              "examples": ["LEGO set marked 6+", "R/C car marked 8+", "card game marked 7+"]},
}
RULE_CRITERIA = {
    "gifts are new, in original packaging or with store tags.": {
        "true": {"meaning": "Every item is in its store box or packaging, or has store tags attached",
                 "examples": ["sealed LEGO box", "R/C car in its retail box", "shirt with the price tag on"]},
        "false": {"meaning": "At least one item looks used, opened or has no packaging or tags",
                  "examples": ["loose toy with no box", "worn shoes", "opened box with parts missing"]}},
    "gifts are unwrapped.": {
        "true": {"meaning": "You can see the items' own boxes or packaging; nothing is covered in gift wrap",
                 "examples": ["toy boxes laid out on a table", "items in a clear store bag"]},
        "false": {"meaning": "At least one item is covered in wrapping paper or sealed in a gift box or gift bag",
                  "examples": ["box wrapped in holiday paper", "item hidden inside a closed gift bag"]}},
}

# What counts for common wish-list words. A donor can override any wish with "Wish: what counts".
# Each entry: words that trigger it, what counts and examples, what doesn't and examples. "replaces"
# drops looser entries the same wish also triggers, e.g. "Xbox gift card" is a gift card, not a game.
GUIDE = [
    {"id": "tablet", "words": ("tablet",),
     "counts": "A tablet computer with a touchscreen",
     "yes": ["iPad", "Amazon Fire HD Kids tablet", "Samsung Galaxy Tab", "Android tablet"],
     "not": "Things that only look like or go with a tablet",
     "no": ["toy laptop", "tablet case on its own", "phone", "LeapFrog preschool learning toy"]},
    {"id": "tech", "words": ("tech", "electronic", "gadget"),
     "counts": "A toy or device that runs on batteries or charging and does something electronic",
     "yes": ["remote-control car", "drone", "robot toy", "coding kit", "kids' camera", "walkie-talkies",
             "headphones", "handheld game"],
     "not": "Toys with no electronics, even if they show machines or vehicles",
     "no": ["LEGO or other building set", "die-cast car", "plush toy", "book", "board game"]},
    {"id": "lego", "words": ("lego",),
     "counts": "A LEGO-brand set, with the red LEGO logo on the box",
     "yes": ["LEGO City", "LEGO Technic", "LEGO Ninjago", "LEGO DUPLO"],
     "not": "Building sets from other brands and toys that are not building sets",
     "no": ["Mega Bloks", "K'NEX", "Magna-Tiles", "R/C car", "action figure"]},
    {"id": "remote", "words": ("remote", "r/c", "rc"), "replaces": ("cars", "robot"),
     "counts": "A toy that is driven or controlled with a remote control that comes in the box",
     "yes": ["R/C car", "R/C monster truck", "drone", "R/C robot"],
     "not": "Vehicles or robots that have no remote control",
     "no": ["die-cast car", "push car", "LEGO car set", "wind-up robot"]},
    {"id": "cars", "words": ("hot wheels", "car"),
     "counts": "Toy cars the child can hold, race or drive",
     "yes": ["Hot Wheels or Matchbox car pack", "toy car race track", "R/C car"],
     "not": "Toys that are not cars, or boxes that only picture cars",
     "no": ["toy train", "puzzle with a car picture", "book about cars", "ride-on toy for toddlers"]},
    {"id": "robot", "words": ("robot",),
     "counts": "A toy robot or a kit for building a robot",
     "yes": ["R/C robot", "programmable robot", "robot-building kit", "transforming robot toy"],
     "not": "Toys that are not robots", "no": ["R/C car", "action figure", "LEGO animal set"]},
    {"id": "drone", "words": ("drone",), "replaces": ("remote",),
     "counts": "A small flying drone with a remote or app control",
     "yes": ["mini drone", "quadcopter", "drone with camera"],
     "not": "Things that do not fly", "no": ["R/C car", "toy plane with no remote", "paper airplane kit"]},
    {"id": "science", "words": ("science", "stem", "educational"),
     "counts": "A kit or toy that teaches a skill through doing: science, coding, building or math",
     "yes": ["volcano kit", "geode cracking kit", "microscope", "snap circuits", "coding robot", "chemistry set"],
     "not": "Toys without a learning activity, and toys for much younger children",
     "no": ["plush toy", "R/C car", "baby shape sorter"]},
    {"id": "book", "words": ("book",),
     "counts": "A book a 12-year-old would read",
     "yes": ["chapter book", "graphic novel", "manga volume", "book box set"],
     "not": "Printed things that come with a toy, and picture books for little kids",
     "no": ["instruction booklet in a toy box", "board book for babies"]},
    {"id": "puzzle", "words": ("puzzle",),
     "counts": "A puzzle to solve", "yes": ["500-piece jigsaw puzzle", "Rubik's cube", "brain-teaser set"],
     "not": "Building sets and toddler puzzles", "no": ["LEGO set", "wooden shape sorter"]},
    {"id": "game", "words": ("game",),
     "counts": "A board game, card game or party game for older kids",
     "yes": ["Connect 4", "Sorry!", "Guess Who", "Pictionary", "Battleship"],
     "not": "Toys for free play, and games for toddlers",
     "no": ["LEGO set", "R/C car", "plush toy"]},
    {"id": "card", "words": ("card",),
     "counts": "Playing cards, a card game or trading cards",
     "yes": ["Go Fish deck", "Skip-Bo", "Pokemon cards", "deck of playing cards"],
     "not": "Other toys and games", "no": ["board game without cards", "LEGO set", "gift card"]},
    {"id": "gift_card", "words": ("gift card",), "replaces": ("card", "video_games"),
     "counts": "A gift card for the exact service or store named in the wish",
     "yes": ["Roblox gift card for a Roblox wish", "Xbox gift card for an Xbox wish"],
     "not": "Gift cards for something else, and things that are not gift cards",
     "no": ["Target or Visa gift card for a Roblox wish", "trading cards", "card game"]},
    {"id": "video_games", "words": ("playstation", "play station", "xbox", "nintendo", "switch", "joy-con", "video game"),
     "replaces": ("game", "card"),
     "counts": "A video game, controller or accessory for the console named in the wish",
     "yes": ["PlayStation 5 game", "Nintendo Switch Joy-Con pair", "Xbox controller"],
     "not": "Games for other consoles and non-video games", "no": ["board game", "Xbox game for a PlayStation wish"]},
    {"id": "game_room", "words": ("game room",), "replaces": ("game",),
     "counts": "Things that equip or decorate a game room for a preteen",
     "yes": ["gaming headset", "LED light strip", "door basketball hoop", "controller stand", "bean bag chair"],
     "not": "Ordinary toys", "no": ["LEGO set", "card game", "plush toy"]},
    {"id": "camera", "words": ("camera",),
     "counts": "An instant camera, its film, or accessories for one",
     "yes": ["Fujifilm Instax Mini camera", "Polaroid Go", "Instax film pack", "Instax album or camera case"],
     "not": "Other cameras and toys", "no": ["toy camera for preschoolers", "phone", "photo frame"]},
    {"id": "bike", "words": ("bike",),
     "counts": "A full-size bicycle that fits a 12-year-old (about 24 to 26 inch wheels), with or without a helmet",
     "yes": ["24-inch mountain bike", "26-inch cruiser bike", "bike with a youth or adult helmet"],
     "not": "Bikes for little kids, and helmets or accessories on their own",
     "no": ["tricycle", "12-inch bike with training wheels", "helmet only", "bike bell or lights"]},
    {"id": "scooter", "words": ("scooter",),
     "counts": "A kick scooter or electric scooter sized for a 12-year-old",
     "yes": ["Razor A5 kick scooter", "electric scooter", "scooter with a helmet"],
     "not": "Toddler scooters and ride-ons", "no": ["3-wheel toddler scooter", "foot-to-floor ride-on", "skateboard"]},
    {"id": "football", "words": ("football",),
     "counts": "An American football or football gear such as a youth helmet",
     "yes": ["youth-size football", "football and helmet set", "youth football helmet"],
     "not": "Other sports balls", "no": ["soccer ball", "basketball", "foam toddler ball"]},
    {"id": "soccer", "words": ("soccer",),
     "counts": "A soccer ball, soccer goal or net, or soccer gear",
     "yes": ["size 5 soccer ball", "pop-up soccer goal", "soccer ball and net set"],
     "not": "Other sports", "no": ["American football", "LEGO soccer display set"]},
    {"id": "barcelona", "words": ("barcelona",),
     "counts": "FC Barcelona clothing or gear",
     "yes": ["Barcelona jersey", "Barcelona hoodie", "Barcelona soccer ball"],
     "not": "Other teams and plain soccer gear", "no": ["Real Madrid jersey", "Real Madrid player merchandise"]},
    {"id": "guitar", "words": ("guitar",),
     "counts": "A real, playable guitar sized for a beginner",
     "yes": ["3/4-size acoustic guitar", "beginner electric guitar kit"],
     "not": "Toy guitars and other instruments", "no": ["toy guitar", "ukulele", "guitar picks only"]},
    {"id": "jewelry", "words": ("jewelry", "earring", "necklace", "ring"),
     "counts": "Jewelry for a preteen", "yes": ["earrings", "necklace", "ring set", "charm bracelet"],
     "not": "Toy jewelry for little kids and things that are not jewelry",
     "no": ["dress-up jewelry for toddlers", "ring toss game", "jewelry box with nothing in it"]},
    {"id": "beauty", "words": ("make-up", "makeup", "perfume", "bath set", "body set"),
     "counts": "Makeup, perfume, body spray, lotion or bath gift sets for a preteen",
     "yes": ["Bath & Body Works gift set", "body mist", "lip gloss set", "eyeshadow palette", "bath bomb set"],
     "not": "Pretend makeup for little kids and unrelated items", "no": ["pretend play makeup", "doll"]},
    {"id": "squishy", "words": ("slime", "nee-doh", "squishy", "squishies"),
     "counts": "Slime, Nee-Doh or squishy stress toys",
     "yes": ["Nee-Doh Nice Cube", "slime kit", "squishy foam toys", "squish ball pack"],
     "not": "Regular toys", "no": ["plush toy", "Play-Doh", "interactive pet toy"]},
    {"id": "craft", "words": ("craft", "art", "arts"),
     "counts": "A craft kit or art supplies a 12-year-old would use",
     "yes": ["diamond painting kit", "bead kit", "painting kit", "markers and sketchbook", "friendship bracelet kit"],
     "not": "Things without a making activity, and toddler crafts",
     "no": ["science kit, even one with paints (that is science, not art)", "single notebook", "LEGO set",
            "toddler coloring book"]},
    {"id": "baking", "words": ("baking",),
     "counts": "A real baking set for kids", "yes": ["kids' baking kit with utensils", "cupcake baking set"],
     "not": "Pretend food", "no": ["pretend play food", "toy kitchen"]},
    {"id": "figures", "words": ("action figure",),
     "counts": "Action figures for a preteen", "yes": ["Marvel Legends figure", "Pokemon figure", "figure multipack"],
     "not": "Plush and preschool figures", "no": ["plush toy", "chunky preschool figure"]},
    {"id": "nerf", "words": ("nerf",),
     "counts": "Nerf blasters, darts or Nerf accessories", "yes": ["Nerf Elite blaster", "Nerf dart refill", "Nerf target set"],
     "not": "Other toys", "no": ["water gun", "toy sword"]},
    {"id": "watch", "words": ("watch",),
     "counts": "A wristwatch or kids' smartwatch", "yes": ["digital sports watch", "kids' smartwatch", "analog watch"],
     "not": "Toy watches and clocks", "no": ["preschool toy watch", "alarm clock"]},
    {"id": "hello_kitty", "words": ("hello kitty",),
     "counts": "Anything featuring Hello Kitty", "yes": ["Hello Kitty bedding", "Hello Kitty lamp", "Hello Kitty plush"],
     "not": "Other characters", "no": ["plain pink blanket", "My Melody-only item"]},
    {"id": "batman", "words": ("batman",),
     "counts": "Anything that features Batman",
     "yes": ["Batman action figure", "LEGO Batmobile", "Batman shirt", "Batman book"],
     "not": "Gifts without Batman, including other superheroes", "no": ["Spider-Man toy", "plain black car"]},
]


# ---------- images ----------

def prepare_image(raw: bytes) -> str:
    """Upright, within Jev's size limits, baseline JPEG, base64."""
    with Image.open(io.BytesIO(raw)) as original:
        img = ImageOps.exif_transpose(original).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    if min(img.size) < MIN_SIDE:
        scale = MIN_SIDE / min(img.size)
        img = img.resize((min(MAX_SIDE, round(img.width * scale)), min(MAX_SIDE, round(img.height * scale))))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return base64.b64encode(out.getvalue()).decode("ascii")


# ---------- the Jev request ----------

def lines(text: str) -> list[str]:
    return [l.strip(" -•\t") for l in (text or "").splitlines() if l.strip(" -•\t")]


def parse_tag(text: str) -> dict:
    """Reads a pasted tag in either layout: "Name / MALE - 12 Years / Wish: ..." or "NAME: ALEX / AGE: 9 YEARS / WISH: ..."."""
    tag = {"name": "", "age": "", "gender": "", "wishes": [], "favorites": [], "concerns": [], "clothing": ""}
    for line in lines(text):
        key, sep, value = line.partition(":") if ":" in line else line.partition("\t")
        key, value = key.strip().lower(), value.strip()
        if value.strip("-– ") == "":
            value = ""
        if m := re.fullmatch(r"(male|female)\s*-\s*(\d+)\s*years?", line.strip(), re.I):
            tag["gender"], tag["age"] = m.group(1).title(), m.group(2)
        elif not sep:
            tag["name"] = tag["name"] or line
        elif not value:
            continue
        elif key == "name":
            tag["name"] = value.title()
        elif key == "gender":
            tag["gender"] = value.title()
        elif key == "age":
            tag["age"] = (re.match(r"\d+", value) or [value])[0]
        elif key.startswith(("wish", "need")):
            if value.lower() not in (w.lower() for w in tag["wishes"]):
                tag["wishes"].append(value)
        elif key.startswith("favorite"):
            tag["favorites"].append(value)
        elif key.startswith("special"):
            tag["concerns"].append(value)
        elif key.startswith("clothing"):
            tag["clothing"] = value
    return tag


def wish_guidance(line: str) -> tuple[str, dict | None]:
    """Splits "Wish: what counts" and finds built-in definitions for words it recognises."""
    wish, _, own = line.partition(":")
    wish, own = wish.strip(), own.strip()
    if own:
        return wish, {"counts": own, "yes": [], "not": "Gifts that do not match that description", "no": []}
    hits = [g for g in GUIDE if any(re.search(rf"\b{re.escape(w)}s?\b", wish, re.I) for w in g["words"])]
    dropped = {r for h in hits for r in h.get("replaces", ())}
    hits = [h for h in hits if h["id"] not in dropped]
    if not hits:
        return wish, None
    return wish, {"counts": "; or ".join(h["counts"] for h in hits), "yes": [e for h in hits for e in h["yes"]],
                  "not": "; ".join(h["not"] for h in hits), "no": [e for h in hits for e in h["no"]]}


def fit_criteria(guide: dict | None) -> dict:
    """Matching yes/no descriptions so Jev can compare the two sides directly."""
    if not guide:
        return {"true": "A gift item in the photos fits the wish.",
                "false": "None of the gift items fit the wish, or a fit only appears in the background."}
    yes = {"meaning": f"At least one gift item is this: {guide['counts']}"}
    no = {"meaning": f"No gift item fits. Not counted: {guide['not']}, or things only in the background"}
    if guide["yes"]:
        yes["examples"] = guide["yes"]
    if guide["no"]:
        no["examples"] = guide["no"]
    return {"true": yes, "false": no}


def child(tag: dict) -> dict:
    who = {k: tag[k] for k in ("age", "gender", "wishes", "favorites") if tag[k]}
    if tag["age"]:
        who["age"] = f"{tag['age']} years old"
    return who


def build_request(form: dict) -> tuple[dict, list[dict]]:
    """Returns the API body and the checklist describing each question."""
    tag = parse_tag(form.get("tag"))
    assets, state = {}, {}

    def add(raw):
        ref = f"img-{uuid.uuid4()}"
        assets[ref] = {"type": "image", "format": "jpg", "data_base64": prepare_image(raw)}
        return ref

    state["gift_photos"] = [add(raw) for raw in form["gift_photos"]]
    state["photo_notes"] = PHOTO_NOTES
    state["child"] = child(tag)

    questions, checklist = {}, []

    def ask(qid, kind, label, instructions, bad_if_yes=False, criteria=None):
        q = {"type": "noul", "instructions": instructions}
        if criteria:
            q["criteria"] = criteria
        questions[qid] = q
        checklist.append({"id": qid, "kind": kind, "label": label, "bad_if_yes": bad_if_yes})

    if any(g and "LEGO" in g["counts"] for g in (wish_guidance(x)[1] for x in tag["wishes"] + tag["favorites"])):
        questions["small_lego"] = {  # only worth half a gift each, and only when LEGO is on the tag
            "type": "score",
            "instructions": {"question": f"How many small boxed LEGO sets, with fewer than {SMALL_LEGO} pieces, are "
                                         "in `gift_photos`?",
                             "how": "Read the piece count (pcs) printed on each LEGO box. Count each box once, even "
                                    "if it appears in several photos. Do not count polybags or larger sets."},
            "criteria": ["None", "1 small set", "2 small sets", "3 small sets", "4 or more small sets"],
        }
    questions["strong_count"] = {
        "type": "score",
        "instructions": {
            "question": "How many separate strong gifts for `child` are in `gift_photos`, not counting small LEGO sets?",
            "strong_gift": STRONG,
            "what_each_wish_means": {w: g["counts"] for w, g in (wish_guidance(x) for x in tag["wishes"]) if g},
            "counting": "Count each physical item once, even if it appears in several photos.",
        },
        "criteria": ["No strong gifts", "1 strong gift", "2 strong gifts", "3 or more strong gifts"],
    }

    for i, line in enumerate(tag["wishes"]):
        wish, guide = wish_guidance(line)
        ask(f"wish_{i}", "wish", wish,
            {"question": "Does at least one gift in `gift_photos` fit `wish`?", "wish": wish},
            criteria=fit_criteria(guide))
    for i, fav in enumerate(tag["favorites"]):
        _, guide = wish_guidance(fav)
        ask(f"fav_{i}", "favorite", f"Favorite: {fav}",
            {"question": "Is at least one gift in `gift_photos` built around `favorite`?", "favorite": fav},
            criteria=fit_criteria(guide))

    ask("too_young", "flag", "Nothing meant for much younger kids",
        {"question": "Is any gift item in `gift_photos` made for children much younger than `child`?",
         "check": "Look for baby, toddler and preschool toys."},
        bad_if_yes=True, criteria=TOO_YOUNG)
    for i, concern in enumerate(tag["concerns"]):
        ask(f"concern_{i}", "rule", f"Tag note: {concern}",
            {"question": "Does the bag in `gift_photos` respect `note`? Answer yes if nothing in the bag is affected.",
             "note": concern})
    for i, rule in enumerate(lines(form.get("rules"))):
        ask(f"rule_{i}", "rule", rule,
            {"question": "Do the gift items in `gift_photos` follow `rule`?", "rule": rule},
            criteria=RULE_CRITERIA.get(rule.lower()))

    body = {"model": MODEL, "state": state, "assets": assets, "questions": questions}
    return body, checklist


def call_jev(body: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "TypeSafe-Danger-Zone-Images": "true"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Jev returned {e.code}: {e.read().decode(errors='replace')[:500]}") from None


# ---------- turning probabilities into a verdict ----------

def grade(checklist: list[dict], answers: dict) -> dict:
    rows = []
    for c in checklist:
        p = answers.get(c["id"], {}).get("noul")
        good = None if p is None else (1 - p if c["bad_if_yes"] else p)
        status = "unknown" if good is None else "met" if good >= YES else "missing" if good <= NO else "unsure"
        rows.append({**c, "p": p, "status": status})

    count = answers.get("strong_count", {})
    full = {int(k): v for k, v in count.get("probabilities", {}).items()}
    small = {int(k): v for k, v in answers.get("small_lego", {}).get("probabilities", {"0": 1.0}).items()}
    probs = {}  # strong gifts = full-size ones + half a gift per small LEGO set, rounded down, capped at GOAL
    for i, pi in full.items():
        for j, pj in small.items():
            total = min(GOAL, i + j // 2)
            probs[total] = probs.get(total, 0.0) + pi * pj
    small_likely = max(small, key=small.get)
    p_goal = probs.get(GOAL, 0.0)
    likely = max(probs, key=probs.get) if probs else 0
    short = max(1, GOAL - likely)

    if not any(r["status"] == "met" for r in rows if r["kind"] in ("wish", "favorite")):
        likely, short = 0, GOAL  # nothing on the tag is covered, so nothing in the bag can be strong
    rule_fail = [r for r in rows if r["kind"] == "rule" and r["status"] == "missing"]
    wishes = [r for r in rows if r["kind"] == "wish"]
    open_wishes = [r for r in wishes if r["status"] != "met"]

    todo = [f"Fix: {r['label']}" for r in rule_fail]
    todo += [f"Swap out the toy meant for little kids" if r["status"] == "missing"
             else "Double-check nothing is meant for little kids (Jev wasn't sure)"
             for r in rows if r["id"] == "too_young" and r["status"] != "met"]
    if rule_fail:
        verdict = "fix"
    elif p_goal >= YES:
        verdict = "ready"
    elif p_goal <= NO:
        verdict = "shop"
        todo.insert(0, f"Buy {short} more strong gift{'s' if short > 1 else ''}.")
        if short == 1 and small_likely % 2:
            todo.append(f"Idea: one more small LEGO set, to pair with the odd one in the bag")
        todo += [f"Idea: {r['label']}" for r in open_wishes] or ["Idea: a second gift for any wish on the tag"]
    else:
        verdict = "check"
        todo.insert(0, "A volunteer should look: Jev isn't sure the bag has 3 strong gifts.")
    if small_likely:
        todo.append(f"Counted {small_likely}{'+' if small_likely == 4 else ''} small LEGO set"
                    f"{'s' if small_likely > 1 else ''} (under {SMALL_LEGO} pieces) as half a gift each.")
    todo += [f"Double-check: {r['label']} (Jev wasn't sure)" for r in rows
             if r["status"] == "unsure" and r["kind"] in ("rule", "wish")]
    return {"verdict": verdict, "rows": rows, "todo": todo, "probs": probs, "likely": likely,
            "full": full, "small": small, "wishes_met": len(wishes) - len(open_wishes), "wishes_total": len(wishes)}


# ---------- web page ----------

CSS = """
:root{--bg:#f3f5f1;--surface:#fff;--ink:#1d2a24;--muted:#5d6b64;--line:#d9e0db;--pine:#1f5c45;--pine-soft:#e3efe8;
--berry:#b0263f;--berry-soft:#f8e4e8;--kraft:#e7d3ae;--kraft-ink:#4a3a1f;--amber:#a46200;--amber-soft:#fbefd9}
@media (prefers-color-scheme:dark){:root{--bg:#111a16;--surface:#18241f;--ink:#e6efe9;--muted:#9aaca3;--line:#2b3a33;
--pine:#6fc79f;--pine-soft:#1d3a2e;--berry:#f07a8f;--berry-soft:#3a1c23;--kraft:#6a5634;--kraft-ink:#f6e8cc;
--amber:#f0b45a;--amber-soft:#3a2c14;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif;padding:28px 16px 56px}
.wrap{max-width:760px;margin:0 auto;display:grid;gap:22px}h1{font:400 2.2rem/1.1 Georgia,serif;margin:0}
.sub{color:var(--muted);margin:6px 0 0}
form{background:var(--kraft);color:var(--kraft-ink);border-radius:6px 22px 22px 6px;padding:24px;display:grid;gap:16px}
label{display:grid;gap:6px;font-weight:600;font-size:.95rem}
.lbl{font:500 .72rem/1 ui-monospace,monospace;letter-spacing:.1em;text-transform:uppercase;opacity:.8}
input[type=text],textarea{font:15px/1.4 system-ui,sans-serif;padding:9px 11px;border-radius:9px;border:1px solid #0002;background:var(--surface);color:var(--ink);width:100%}
textarea{min-height:76px}.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px}
small{font-weight:400;opacity:.8}button{justify-self:start;border:0;border-radius:999px;padding:12px 22px;font:600 1rem system-ui;background:var(--pine);color:var(--surface);cursor:pointer}
.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;display:grid;gap:10px}
.pill{justify-self:start;font:700 .8rem/1 system-ui;text-transform:uppercase;letter-spacing:.04em;padding:7px 12px;border-radius:999px}
.ready{background:var(--pine-soft);color:var(--pine)}.check{background:var(--amber-soft);color:var(--amber)}.shop,.fix{background:var(--berry-soft);color:var(--berry)}
.bars{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}.bar{border:1px solid var(--line);border-radius:10px;padding:8px;text-align:center;font-size:.85rem}
.bar b{display:block;font:600 1.1rem ui-monospace,monospace}.bar.on{border-color:var(--pine);background:var(--pine-soft)}
ul.checks{list-style:none;margin:0;padding:0;border:1px solid var(--line);border-radius:14px;background:var(--surface)}
ul.checks li{display:grid;grid-template-columns:28px 1fr auto;gap:10px;align-items:center;padding:11px 14px;border-top:1px solid var(--line)}
ul.checks li:first-child{border-top:0}.m{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px}
.met .m{background:var(--pine-soft);color:var(--pine)}.unsure .m,.unknown .m{background:var(--amber-soft);color:var(--amber)}.missing .m{background:var(--berry-soft);color:var(--berry)}
.p{font:500 .8rem ui-monospace,monospace;color:var(--muted);font-variant-numeric:tabular-nums}.kind{font:500 .68rem ui-monospace,monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.err{background:var(--berry-soft);color:var(--berry);padding:14px 16px;border-radius:12px}pre{overflow:auto;font-size:12px}
"""

VERDICT = {"ready": "3 strong gifts: ready", "check": "Volunteer: take a look", "shop": "Needs shopping",
           "fix": "Fix the bag"}
LEVELS = ["0", "1", "2", "3+"]
TAG_EXAMPLE = "Sam&#10;MALE - 12 Years&#10;Wish: BIKE AND HELMET&#10;Wish: CRAFT KITS/ART SUPPLIES&#10;Favorite: AVENGERS, SPIDER MAN&#10;Wish: REMOTE CONTROL CARS/ROBOTS&#10;Special Concerns: PLEASE NO CLOTHES"
MARK = {"met": "✓", "unsure": "?", "unknown": "?", "missing": "✕"}


def page(body_html: str, form: dict | None = None) -> bytes:
    f = {k: html.escape(str(v)) for k, v in (form or {}).items() if isinstance(v, str)}
    rules = f.get("rules", html.escape(DEFAULT_RULES))
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Angel Tree Bag Check</title><style>{CSS}</style></head><body><div class="wrap">
<header><h1>Angel Tree Bag Check</h1><p class="sub">Paste the child's tag and add photos of the bag. Jev counts the strong gifts: 3 means it's ready, fewer means shopping.</p></header>
{body_html}
<form method="post" enctype="multipart/form-data">
  <label>The child's tag <small>paste it as printed: name, age, Wish:, Favorite:, Special Concerns:. Add what counts after a wish, e.g. "Wish: TECH TOYS: R/C cars, robots"</small>
    <textarea name="tag" rows="8" placeholder="{TAG_EXAMPLE}" required>{f.get('tag','')}</textarea></label>
  <label><span class="lbl">Bag photos</span><input type="file" name="gift_photos" accept="image/*" multiple required>
    <small>Take everything out of the bag and lay it on a plain table, labels facing up. Lay LEGO boxes flat so the
    piece count shows. iPhone (HEIC), JPEG or PNG.</small></label>
  <label>Drop-off rules <small>one per line, edit to match your site</small><textarea name="rules">{rules}</textarea></label>
  <button type="submit">Check the bag</button>
</form></div></body></html>""".encode()


def pct(r: dict) -> str:
    if r["p"] is None:
        return "—"
    return format(1 - r["p"] if r["bad_if_yes"] else r["p"], ".0%")


def result_html(g: dict, tag: dict, model: str) -> str:
    rows = "".join(
        f'<li class="{r["status"]}"><span class="m">{MARK[r["status"]]}</span><div>{html.escape(r["label"])}'
        f'<div class="kind">{r["kind"]}</div></div><span class="p">{pct(r)}</span></li>'
        for r in g["rows"])
    bars = "".join(f'<div class="bar{" on" if i == g["likely"] else ""}"><b>{g["probs"].get(i, 0):.0%}</b>{LEVELS[i]} strong</div>'
                   for i in range(len(LEVELS)))
    todo = "".join(f"<li>{html.escape(t)}</li>" for t in g["todo"]) or "<li>Nothing to add. Attach the tag and drop it off.</li>"
    who = html.escape(" · ".join(x for x in (tag["name"], tag["gender"], tag["age"] and f"{tag['age']} years") if x))
    return f"""<div class="card"><div class="kind">{who}</div><span class="pill {g['verdict']}">{VERDICT[g['verdict']]}</span>
<div class="bars">{bars}</div>
<div>{g['wishes_met']} of {g['wishes_total']} wishes covered</div><ul>{todo}</ul>
<div class="p">Answered by {html.escape(model)} · percent = Jev's probability</div></div>
<ul class="checks">{rows}</ul>"""


def parse_form(handler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    raw = b"Content-Type: " + handler.headers["Content-Type"].encode() + b"\r\n\r\n" + handler.rfile.read(length)
    msg = BytesParser(policy=email_policy).parsebytes(raw)
    form = {"gift_photos": []}
    for part in msg.iter_parts():
        name, data = part.get_param("name", header="content-disposition"), part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            if not data:
                continue
            if name == "gift_photos":
                form["gift_photos"].append(data)
            else:
                form[name] = data
        else:
            form[name] = data.decode("utf-8", errors="replace").strip()
    return form


class Handler(BaseHTTPRequestHandler):
    dry_run = False

    def send(self, content: bytes, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        self.send(page(""))

    def do_POST(self):
        form = parse_form(self)
        try:
            if not form["gift_photos"]:
                raise ValueError("Add at least one gift photo.")
            tag = parse_tag(form.get("tag"))
            if not tag["wishes"]:
                raise ValueError("Paste the child's tag, with at least one line starting with Wish:")
            body, checklist = build_request(form)
            if self.dry_run:
                preview = json.loads(json.dumps(body))
                for a in preview["assets"].values():
                    a["data_base64"] = f"<{len(a['data_base64'])} base64 chars>"
                self.send(page(f'<div class="card"><b>Dry run: this is what would go to Jev</b>'
                               f'<pre>{html.escape(json.dumps(preview, indent=2))}</pre></div>', form))
                return
            key = os.environ.get("TYPESAFE_API_KEY")
            if not key:
                raise ValueError("Set TYPESAFE_API_KEY before starting the app.")
            resp = call_jev(body, key)
            self.send(page(result_html(grade(checklist, resp["answers"]), tag, resp.get("model", MODEL)), form))
        except (ValueError, RuntimeError, OSError) as e:
            self.send(page(f'<div class="err">{html.escape(str(e))}</div>', form))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dry-run", action="store_true", help="show the Jev request instead of sending it")
    args = ap.parse_args()
    Handler.dry_run = args.dry_run
    print(f"Angel Tree Gift Check on http://localhost:{args.port}" + (" (dry run)" if args.dry_run else ""))
    ThreadingHTTPServer(("", args.port), Handler).serve_forever()
