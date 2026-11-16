"""Offline tests: tag reading, wish definitions and the verdict. No call to Jev."""

import unittest

import app

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


if __name__ == "__main__":
    unittest.main()
