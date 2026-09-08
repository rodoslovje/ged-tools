"""In-graph dedupe in narrative_to_gedcom: scoring, merging and reconciling."""
import importlib.util
import os

_SPEC = importlib.util.spec_from_file_location(
    "narrative_to_gedcom",
    os.path.join(os.path.dirname(__file__), "..", "tools", "narrative_to_gedcom.py"),
)
nar = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(nar)


def P(given, surname="Cafuta", **kw):
    p = nar.Person(given=given, surname=surname)
    p.birth = {"date": kw.get("b", ""), "place": ""}
    p.death = {"date": kw.get("d", ""), "place": ""}
    p.father, p.mother = kw.get("father", ""), kw.get("mother", "")
    p.sex, p.stub = kw.get("sex", ""), kw.get("stub", False)
    p.page = kw.get("page", 0)
    p.source = {"page": p.page or 1, "text": kw.get("text", given)}
    return p


# ----------------------------------------------------------------- scoring
def test_identical_birth_date_is_certain():
    assert nar.match_score(P("Jožef", b="5 JUL 1842"),
                           P("Jožef", b="5 JUL 1842"))[0] == 100


def test_name_variants_still_match():
    assert nar.match_score(P("Ivan", b="1842"), P("Janez", b="1842"))


def test_different_birth_dates_never_match():
    assert nar.match_score(P("Jožef", b="5 JUL 1842"),
                           P("Jožef", b="6 JUL 1842")) is None


def test_year_only_does_not_conflict_with_a_full_date():
    assert nar.match_score(P("Jožef", b="1842"), P("Jožef", b="5 JUL 1842"))


def test_contradicting_parents_veto_the_match():
    a = P("Marija", b="1842", father="Jurij")
    b = P("Marija", b="1842", father="Blaž")
    assert nar.match_score(a, b) is None


def test_agreeing_parents_outrank_a_bare_year():
    bare = nar.match_score(P("Marija", b="1842"), P("Marija", b="1842"))[0]
    both = nar.match_score(P("Marija", b="1842", father="Jurij"),
                           P("Marija", b="1842", father="Jurij"))[0]
    assert both > bare


def test_opposite_sex_never_matches():
    assert nar.match_score(P("Vid", b="1842", sex="m"),
                           P("Vid", b="1842", sex="f")) is None


def test_different_surnames_never_match():
    assert nar.match_score(P("Jožef", "Cafuta", b="1842"),
                           P("Jožef", "Krajnc", b="1842")) is None


def test_name_alone_scores_below_the_default_threshold():
    assert nar.match_score(P("Jožef"), P("Jožef"))[0] < 85


# ------------------------------------------------------------------ merging
def _tree(people, fams=()):
    t = nar.Tree()
    for p in people:
        t.add_person(p)
        p.fams = []
    for husb, wife, chil in fams:
        f = t.add_family(husb, wife)
        f["chil"] = list(chil)
        for x in (husb, wife):
            if x is not None:
                x.fams.append((f, wife if x is husb else husb))
        for c in chil:
            c.famc = f
    return t


def test_a_real_entry_beats_a_spouse_stub_as_master():
    full = P("Urša", b="4 SEP 1843", d="22 AUG 1925", text="Urša Kmetec, rojena...")
    stub = P("Urša", b="4 SEP 1843", stub=True, text="Jakob ... z Uršo Kmetec")
    t = _tree([stub, full])          # stub first, so order cannot decide it
    assert nar.dedupe_people(t) == 1
    assert t.indi == [full]
    assert full.death["date"] == "22 AUG 1925"


def test_merging_keeps_the_more_precise_date_and_both_sentences():
    a = P("Blaž", b="1796", text="A")
    b = P("Blaž", b="28 JAN 1796", text="B")
    t = _tree([a, b])
    nar.dedupe_people(t)
    kept = t.indi[0]
    assert kept.birth["date"] == "28 JAN 1796"
    assert [s["text"] for s in kept.extra_sources] == ["B"]


def test_merging_rewrites_family_pointers():
    husb, wife = P("Mihael", sex="m"), P("Apolonija", "Krajnc", sex="f")
    dup_wife = P("Apolonija", "Krajnc", sex="f", b="1729", stub=True)
    wife.birth = {"date": "1729", "place": ""}
    t = _tree([husb, wife, dup_wife], [(husb, wife, []), (husb, dup_wife, [])])
    nar.dedupe_people(t)
    assert dup_wife not in t.indi
    assert all(f["wife"] is wife for f in t.fam)
    assert nar.dedupe_families(t) == 1
    assert len(t.fam) == 1


def test_duplicate_couples_merge_and_union_children():
    husb, wife = P("Janez", sex="m"), P("Cecilija", "Korpič", sex="f")
    c1, c2 = P("Milena"), P("Slavko")
    t = _tree([husb, wife, c1, c2],
              [(husb, wife, [c1]), (husb, wife, [c1, c2])])
    assert nar.dedupe_families(t) == 1
    assert len(t.fam) == 1
    assert t.fam[0]["chil"] == [c1, c2]


# --------------------------------------------------------------- reconcile
def test_reconcile_leaves_a_child_in_exactly_one_family():
    a, b, kid = P("Jurij", sex="m"), P("Jožef", sex="m"), P("Marija")
    t = _tree([a, b, kid], [(a, None, [kid]), (b, None, [kid])])
    kid.famc = t.fam[0]
    assert nar.reconcile(t) == 1
    listed = [f for f in t.fam if kid in f["chil"]]
    assert len(listed) == 1 and kid.famc is listed[0]
    assert any("drugim starše" in n for n in kid.notes)


def test_prune_drops_families_that_record_nothing():
    lone, kid = P("Jurij", sex="m"), P("Marija")
    t = _tree([lone, kid], [(lone, None, [])])
    assert nar.prune_families(t) == 1
    assert t.fam == [] and lone.fams == []


def test_reindex_is_contiguous():
    t = _tree([P("A"), P("B")], [])
    t.indi.pop(0)
    nar.reindex(t)
    assert [p.xref for p in t.indi] == ["@I1@"]


# ------------------------------------------------- generation plausibility
def test_generation_window_accepts_a_normal_gap():
    assert nar.generation_ok(P("Jurij", b="1800"), P("Marija", b="1830"))


def test_generation_window_rejects_an_impossible_parent():
    assert not nar.generation_ok(P("Filip", b="ABT 1695"), P("Ana", b="26 JUL 1834"))
    assert not nar.generation_ok(P("Blaž", b="1826"), P("Alojzija", b="8 APR 1908"))


def test_generation_window_rejects_a_parent_too_young():
    assert not nar.generation_ok(P("Jurij", b="1800"), P("Marija", b="1810"))


def test_mothers_get_a_tighter_window_than_fathers():
    child = P("Ana", b="1900")
    assert nar.generation_ok(P("Jurij", b="1840", sex="m"), child)
    assert not nar.generation_ok(P("Neža", b="1840", sex="f"), child)


def test_an_unknown_year_is_never_an_objection():
    assert nar.generation_ok(P("Jurij"), P("Marija", b="1830"))
    assert nar.generation_ok(P("Jurij", b="1800"), P("Marija"))


# ------------------------------------------------------ parent resolution
def _findable(tree_people):
    t = nar.Tree()
    for p in tree_people:
        t.add_person(p)
        p.fams = []
    return t


def test_resolves_a_father_named_in_the_entry():
    dad = P("Jožef", "Korpič", b="11 FEB 1846", sex="m", page=1065)
    kid = P("Terezija", "Korpič", b="19 AUG 1884", father="Jožef", page=1065)
    assert nar.parent_candidates(_findable([dad, kid]), kid) == [dad]


def test_does_not_resolve_across_a_different_surname():
    dad = P("Janez", "Belšak", b="1900", sex="m", page=1090)
    kid = P("Franc", "Horvat", b="1932", father="Janez", page=1090)
    assert nar.parent_candidates(_findable([dad, kid]), kid) == []


def test_does_not_resolve_an_implausible_namesake():
    old = P("Jožef", "Korpič", b="1700", sex="m", page=1065)
    kid = P("Terezija", "Korpič", b="1884", father="Jožef", page=1065)
    assert nar.parent_candidates(_findable([old, kid]), kid) == []


def test_two_candidates_stay_unresolved():
    a = P("Jožef", "Korpič", b="1846", sex="m", page=1065)
    b = P("Jožef", "Korpič", b="1850", sex="m", page=1065)
    kid = P("Terezija", "Korpič", b="1884", father="Jožef", page=1065)
    assert len(nar.parent_candidates(_findable([a, b, kid]), kid)) == 2


def test_a_distant_namesake_needs_the_wife_to_confirm_him():
    far = P("Jožef", "Korpič", b="1846", sex="m", page=1120)
    kid = P("Terezija", "Korpič", b="1884", father="Jožef", mother="Neža", page=1065)
    assert nar.parent_candidates(_findable([far, kid]), kid) == []
    wife = P("Neža", "Merkuš", sex="f")
    t = _findable([far, kid, wife])
    f = t.add_family(far, wife)
    far.fams, wife.fams = [(f, wife)], [(f, far)]
    assert nar.parent_candidates(t, kid) == [far]


def test_a_child_naming_NN_is_never_resolved():
    dad = P("Jožef", "Korpič", b="1846", sex="m", page=1065)
    kid = P("Terezija", "Korpič", b="1884", father="NN", page=1065)
    assert nar.parent_candidates(_findable([dad, kid]), kid) == []


# --------------------------------------------------------------- pedigree
def test_adoption_wording_is_recognised():
    p = P("Ana", text="Posvojenka: Ana Vidovič, rojena 1814, starša Gašper in Katarina")
    assert nar.pedigree_of(p) == "adopted"


def test_fostering_wording_is_recognised():
    assert nar.pedigree_of(P("Jožef", text="Jožef Hentak, potem v reji pri Cafuti")) == "foster"


def test_an_ordinary_entry_has_no_pedigree():
    assert nar.pedigree_of(P("Jurij", text="Jurij Weisbacher, rojen 1725")) is None


# --------------------------------------------- reconcile picks by agreement
def test_reconcile_keeps_the_family_the_entry_corroborates():
    right, rwife = P("Janez", "Horvat", b="1900", sex="m"), P("Marija", "Zavec", b="1905", sex="f")
    wrong, wwife = P("Anton", "Belšak", b="1900", sex="m"), P("Ana", "Kolar", b="1905", sex="f")
    kid = P("Franc", "Horvat", b="1932", father="Janez", mother="Marija")
    t = _findable([right, rwife, wrong, wwife, kid])
    f_wrong = t.add_family(wrong, wwife); f_wrong["chil"] = [kid]
    f_right = t.add_family(right, rwife); f_right["chil"] = [kid]
    kid.famc = f_wrong                       # the first link made, and wrong
    nar.reconcile(t)
    assert kid.famc is f_right
    assert kid not in f_wrong["chil"]


def test_flag_implausible_notes_but_keeps_the_link():
    dad = P("Blaž", "Letič", b="2 MAR 1826", sex="m")
    kid = P("Alojzija", "Hentak", b="8 APR 1908")
    t = _findable([dad, kid])
    f = t.add_family(dad, None); f["chil"] = [kid]; kid.famc = f
    assert nar.flag_implausible(t) == 1
    assert kid in f["chil"]
    assert any("Letnici se ne ujemata" in n for n in kid.notes)


# --------------------------------------------- labelled continuation records
def test_a_list_label_does_not_hide_a_person():
    assert nar.LIST_LABEL.sub("", "a) Jožef Korpič rojen 11.2.1846") == \
        "Jožef Korpič rojen 11.2.1846"
    assert nar.LIST_LABEL.sub("", "2. Ana Vidovič, rojena 1814") == "Ana Vidovič, rojena 1814"
    assert nar.LIST_LABEL.sub("", "Jožef Korpič rojen 1846") == "Jožef Korpič rojen 1846"


# ------------------------------------------------------- counted children
def test_counted_children_reads_number_and_sex():
    assert nar.counted_children("Dve hčerki umrli v otroštvu") == (2, "f")
    assert nar.counted_children("Še trije otroci") == (3, "")
    assert nar.counted_children("Dvojčiči, rojeni 2000, starša Mehmed in Marta") == (2, "")


def test_a_list_heading_is_not_expanded():
    """'Šest otrok ...:' introduces children who get their own entries."""
    assert nar.counted_children(
        "Šest otrok Apolonije Pignar iz prvega zakona z Ivanom Kuharjem:") == (0, "")


def test_an_ordinary_entry_counts_nothing():
    assert nar.counted_children("Marija Voglar, rojena 30.12.1771") == (0, "")


def test_expand_counted_creates_that_many_placeholders():
    dad = P("Martin", "Zavec", b="1910", sex="m")
    t = _tree([dad])
    f = t.add_family(dad, None)
    dad.fams = [(f, None)]
    rec = nar.Rec(1057, 7, "Dve hčerki umrli v otroštvu", True, 0.0)
    assert nar.expand_counted(t, rec.text, 2, "f", dad, rec, {}) == 2
    kids = [p for p in t.indi if p.given == nar.UNNAMED]
    assert len(kids) == 2
    assert all(k.sex == "f" and k.surname == "Zavec" for k in kids)
    assert all(k.famc is f for k in kids)
    assert all(any("Neimenovan otrok" in n for n in k.notes) for k in kids)


def test_placeholders_are_never_merged_with_each_other():
    a = P(nar.UNNAMED, "Zavec", b="1900")
    b = P(nar.UNNAMED, "Zavec", b="1900")
    assert nar.match_score(a, b) is None
    t = _tree([a, b])
    assert nar.dedupe_people(t) == 0


# ---------------------------------------------------- estimates & presumption
def test_birth_estimated_from_age_at_death():
    p = P("Julijana", b="", d="24 MAY 1888")
    p.age_death = 1
    t = _tree([p])
    assert nar.estimate_births(t) == 1
    assert p.birth["date"] == "ABT 1887"
    assert p.birth["note"] == "ocenjeno iz starosti ob smrti"


def test_birth_estimated_from_year_of_marriage():
    p = P("Janez")
    t = _tree([p])
    f = t.add_family(p, None)
    f["date"] = "27 DEC 1976"
    p.fams = [(f, None)]
    nar.estimate_births(t)
    assert p.birth["date"] == f"ABT {1976 - nar.TYPICAL_MARRIAGE_AGE}"
    assert p.birth["note"] == "ocenjeno iz leta poroke"


def test_birth_estimated_from_the_children():
    parent, kid = P("Katarina"), P("Ana", b="1863")
    t = _tree([parent, kid])
    f = t.add_family(parent, None)
    f["chil"] = [kid]
    parent.fams = [(f, None)]
    nar.estimate_births(t)
    assert parent.birth["date"] == f"ABT {1863 - nar.TYPICAL_GENERATION}"
    assert parent.birth["note"] == "ocenjeno iz letnic otrok"


def test_a_recorded_birth_is_never_overwritten():
    p = P("Blaž", b="28 JAN 1796", d="16 JUL 1863")
    p.age_death = 67
    t = _tree([p])
    assert nar.estimate_births(t) == 0
    assert p.birth["date"] == "28 JAN 1796"


def test_presumed_dead_after_a_hundred_years():
    p = P("Matija", b="19 FEB 1770")
    t = _tree([p])
    assert nar.presume_deaths(t, 2026) == 1
    assert p.death["known"] and not p.death["date"]
    assert p.death["note"] == f"domnevno, preko {nar.PRESUMED_DEAD_AGE} let"


def test_the_recently_born_are_left_alone():
    p = P("Danica", b="29 JAN 1964")
    t = _tree([p])
    assert nar.presume_deaths(t, 2026) == 0
    assert not p.death.get("known")


def test_a_known_death_is_not_overwritten():
    p = P("Blaž", b="1796", d="16 JUL 1863")
    t = _tree([p])
    assert nar.presume_deaths(t, 2026) == 0
    assert p.death["date"] == "16 JUL 1863"


def test_an_undated_death_still_counts_as_known():
    p = P("Ana", b="1900")
    p.death = {"date": "", "place": "", "known": True}
    t = _tree([p])
    assert nar.presume_deaths(t, 2026) == 0
    assert not p.death.get("note")


# ------------------------------------------------- "po domače" house names
def test_place_key_ignores_the_declined_settlement():
    assert nar.place_key("Dravci 9") == nar.place_key("Dravcih 9") == ("dravc", "9")
    assert nar.place_key("Dravce 2a") == ("dravc", "2a")
    assert nar.place_key("Sovičah 8") == nar.place_key("Soviče 8") == ("sovic", "8")
    assert nar.place_key("Ptuj") is None


def test_addresses_expand_from_a_list():
    assert nar._expand_addrs("Dravci 1, Dravci 2a in 2b") == \
        ["Dravci 1", "Dravci 2a", "Dravci 2b"]
    assert nar._expand_addrs("Dravci 9 in 10") == ["Dravci 9", "Dravci 10"]


def test_a_dash_is_a_separator_not_a_range():
    """'Soviče 8 - 10' covers 8 and 10; 9 has a chapter of its own."""
    assert nar._expand_addrs("Soviče 8 – 10") == ["Soviče 8", "Soviče 10"]


def _rec(text, chapter="", vulgo=""):
    return nar.Rec(1042, 0, text, False, 0.0, chapter, vulgo)


def test_house_name_from_a_section_heading_with_addresses():
    h = nar.collect_houses([_rec("2 Pfeiferjevi (po domače Labiševi) – Dravci 1, Dravci 2a in 2b")])
    assert h[("dravc", "1")] == "Labiševi"
    assert h[("dravc", "2b")] == "Labiševi"


def test_house_name_falls_back_to_the_chapter_address():
    h = nar.collect_houses([_rec("1 Vindiš (po domače Kolarjevi)", chapter="Dravci 2")])
    assert h == {("dravc", "2"): "Kolarjevi"}


def test_house_name_from_a_chapter_title():
    h = nar.collect_houses([_rec("Ana Belšak, rojena 1923",
                                 chapter="Dravci 9 in 10", vulgo="Tumpovi")])
    assert h[("dravc", "9")] == h[("dravc", "10")] == "Tumpovi"


def test_alternative_house_names_are_both_kept():
    h = nar.collect_houses([_rec("1 Merčevi (po domače Joštovi ali Tomažekovi)",
                                 chapter="Soviče 12")])
    assert h[("sovic", "12")] == "Joštovi / Tomažekovi"


def test_a_named_house_beats_the_grouping_around_it():
    recs = [_rec("x", chapter="Soviče 8 – 10", vulgo="Kukmačevi"),
            _rec("y", chapter="Soviče 9", vulgo="Pozničar")]
    h = nar.collect_houses(recs)
    assert h[("sovic", "9")] == "Pozničar"
    assert h[("sovic", "8")] == "Kukmačevi"


def test_places_are_annotated_with_the_house_name():
    p = P("Ana", "Belšak", b="1923")
    p.birth = {"date": "1923", "place": "Dravce 9"}
    p.resi = [("Dravcih 10", True)]
    t = _tree([p])
    assert nar.annotate_places(t, {("dravc", "9"): "Tumpovi",
                                   ("dravc", "10"): "Tumpovi"}) == 2
    assert p.birth["place"] == "Dravce 9 (pd Tumpovi)"
    assert p.resi == [("Dravcih 10 (pd Tumpovi)", True)]


def test_an_unknown_house_is_left_alone():
    p = P("Ana", b="1923")
    p.birth = {"date": "1923", "place": "Ptuj"}
    t = _tree([p])
    assert nar.annotate_places(t, {("dravc", "9"): "Tumpovi"}) == 0
    assert p.birth["place"] == "Ptuj"


def test_annotating_twice_does_not_repeat_the_name():
    p = P("Ana", b="1923")
    p.birth = {"date": "1923", "place": "Dravci 9"}
    t = _tree([p])
    houses = {("dravc", "9"): "Tumpovi"}
    nar.annotate_places(t, houses)
    assert nar.annotate_places(t, houses) == 0
    assert p.birth["place"] == "Dravci 9 (pd Tumpovi)"


# --------------------------------------------- nicknames vs. surnames
def test_a_house_nickname_leaves_the_surname_clean():
    h = nar._split_head("Jožef Vindiš (Kolarjev Pepek), rojen 15.2.1902")
    assert (h.given, h.surname, h.nick) == ("Jožef", "Vindiš", "Kolarjev Pepek")


def test_a_one_word_parenthetical_is_a_spelling_of_the_surname():
    h = nar._split_head("Marija Weisbacher (Weispoher), rojena 12.7.1767")
    assert (h.surname, h.alt, h.nick) == ("Weisbacher", "Weispoher", "")


def test_a_long_parenthetical_becomes_a_remark():
    h = nar._split_head("Marija Hlupič (sprva v matični knjigi priimek Lubec), rojena 1880")
    assert h.surname == "Hlupič" and h.nick == "" and "matični" in h.remark


def test_an_alternative_given_name_is_kept_as_aka():
    h = nar._split_head("Janez (Ivan) Fajfar, rojen 11.12.1886")
    assert (h.given, h.surname, h.aka) == ("Janez", "Fajfar", ["Ivan"])


def test_several_alternative_given_names_are_all_kept():
    h = nar._split_head("Janez (Johan, Ivan) Letič, rojen 1880")
    assert (h.given, h.surname, h.aka) == ("Janez", "Letič", ["Johan", "Ivan"])


# ------------------------------------------------- settlement name normalising
def test_stem_matches_the_inflected_forms():
    for a, b in [("Dravci", "Dravcih"), ("Dravci", "Dravce"), ("Soviče", "Sovičah"),
                 ("Vareja", "Varejah"), ("Pobrežje", "Pobrežju"), ("Ptuj", "Ptuju"),
                 ("Maribor", "Mariboru"), ("Leskovec", "Leskovcu"),
                 ("Bukovec", "Bukovci"), ("Šturmovec", "Šturmovci")]:
        assert nar.town_stem(a) == nar.town_stem(b), (a, b)


def test_stem_keeps_different_settlements_apart():
    assert nar.town_stem("Grajena") != nar.town_stem("Grajenščak")
    assert nar.town_key("Velika Varnica") != nar.town_key("Veliki Okič")
    assert nar.town_key("Spodnja Hajdina") != nar.town_key("Spodnja Pristava")


def test_multiword_names_agree_across_cases():
    assert nar.town_key("Spodnja Hajdina") == nar.town_key("Spodnji Hajdini")


def test_town_key_ignores_the_house_number():
    assert nar.town_key("Dravci 17") == nar.town_key("Dravcih") == ("dravc",)


def _placed(*pairs):
    """pairs of (place, declined)"""
    people = []
    for i, (place, declined) in enumerate(pairs):
        p = P(f"Oseba{i}")
        p.birth = {"date": "", "place": place, "declined": declined}
        people.append(p)
    return _tree(people)


def test_the_undeclined_form_wins_over_a_commoner_declined_one():
    t = _placed(("Ljubljani", True), ("Ljubljani", True), ("Ljubljana", False))
    assert nar.canonical_towns(t)[("ljubljan",)] == "Ljubljana"


def test_a_locative_ending_loses_even_when_commonest():
    t = _placed(("Mariboru", False), ("Mariboru", False), ("Maribor", False))
    assert nar.canonical_towns(t)[("maribor",)] == "Maribor"


def test_a_feminine_locative_in_i_is_recognised_by_its_pair():
    t = _placed(("Ljubljani", False), ("Ljubljani", False), ("Ljubljana", False))
    assert nar.canonical_towns(t)[("ljubljan",)] == "Ljubljana"


def test_a_nominative_plural_in_i_is_not_mistaken_for_a_locative():
    t = _placed(("Dravci", False), ("Dravcih", True))
    assert nar.canonical_towns(t)[("dravc",)] == "Dravci"


def test_canonicalise_rewrites_and_keeps_the_house_number():
    p = P("Ana")
    p.birth = {"date": "", "place": "Dravcih 10", "declined": True}
    p.death = {"date": "", "place": "Dravci", "declined": False}
    p.resi = [("Dravce 2", False)]
    t = _tree([p])
    assert nar.canonicalise_places(t) == 2
    assert p.birth["place"] == "Dravci 10"
    assert p.resi == [("Dravci 2", False)]


def test_an_only_ever_declined_place_is_still_made_consistent():
    t = _placed(("Cirkulanah", True), ("Cirkulane", True))
    assert nar.canonical_towns(t)[("cirkulan",)] == "Cirkulane"


# ------------------------------------------------------------ misprints
def test_a_misprint_folds_into_the_dominant_spelling():
    counts = {("dravc",): 696, ("dravcen",): 1, ("dravic",): 2}
    assert nar.fold_typos(counts) == {("dravcen",): ("dravc",),
                                      ("dravic",): ("dravc",)}


def test_a_different_village_is_not_a_misprint():
    """Borovci and Bukovci differ by two letters but are two places."""
    assert nar.fold_typos({("bukovc",): 23, ("borovc",): 1}) == {}
    assert nar.fold_typos({("sovic",): 202, ("svici",): 1}) == {}


def test_a_place_used_often_is_never_folded():
    assert nar.fold_typos({("dravc",): 696, ("dravcen",): 40}) == {}


def test_folding_needs_one_unambiguous_target():
    counts = {("dravc",): 100, ("dravk",): 100, ("dravcn",): 1}
    assert ("dravcn",) not in nar.fold_typos(counts)


def test_a_misprinted_place_is_rewritten_with_its_house_number():
    people = []
    for place in ["Dravci 11"] * 30 + ["Dravcen 11", "Dravic 18"]:
        p = P(f"X{len(people)}")
        p.birth = {"date": "", "place": place, "declined": False}
        people.append(p)
    t = _tree(people)
    nar.canonicalise_places(t)
    assert {p.birth["place"] for p in t.indi} == {"Dravci 11", "Dravci 18"}


# ------------------------------------------- house names without "po domače"
def test_a_possessive_section_heading_names_the_house():
    assert nar.section_house("2 Žepekovi: Habjanič – Milošič")[0] == "Žepekovi"
    assert nar.section_house("1 Gnilškovi")[0] == "Gnilškovi"
    assert nar.section_house("3 Žlahtičevi")[0] == "Žlahtičevi"
    assert nar.section_house("1 Petrovič (Kosovi)")[0] == "Kosovi"


def test_two_house_names_are_both_kept():
    assert nar.section_house("2 Habjaničevi - Lekečinčevi")[0] == \
        "Habjaničevi / Lekečinčevi"


def test_a_plain_surname_heading_is_not_a_house_name():
    for t in ["1 Voglar – Vidovič – Rakuš", "2 Horvat", "1 Krajnc",
              "3 Drugi prebivalci te hiše", "1 Od Jagodičev do Korpičev"]:
        assert nar.section_house(t)[0] == "", t


def test_an_explicit_po_domace_heading_is_left_to_the_other_reader():
    assert nar.section_house("1 Merčevi (po domače Joštovi ali Tomažekovi)")[0] == ""


def test_a_section_that_names_its_own_address_keeps_it():
    name, addr = nar.section_house("1 Tejekovi v Sovičah 6 (danes Soviče 12)")
    assert name == "Tejekovi"
    assert nar.place_key("Soviče 6") in {nar.place_key(a)
                                         for a in nar._expand_addrs(addr)}


def test_the_section_house_name_reaches_that_person_only():
    p = P("Ana", "Cafuta", b="1880")
    p.birth = {"date": "1880", "place": "Dravci 8", "declined": False}
    p.house, p.house_addr = "Cafutovi", "Dravci 8"
    other = P("Jurij", "Žlahtič", b="1880")
    other.birth = {"date": "1880", "place": "Dravci 8", "declined": False}
    other.house, other.house_addr = "Žlahtičevi", "Dravci 8"
    t = _tree([p, other])
    nar.annotate_places(t, {})
    assert p.birth["place"] == "Dravci 8 (pd Cafutovi)"
    assert other.birth["place"] == "Dravci 8 (pd Žlahtičevi)"


def test_a_declared_house_name_beats_the_section_one():
    p = P("Ana", b="1880")
    p.birth = {"date": "1880", "place": "Dravci 9", "declined": False}
    p.house, p.house_addr = "Nekaj", "Dravci 9"
    t = _tree([p])
    nar.annotate_places(t, {("dravc", "9"): "Tumpovi"})
    assert p.birth["place"] == "Dravci 9 (pd Tumpovi)"


# --------------------------------------------------- co-resident households
def test_a_coresident_heading_is_recognised():
    for t in ["3 Drugi prebivalci te hiše:", "2 Drugi prebivalci te hiše",
              "3 Drugi:", "4 Viničarji na Kovačevem, Dravci 11:"]:
        assert nar.coresident_heading(t)[0], t


def test_a_coresident_heading_keeps_its_own_address():
    assert nar.place_key("Dravci 11") in {
        nar.place_key(a) for a in
        nar._expand_addrs(nar.coresident_heading("4 Viničarji na Kovačevem, Dravci 11:")[1])}


def test_an_ordinary_section_heading_is_not_a_coresident_one():
    for t in ["1 Weisbacher", "2 Cafutovi", "1 Vindiš (po domače Kolarjevi)"]:
        assert not nar.coresident_heading(t)[0], t


# ------------------------------------------ expected structure vs. real misses
def test_a_numbered_section_heading_is_expected_structure():
    for t in ["1 Voglar – Vidovič – Rakuš", "1 Weisbacher", "2 Zavec – Kozel",
              "1 Od Fegušev do Vidovičev", "3 Drugi prebivalci te hiše:"]:
        assert nar.skip_kind(t) == "structure", t


def test_a_family_group_heading_is_expected_structure():
    for t in ["Brezniki", "Lozinški", "Letonjevi", "Vidoviči"]:
        assert nar.skip_kind(t) == "structure", t


def test_a_land_record_is_expected_structure():
    for t in ["21.9.1855 kupi Martin Weisbacher",
              "8.2.1872 kupita Jožef in Neža Korpič",
              "Leta 1828 nov lastnik:",
              "1791 Bedrač izgubi posest na dražbi, kupec Anton Mlakar"]:
        assert nar.skip_kind(t) == "structure", t


def test_an_unreadable_person_entry_is_still_reported():
    for t in ["Jožef Fošnarič, odšel v ZDA",
              "Franc Hentak, viničar, Dravci 11",
              "mrtvorojena deklica Habjanič, rojena 22.12.1915, Dravci 11"]:
        assert nar.skip_kind(t) == "unread", t


# --------------------------------------------- people recorded without dates
def test_a_bare_name_entry_is_admitted_as_a_person():
    for t in ["Jožef Fošnarič, odšel v ZDA", "Franc Hentak, viničar, Dravci 11",
              "Aleksander Cafuta", "Mitja Letonja, zdravnik ptujske bolnišnice",
              "Janez Vindiš – vnuk Jožefa Cafute"]:
        assert nar._not_a_person(nar.Rec(1, 0, t, False, 0.0)) == "", t


def test_a_narrative_sentence_is_still_not_a_person():
    for t in ["Elizabeta Habjanič marca 1965 posest proda Antonu in Jožefi Milošič",
              "Tejekovi so sprva živeli v Tajni na polju",
              "Kam sodi Jakob Krajnc, nisem uspela ugotoviti",
              "Teden dni po vpisu v ZK dediči posestvo",
              "Franci Hvalec Boštjan Hvalec −"]:
        assert nar._not_a_person(nar.Rec(1, 0, t, False, 0.0)) != "", t


def test_an_undated_entry_stops_the_name_at_the_comma():
    h = nar._split_head("Jožef Fošnarič, odšel v ZDA")
    assert (h.given, h.surname) == ("Jožef", "Fošnarič")
    h = nar._split_head("Janez Vindiš – vnuk Jožefa Cafute")
    assert (h.given, h.surname) == ("Janez", "Vindiš")


# ------------------------------------------------------------ land records
def test_land_records_are_recognised():
    for t in ["21.9.1855 kupi Martin Weisbacher",
              "8.2.1872 kupita Jožef in Neža Korpič",
              "Leta 1828 nov lastnik:",
              "1791 Bedrač izgubi posest na dražbi, kupec Anton Mlakar"]:
        assert nar.is_land_record(t), t


def test_an_ordinary_entry_is_not_a_land_record():
    for t in ["Jožef Fošnarič, odšel v ZDA", "1 Weisbacher",
              "Marija Voglar, rojena 30.12.1771, Dravci"]:
        assert not nar.is_land_record(t), t


# ------------------------------------------------- tenants, trades, land notes
def test_a_prose_tenant_heading_is_a_coresident_heading():
    for t in ["Konec 19. stoletja najemniki pri Weisbacherjevih HLUPIČEVI:",
              "Več otrok na služenju pri Letičevih:",
              "Pri Janezu Letiču sta služila"]:
        assert nar.coresident_heading(t)[0], t


def test_a_person_who_happens_to_be_a_tenant_is_not_a_heading():
    for t in ["Franc Hentak, viničar, Dravci 11", "Breznik Jožef, viničar",
              "Andrej Feguš, skrbnik viničar in Terezija"]:
        assert not nar.coresident_heading(t)[0], t


def _occu(text):
    return sorted({x.group(1) for x in nar.OCCU.finditer(text)})


def test_occupations_are_recognised():
    assert _occu("Mitja Letonja, zdravnik ptujske bolnišnice") == ["zdravnik"]
    assert _occu("Aleksander Cafuta, sodar") == ["sodar"]
    assert _occu("Janez Weisbacher, šolski sluga na Ptuju") == ["šolski sluga"]


def test_a_surname_is_not_read_as_a_trade():
    """kovač is a smith, Kovač is a family."""
    assert _occu("Marija Kovač, rojena 1880") == []
    assert _occu("Jožef Mlinar, rojen 1850") == []
    assert _occu("Ana Krčmar, rojena 1900") == []


def test_the_midwife_is_not_the_persons_trade():
    assert _occu("Avgust Weisbacher, babica pri porodu Elizabeta Weisbacher") == []


def test_the_books_own_title_is_skipped_silently():
    assert nar.skip_kind("DRAVČANI ZGODOVINA DRAVCEV IN NJENIH PREBIVALCEV") == "structure"


def test_a_land_record_reaches_everyone_it_names():
    a = P("Jožef", "Korpič", b="1846", sex="m")
    b = P("Neža", "Merkuš", b="1848", sex="f")
    other = P("Martin", "Voglar", b="1700", sex="m")
    t = _tree([a, b, other])
    t.land_records = [("8.2.1872 kupita Jožef Korpič in Neža Merkuš", 1104, other)]
    assert nar.attach_land_records(t) == 2
    assert any("Zemljiški zapis" in n for n in a.notes)
    assert any("Zemljiški zapis" in n for n in b.notes)
    assert not any("Zemljiški zapis" in n for n in other.notes)


def test_a_land_record_naming_nobody_stays_with_the_head():
    head = P("Matevž", "Voglar", b="1732", sex="m")
    t = _tree([head])
    t.land_records = [("Leta 1828 nov lastnik:", 1118, head)]
    assert nar.attach_land_records(t) == 1
    assert any("Zemljiški zapis" in n for n in head.notes)


def test_a_land_record_skips_someone_who_could_not_have_been_alive():
    old = P("Gregor", "Weisbacher", b="1665", sex="m")
    t = _tree([old])
    t.land_records = [("11.4.1961 posest prodata Gregor Weisbacher", 1060, None)]
    nar.attach_land_records(t)
    assert not any("Zemljiški zapis" in n for n in old.notes)


# ---------------------------------------------------- people reported in prose
def _prose(text):
    return nar.parse_record(nar.normalise_prose(text), 1, 0, {}, full_text=text)


def test_a_death_reported_in_prose_becomes_a_person():
    p = _prose("V Dravcih 2 je umrl Jurij Zrnec (Sernez), 16.10.1782, star 60 let")
    assert (p.given, p.surname, p.alt_surname) == ("Jurij", "Zrnec", "Sernez")
    assert p.death["date"] == "16 OCT 1782"


def test_a_date_in_front_of_the_verb_is_carried_across():
    p = _prose("14.7.1892 umrla Marija Mihelač, Dravci 18, stara 2 meseca")
    assert (p.given, p.surname) == ("Marija", "Mihelač")
    assert p.death["date"] == "14 JUL 1892"


def test_a_pointing_verb_is_not_glued_to_the_surname():
    """'je bil' only points at the subject -- the facts already follow it."""
    p = _prose("hlapec je bil Janez Fleis, rojen 18.10.1866")
    assert (p.given, p.surname) == ("Janez", "Fleis")
    assert p.birth["date"] == "18 OCT 1866"


def test_a_trade_in_the_original_wording_survives_the_rewrite():
    p = _prose("pri Horvatovih je kot dekla živela Marija (Micika) Topolovec, "
               "rojena 15.9.1907, Trdobojci 15")
    assert p.occu == ["dekla"] and p.birth["date"] == "15 SEP 1907"
    p = _prose("Kasneje pride še Blaž Lozinšek, kočar, Dravci 11")
    assert (p.given, p.surname) == ("Blaž", "Lozinšek") and p.occu == ["kočar"]


def test_a_lowercase_opening_no_longer_hides_a_person():
    for t in ["hlapec je bil Janez Fleis, rojen 18.10.1866",
              "14.7.1892 umrla Marija Mihelač, Dravci 18"]:
        assert nar._not_a_person(nar.Rec(1, 0, t, False, 0.0)) == "", t


def test_a_marriage_reported_in_prose_names_both_spouses():
    m = nar.PROSE_MARRIAGE.search(
        "Na Dravci 2 se 25.1.1796 poročita 54-letni Matevž Bezjak in 40-letna Marija Zavec.")
    assert m.groups() == ("Matevž Bezjak", "Marija Zavec")


def test_a_stillborn_child_is_NN_with_the_stated_sex():
    surname, sex = nar.stillborn_child(
        "mrtvorojena deklica Habjanič, rojena 22.12.1915, Dravci 11, oče Jožef")
    assert (surname, sex) == ("Habjanič", "f")
    assert nar.stillborn_child("Marija Habjanič, rojena 1915") == (None, "")


def test_a_prose_land_record_is_still_a_land_record():
    for t in ["Zaradi prezadolženosti Kozelovih 26.6.1883 postane lastnik "
              "njihovega posestva ptujski trgovec Oto Bratanič.",
              "Rozalija Vaupotič je hišo in posest leta 2017 prodala Nelfiju Babiču",
              "Elizabeta Habjanič marca 1965 posest proda Antonu in Jožefi Milošič"]:
        assert nar.is_land_record(t), t


def test_a_person_who_inherits_is_not_a_land_record():
    assert not nar.is_land_record(
        "Elizabeta (Liza) Habjanič, rojena 26.10.1910, Terezovec, oče Franc, "
        "prevzemnica domačije, posest")


def test_a_children_list_heading_is_expected_structure():
    assert nar.skip_kind(
        "Šest otrok Apolonije Pignar iz prvega zakona z Ivanom Kuharjem, "
        "rojenih med 1875 in 1885:") == "structure"


# --------------------------------------------------------------- name repair
def test_sex_is_inferred_from_the_ending():
    for name in ["Rosalia", "Albertine", "Wilhelmine", "Marija", "Vesna"]:
        assert nar.FEM_ENDING.search(nar.norm(name)), name
    for name in ["Jurij", "Anton", "Franc", "Stanko", "Janko", "Marko"]:
        assert nar.MASC_ENDING.search(nar.norm(name)), name


def test_sex_inference_leaves_placeholders_alone():
    p = P(nar.UNNAMED, "Zavec")
    t = _tree([p])
    nar.infer_sex(t)
    assert p.sex == ""


def test_a_place_phrase_is_not_part_of_the_name():
    h = nar._split_head("Amalija Finžger iz Lovrenca na Dravskem polju")
    assert (h.given, h.surname) == ("Amalija", "Finžger")
    assert h.place.startswith("Lovrenca")
    h = nar._split_head("Ana Smodiš v Ljubljani")
    assert (h.given, h.surname, h.place) == ("Ana", "Smodiš", "Ljubljani")


def test_a_name_never_runs_past_a_comma():
    h = nar._split_head("Jurij Weisbacher, neznani starši iz Dravc, "
                        "je bil 27.1.1789 poročen z Marijo Zrnec")
    assert (h.given, h.surname) == ("Jurij", "Weisbacher")


def test_the_spouse_keeps_her_origin_out_of_her_surname():
    assert nar._split_origin("Jožefom Bedračem iz Dravc 5") == \
        ("Jožefom Bedračem", "Dravc 5")


def test_the_participle_says_who_is_marrying():
    """In 'Janko ... poročen z Leopoldino ..., vnovič poročena z Ljubomirjem
    Lukićem' the second wedding is hers, and its groom is a man."""
    p = nar.parse_record(
        "Janko Vaupotič, rojen 20.1.1931, poročen 20.7.1957 z Leopoldino Predikaka, "
        "rojena 14.11.1934, vnovič poročena 3.12.1960 z Ljubomirjem Lukićem",
        1114, 2, {"lukić": "Lukić"})
    wife = p.marriages[0]["spouse"]
    assert wife.sex == "f" and len(p.marriages) == 1
    second = wife.marriages[0]["spouse"]
    assert (second.given, second.surname, second.sex) == ("Ljubomir", "Lukić", "m")


def test_one_row_per_address():
    p = P("Matevž", "Hojnik", b="1976")
    p.resi = [("Dravci 9", False), ("Dravci 9", True), ("Vareja 3", False),
              ("Dravci 9", False)]
    t = _tree([p])
    assert nar.dedupe_residences(t) == 2
    assert [x for x, _d in p.resi] == ["Dravci 9", "Vareja 3"]


def test_a_remarriage_two_spouses_deep_still_gets_its_family():
    """The groom of a remarriage may himself remarry; every wedding the
    sentence recorded must end up as a family."""
    tree = nar.Tree()
    a = P("Janko", "Vaupotič", sex="m")
    b = P("Leopoldina", "Predikaka", sex="f")
    c = P("Ljubomir", "Lukić", sex="m")
    d = P("Ana", "Kos", sex="f")
    c.marriages = [{"date": "", "place": "", "order": 0, "spouse": d}]
    b.marriages = [{"date": "", "place": "", "order": 0, "spouse": c}]
    a.marriages = [{"date": "", "place": "", "order": 0, "spouse": b}]
    tree.add_person(a)
    a.fams = []
    pending = [a]
    while pending:
        person = pending.pop()
        for _f, sp in nar._wed(tree, person, person.marriages):
            if sp is not None and sp.marriages:
                pending.append(sp)
    assert len(tree.fam) == 3
    assert all(p.xref for p in (a, b, c, d))


def test_a_marriage_is_never_built_twice():
    tree = nar.Tree()
    a, b = P("Janko", sex="m"), P("Ana", sex="f")
    a.marriages = [{"date": "", "place": "", "order": 0, "spouse": b}]
    tree.add_person(a)
    a.fams = []
    nar._wed(tree, a, a.marriages)
    nar._wed(tree, a, a.marriages)
    assert len(tree.fam) == 1


def test_a_married_name_carries_the_surname_only():
    import io
    t = nar.Tree()
    husb, wife = P("Jurij", "Rakuš", sex="m"), P("Gera", "Kozel", sex="f")
    wife.source = {"page": 1, "text": "Gera Kozel, rojena 1868"}
    for x in (husb, wife):
        t.add_person(x)
        x.fams = []
    f = t.add_family(husb, wife)
    wife.fams = [(f, husb)]
    nar.reindex(t)
    out = io.StringIO()
    nar.emit(t, out)
    ged = out.getvalue()
    assert "1 NAME /Rakuš/\n2 TYPE married" in ged
    assert "1 NAME Gera /Rakuš/" not in ged
    assert "1 NAME Gera /Kozel/" in ged


def test_the_book_link_is_a_multimedia_record_everyone_cites():
    import io
    t = nar.Tree()
    p = P("Matevž", "Voglar", b="1732")
    p.source = {"page": 1041, "text": "Matevž Voglar, rojen 31.8.1732"}
    t.add_person(p)
    p.fams = []
    nar.reindex(t)
    out = io.StringIO()
    nar.emit(t, out)
    ged = out.getvalue()
    assert f"0 {nar.SOURCE_OBJE} OBJE" in ged
    assert f"1 FILE {nar.SOURCE_URL}" in ged
    assert "2 FORM URL" in ged
    # cited from the source record and from the person
    assert ged.count(f"1 OBJE {nar.SOURCE_OBJE}") == 2


def test_a_person_without_a_source_line_cites_no_object():
    import io
    t = nar.Tree()
    p = P("NN", "Zavec")
    p.source = {}
    t.add_person(p)
    p.fams = []
    nar.reindex(t)
    out = io.StringIO()
    nar.emit(t, out)
    assert out.getvalue().count(f"1 OBJE {nar.SOURCE_OBJE}") == 1


# --------------------------------------------------------- married surnames
def test_a_wife_is_also_known_by_her_husbands_surname():
    husb, wife = P("Jurij", "Rakuš", sex="m"), P("Gera", "Kozel", sex="f")
    t = _tree([husb, wife])
    f = t.add_family(husb, wife)
    husb.fams, wife.fams = [(f, wife)], [(f, husb)]
    assert nar.married_surnames(wife) == ["Rakuš"]


def test_a_stated_married_surname_comes_first():
    wife = P("Blanka", "Kerec", sex="f")
    wife.married_surname = "Slana"
    husb = P("Stanislav", "Kerec", sex="m")
    t = _tree([husb, wife])
    f = t.add_family(husb, wife)
    wife.fams = [(f, husb)]
    assert nar.married_surnames(wife)[0] == "Slana"


def test_both_husbands_surnames_are_kept():
    wife = P("Terezija", "Furman", sex="f")
    a, b = P("Janez", "Vindiš", sex="m"), P("Jožef", "Mlakar", sex="m")
    t = _tree([wife, a, b])
    wife.fams = [(t.add_family(a, wife), a), (t.add_family(b, wife), b)]
    assert nar.married_surnames(wife) == ["Vindiš", "Mlakar"]


def test_a_wife_keeping_the_same_surname_gains_nothing():
    husb, wife = P("Anton", "Cafuta", sex="m"), P("Marija", "Cafuta", sex="f")
    t = _tree([husb, wife])
    f = t.add_family(husb, wife)
    wife.fams = [(f, husb)]
    assert nar.married_surnames(wife) == []


def test_men_get_no_married_surname():
    husb, wife = P("Jurij", "Rakuš", sex="m"), P("Gera", "Kozel", sex="f")
    t = _tree([husb, wife])
    f = t.add_family(husb, wife)
    husb.fams = [(f, wife)]
    assert nar.married_surnames(husb) == []


def test_a_dropped_z_is_a_husband_not_a_married_name():
    """'poročena Alojzem Habjaničom' is an instrumental -- the book left out
    the preposition, so this is the groom, not the name she took."""
    p = nar.parse_record(
        "Ana Korpič, rojena 1920, poročena Alojzem Habjaničom, rojen 1915", 1067, 2, {})
    assert p.married_surname == ""


def test_a_spouse_name_stops_at_a_dash():
    """'poročena z Maksom Vaupotičem − Nataša Vaupotič, rojena 17.7.1967' is
    two entries whose bullet ended up mid-line."""
    p = nar.parse_record(
        "Marija Žnidarič, rojena 5.3.1944, poročena z Maksom Vaupotičem − "
        "Nataša Vaupotič, rojena 17.7.1967", 1057, 7, {"vaupotič": "Vaupotič"})
    assert p.marriages[0]["spouse"].label() == "Maks Vaupotič"


def test_a_dative_name_is_put_back_in_the_nominative():
    assert nar._undative("Jožefu Miložiču") == "Jožef Miložič"
    assert nar._undative("Matevžu Vidoviču") == "Matevž Vidovič"
    # a nominative pair is left alone
    assert nar._undative("Jožef Miložič") == "Jožef Miložič"
    assert nar._undative("Marija Vidovič") == "Marija Vidovič"


def test_a_shouted_title_never_continues_an_entry():
    recs = nar.extract.__doc__  # documented behaviour; exercised below
    assert nar.skip_kind("DRAVČANI") == "structure"


def test_a_legitimated_surname_is_not_part_of_the_maiden_name():
    h = nar._split_head("Marija Kranjc pozakonjena Korpič, rojena 2.1.1942")
    assert (h.given, h.surname, h.married) == ("Marija", "Kranjc", "Korpič")


class _Names:
    def __init__(self, known):
        from collections import Counter
        self.given = Counter(known)
        self.surname = Counter()


def test_an_oblique_given_name_is_put_back_in_the_dictionary_form():
    gz = _Names({"roza": 14, "neza": 9, "marija": 200})
    assert nar.renominalise("Rozo", gz) == "Roza"
    assert nar.renominalise("Nežo", gz) == "Neža"


def test_a_name_the_book_already_writes_that_way_is_left_alone():
    gz = _Names({"marija": 200, "jozef": 150})
    assert nar.renominalise("Marija", gz) == "Marija"
    assert nar.renominalise("Jožef", gz) == "Jožef"


def test_an_unknown_name_is_never_reshaped():
    gz = _Names({"marija": 200})
    assert nar.renominalise("Wilhelmine", gz) == "Wilhelmine"


def test_a_widows_remarriage_survives_a_semicolon():
    """'... s Terezijo Šmigoc, ... umrla 17.9.1960; drugič poročena 12.1.1919
    s Francem Cafuto' is her second marriage, not the dead husband's."""
    p = nar.parse_record(
        "Janez Merc, rojen 16.2.1880, umrl 1913, poročen 13.1.1913 s Terezijo "
        "Šmigoc, rojena 27.9.1879, umrla 17.9.1960; drugič poročena 12.1.1919 "
        "s Francem Cafuto, rojen 20.10.1868", 1139, 3, {})
    assert len(p.marriages) == 1
    widow = p.marriages[0]["spouse"]
    assert widow.label() == "Terezija Šmigoc"
    assert widow.marriages[0]["spouse"].label() == "Franc Cafuta"


def test_an_entry_that_starts_with_a_name_is_not_rewritten_as_prose():
    """'Katarina Breznik, ... umrla 1969 Slivniško Pohorje' must stay
    Katarina's entry, not become a person called Slivniško Pohorje."""
    t = ("Katarina Breznik, rojena 20.11.1896, Dravce 11, poročena 4.6.1923 "
         "z Anton Večerič, umrla 1969 Slivniško Pohorje")
    assert nar._split_head(t).prose is False
    p = nar.parse_record(t, 1120, 2, {})
    assert (p.given, p.surname) == ("Katarina", "Breznik")
    assert p.birth["date"] == "20 NOV 1896"


def test_a_feminine_death_returns_to_the_woman_the_entry_is_about():
    p = nar.parse_record(
        "Gera Cafuta, rojena 17.2.1847, poročena s sosedom Jožefom Belšakom, "
        "rojen 19.9.1843, Dravci 13, umrla 27.10.1886, stara 39 let", 1080, 1, {})
    assert p.death["date"] == "27 OCT 1886"
    assert not p.marriages[0]["spouse"].death.get("date")


def test_a_masculine_death_stays_with_the_husband():
    p = nar.parse_record(
        "Marija Belšak, rojena 1850, poročena z Jožefom Kozelom, rojen 1845, "
        "umrl 3.4.1900", 1080, 1, {})
    assert p.marriages[0]["spouse"].death["date"] == "3 APR 1900"
    assert not p.death.get("date")
