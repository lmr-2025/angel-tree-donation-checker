"""Offline tests: tag reading, wish definitions and the verdict. No call to Jev."""

import io
import json
import os
import unittest

from PIL import Image

import app

SAMPLES = os.path.join(os.path.dirname(__file__), "samples", "angels")


def photo():
    out = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(out, "PNG")
    return out.getvalue()

ROSTER_TAG = """Sam
MALE - 12 Years

Wish: BIKE AND HELMET
Wish: CRAFT KITS/ART SUPPLIES
Favorite: AVENGERS, SPIDER MAN
Wish: REMOTE CONTROL CARS/ROBOTS
Clothing Type: CHILD BOY
Special Concerns: PLEASE NO CLOTHES"""

PRINTED_TAG = """NAME:\tALEX
GENDER:\tMALE
AGE:\t9 YEARS
CODE NUMBER:\tTAG0000X
NEED:\t-
WISH:\tSCIENCE KITS
WISH:\t-
FAVORITE:\tLEGOS
WISH:\tLEGO SETS
CLOTHING TYPE:\tYouth Male"""


def wish(i, p):
    return {"id": f"wish_{i}", "kind": "wish", "label": f"wish {i}", "bad_if_yes": False}, {f"wish_{i}": {"noul": p}}


def answers(full, small=None, wishes=(0.99,)):
    checklist, a = [], {"strong_count": {"probabilities": {str(k): v for k, v in full.items()}}}
    if small is not None:
        a["small_lego"] = {"probabilities": {str(k): v for k, v in small.items()}}
    for i, p in enumerate(wishes):
        c, w = wish(i, p)
        checklist.append(c)
        a.update(w)
    return checklist, a


class TagTest(unittest.TestCase):
    def test_roster_layout(self):
        tag = app.parse_tag(ROSTER_TAG)
        self.assertEqual((tag["name"], tag["gender"], tag["age"]), ("Sam", "Male", "12"))
        self.assertEqual(tag["wishes"], ["BIKE AND HELMET", "CRAFT KITS/ART SUPPLIES", "REMOTE CONTROL CARS/ROBOTS"])
        self.assertEqual(tag["favorites"], ["AVENGERS, SPIDER MAN"])
        self.assertEqual(tag["concerns"], ["PLEASE NO CLOTHES"])

    def test_printed_layout_skips_blanks(self):
        tag = app.parse_tag(PRINTED_TAG)
        self.assertEqual((tag["name"], tag["age"]), ("Alex", "9"))
        self.assertEqual(tag["wishes"], ["SCIENCE KITS", "LEGO SETS"])
        self.assertEqual(tag["favorites"], ["LEGOS"])

    def test_repeated_wish_kept_once(self):
        tag = app.parse_tag("Jordan\nMALE - 12 Years\nWish: TABLET\nWish: TABLET")
        self.assertEqual(tag["wishes"], ["TABLET"])


    def test_age_in_months(self):
        tag = app.parse_tag("Riley\nFEMALE - 20 Months\nWish: BLUEY TOYS")
        self.assertEqual((tag["age"], tag["age_months"], app.age_band(tag)), ("20 months", 20, "baby"))
        self.assertEqual(app.who(tag), "a 20-month-old")
        self.assertEqual(app.parse_tag("NAME: KAI\nAGE: 18 MONTHS\nWISH: BOOKS")["age_months"], 18)

    def test_sizes_and_code(self):
        tag = app.parse_tag(PRINTED_TAG + "\nSHIRT SIZE:\t7\nPANTS SIZE:\t7/8\nSHOE SIZE:\t13.5\nCOAT SIZE:\t-")
        self.assertEqual(tag["code"], "TAG0000X")
        self.assertEqual(tag["sizes"], {"top": "7", "pants": "7/8", "shoes": "13.5"})


class GuideTest(unittest.TestCase):
    def counts(self, text):
        guide = app.wish_guidance(text)[1]
        return guide and guide["counts"]

    def test_gift_card_is_not_a_game(self):
        self.assertIn("gift card", self.counts("XBOX GIFT CARD"))

    def test_remote_car_needs_a_remote(self):
        self.assertNotIn("die-cast", self.counts("FAST REMOTE CAR"))

    def test_cards_do_not_match_cars(self):
        self.assertNotIn("Toy cars", self.counts("AGE APPROPRIATE BOOKS,PUZZLES, GAMES,CARDS"))

    def test_donor_override(self):
        self.assertEqual(self.counts("TECH TOYS: R/C cars count"), "R/C cars count")

    def test_scooter_fits_the_childs_age(self):
        little = app.wish_guidance("SCOOTER & HELMET", app.parse_tag("Mia\nFEMALE - 4 Years"))[1]
        big = app.wish_guidance("SCOOTER & HELMET", app.parse_tag("Sam\nMALE - 12 Years"))[1]
        self.assertIn("3-wheel light-up scooter", little["yes"])
        self.assertIn("3-wheel toddler scooter", big["no"])

    def test_bike_fits_the_childs_age(self):
        little = app.wish_guidance("BIKE & HELMET", app.parse_tag("Mia\nFEMALE - 5 Years"))[1]
        self.assertIn("12-inch bike with training wheels", little["yes"])
        self.assertNotIn("12-year-old", little["counts"])

    def test_who_fills_in(self):
        g = app.wish_guidance("BOOKS", app.parse_tag("Ana\nFEMALE - 7 Years"))[1]
        self.assertIn("a 7-year-old", g["counts"])
        self.assertNotIn("{who}", app.wish_guidance("BOOKS")[1]["counts"])

    def test_unknown_wish(self):
        self.assertIsNone(self.counts("NARUTO"))


class GradeTest(unittest.TestCase):
    def test_three_strong_is_ready(self):
        self.assertEqual(app.grade(*answers({3: 0.9, 2: 0.1}))["verdict"], "ready")

    def test_two_small_sets_make_one_gift(self):
        g = app.grade(*answers({2: 0.9, 1: 0.1}, small={2: 0.9, 1: 0.1}))
        self.assertEqual(g["verdict"], "ready")

    def test_one_small_set_is_not_enough(self):
        g = app.grade(*answers({2: 0.9, 1: 0.1}, small={1: 0.9, 0: 0.1}))
        self.assertEqual(g["verdict"], "shop")
        self.assertIn("Buy 1 more strong gift.", g["todo"])
        self.assertTrue(any("pair with the odd one" in t for t in g["todo"]))

    def test_unsure_goes_to_a_volunteer(self):
        self.assertEqual(app.grade(*answers({3: 0.5, 2: 0.5}))["verdict"], "check")

    def test_nothing_on_tag_means_buy_three(self):
        g = app.grade(*answers({2: 0.6, 1: 0.4}, wishes=(0.05, 0.1)))
        self.assertEqual(g["verdict"], "shop")
        self.assertIn("Buy 3 more strong gifts.", g["todo"])

    def test_broken_rule_beats_ready(self):
        checklist, a = answers({3: 0.95})
        checklist.append({"id": "concern_0", "kind": "rule", "label": "Tag note: PLEASE NO CLOTHES", "bad_if_yes": False})
        a["concern_0"] = {"noul": 0.1}
        self.assertEqual(app.grade(checklist, a)["verdict"], "fix")

    def test_ready_needs_a_wish_covered(self):
        g = app.grade(*answers({3: 0.9, 2: 0.1}, wishes=(0.1, 0.05)))
        self.assertEqual(g["verdict"], "shop")

    def test_unsure_wishes_are_not_zeroed(self):
        g = app.grade(*answers({3: 0.6, 2: 0.4}, wishes=(0.5,)))
        self.assertEqual((g["verdict"], g["likely"]), ("check", 3))


def clothes(checklist, a, **status):
    for cid, p in status.items():
        checklist.append({"id": f"clothes_{cid}", "kind": "clothes", "label": cid.title(), "bad_if_yes": False})
        a[f"clothes_{cid}"] = {"noul": p}
    return checklist, a


class FullListTest(unittest.TestCase):
    def test_full_list_ready(self):
        g = app.grade(*clothes(*answers({3: 0.9}), top=0.9, pants=0.9, shoes=0.1, coat=0.9), full_list=True)
        self.assertEqual(g["verdict"], "ready")
        self.assertEqual({r["id"]: r["status"] for r in g["rows"]}["clothes_shoes"], "optional")

    def test_shoes_or_coat_missing(self):
        g = app.grade(*clothes(*answers({3: 0.9}), top=0.9, pants=0.9, shoes=0.1, coat=0.2), full_list=True)
        self.assertEqual(g["verdict"], "shop")
        self.assertIn("Buy: Shoes (or coat)", g["todo"])

    def test_every_wish_needed(self):
        g = app.grade(*clothes(*answers({3: 0.9}, wishes=(0.9, 0.1)), top=0.9, pants=0.9, shoes=0.9),
                      full_list=True)
        self.assertEqual(g["verdict"], "shop")
        self.assertIn("Buy: wish 1", g["todo"])

    def test_unreadable_size_goes_to_a_volunteer(self):
        g = app.grade(*clothes(*answers({3: 0.9}), top=0.5, pants=0.9, shoes=0.9), full_list=True)
        self.assertEqual(g["verdict"], "check")

    def test_three_gift_mode_ignores_clothes(self):
        g = app.grade(*clothes(*answers({3: 0.9}), top=0.1))
        self.assertEqual(g["verdict"], "ready")


class RequestTest(unittest.TestCase):
    def build(self, tag, **form):
        return app.build_request({"tag": tag, "gift_photos": [photo()], "rules": "", **form})

    def test_name_and_code_never_sent(self):
        body, _ = self.build(PRINTED_TAG + "\nSHIRT SIZE: 7", goal="full", packing="Science kit")
        sent = json.dumps({k: v for k, v in body.items() if k != "assets"})
        self.assertNotIn("ALEX", sent.upper())
        self.assertNotIn("TAG0000X", sent)

    def test_full_list_asks_about_clothes_in_size(self):
        body, checklist = self.build(PRINTED_TAG + "\nSHIRT SIZE: 7", goal="full")
        self.assertIn("size 7", body["questions"]["clothes_top"]["instructions"]["question"])
        self.assertIn("clothes_shoes", body["questions"])

    def test_no_clothes_note_skips_clothes(self):
        body, _ = self.build(ROSTER_TAG, goal="full")
        self.assertFalse(any(q.startswith("clothes_") for q in body["questions"]))

    def test_toddler_skips_too_young(self):
        body, _ = self.build("Riley\nFEMALE - 20 Months\nWish: DUPLO")
        self.assertNotIn("too_young", body["questions"])

    def test_too_young_is_relative(self):
        body, _ = self.build("Mia\nFEMALE - 4 Years\nWish: LEGO")
        crit = body["questions"]["too_young"]["criteria"]
        self.assertIn("DUPLO set marked 18m+", crit["false"]["examples"])
        self.assertIn("a 4-year-old", crit["true"]["meaning"])

    def test_packing_list(self):
        _, checklist = self.build(ROSTER_TAG, packing="R/C car\nPaint set")
        self.assertEqual([c["label"] for c in checklist if c["kind"] == "packed"], ["R/C car", "Paint set"])


class PackingTest(unittest.TestCase):
    def test_missing_packed_item_comes_first(self):
        checklist, a = answers({3: 0.9})
        checklist.append({"id": "packed_0", "kind": "packed", "label": "Dino tee", "bad_if_yes": False})
        a["packed_0"] = {"noul": 0.05}
        g = app.grade(checklist, a)
        self.assertEqual(g["verdict"], "pack")
        self.assertTrue(g["todo"][0].startswith("Not in the photos: Dino tee"))

    def test_sample_angels_load(self):
        angels = app.load_angels(SAMPLES)
        sam = angels["TAG0001A"]
        tag = app.parse_tag(sam["tag"])
        self.assertEqual((tag["name"], tag["age"], tag["code"]), ("Sam", "5", "TAG0001A"))
        self.assertEqual(tag["sizes"]["shoes"], "12")
        self.assertEqual(tag["concerns"], ["No sequins"])
        self.assertIn("Jeans size 6", sam["packing"])
        self.assertEqual(sam["to_buy"], ["NEED: scooter and helmet"])
        self.assertEqual(app.parse_tag(angels["TAG0002B"]["tag"])["age_months"], 20)


if __name__ == "__main__":
    unittest.main()
