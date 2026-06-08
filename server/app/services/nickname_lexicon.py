"""A static, bidirectional diminutive↔formal nickname lexicon for given names.

Used by entity_index._merge_map to fold person/animal name variants that differ only
by a nickname/diminutive of the SAME given name (e.g. "Bob Smith" ↔ "Robert Smith").
It is deliberately small, hand-curated, and offline: no DB, no LLM, no network.

Structure & semantics:
- `_GROUPS` is a list of sets; every token in a set is treated as EQUIVALENT to every
  other token in the same set. Each set is collapsed to a single canonical token (the
  formal/longest, picked deterministically) via `canon_token`. Membership is
  bidirectional — both "bob"→"robert" and "robert"→"robert" canonicalize the same way.
- Matching is canonical-token based, so it is transitive within a group only: rob, bob,
  bobby, robert all share one canon, but groups never bleed into one another.

Limits / judgement calls (IMPORTANT):
- These are common ENGLISH given-name equivalences only. They are heuristics, not facts:
  some real people named "Jack" are not "John", etc. Callers MUST gate this with a strong
  signal (entity_index requires a SHARED real surname token before ever comparing two
  names) and with the generational-suffix guard below, and the result must stay overridable
  by an explicit user split decision.
- A few names legitimately belong to multiple lineages (e.g. "ed" → Edward, but also a
  diminutive of Edwin/Edmund). We collapse each token into exactly ONE group, choosing the
  most common mapping; rarer lineages are intentionally omitted to avoid false merges.
- Some short forms collide with full unrelated names (e.g. "drew" is both a diminutive of
  Andrew and a standalone given name). We include the common diminutive sense only.

Generational suffixes (jr/sr/iii/...) are NOT names; they are tracked separately so a
caller can refuse a nickname merge across a Jr/Sr boundary (a strong "different person"
signal between otherwise nickname-equivalent names).
"""
from __future__ import annotations

# Each entry: (canonical_formal_token, [equivalent diminutive/variant tokens]). Every token
# (canonical + variants) is treated as EQUIVALENT and maps to the canonical (the formal
# name, listed first). Tokens are lowercased, no punctuation.
_GROUPS: list[tuple[str, list[str]]] = [
    ("jeffrey", ["jeff", "jeffery", "geoff", "geoffrey"]),
    ("robert", ["bob", "bobby", "rob", "robby", "robbie"]),
    ("william", ["bill", "billy", "will", "willy", "willie"]),
    ("elizabeth", ["liz", "lizzie", "beth", "bethy", "betty", "eliza"]),
    ("margaret", ["peg", "peggy", "meg", "maggie", "marge", "margie"]),
    ("richard", ["dick", "rich", "richie", "rick", "ricky"]),
    ("katherine", ["kate", "katie", "kathy", "cathy", "catherine", "kathryn"]),
    ("james", ["jim", "jimmy", "jimmie"]),
    ("thomas", ["tom", "tommy"]),
    ("michael", ["mike", "mikey", "mick", "mickey"]),
    ("christopher", ["chris"]),
    ("christine", ["christy", "chrissy"]),
    ("susan", ["sue", "susie", "suzie", "suzanne"]),
    ("david", ["dave", "davey", "davy"]),
    ("edward", ["ed", "eddie", "eddy", "ted", "teddy"]),
    ("anthony", ["tony"]),
    ("nicholas", ["nick", "nicky"]),
    ("patricia", ["pat", "patty", "patti"]),
    ("patrick", ["paddy"]),
    ("joseph", ["joe", "joey"]),
    ("stephen", ["steve", "stevie", "steven"]),
    ("andrew", ["andy", "drew"]),
    ("matthew", ["matt", "matty"]),
    ("daniel", ["dan", "danny"]),
    ("benjamin", ["ben", "benny", "benji"]),
    ("samuel", ["sam", "sammy"]),
    ("alexander", ["alex", "lex"]),
    ("charles", ["charlie", "chuck", "chas"]),
    ("gregory", ["greg", "gregg"]),
    ("ronald", ["ron", "ronnie"]),
    ("donald", ["don", "donnie"]),
    ("jonathan", ["johnny", "jon", "john"]),
    ("christina", ["tina"]),
    ("deborah", ["deb", "debbie", "debby"]),
    ("jennifer", ["jen", "jenny", "jennie"]),
    ("rebecca", ["becky", "becca"]),
    ("cassandra", ["cassie"]),
    ("frederick", ["fred", "freddie", "freddy"]),
    ("augustus", ["gus", "august"]),
    ("henry", ["hank"]),
    ("lawrence", ["larry", "laurence"]),
    ("kimberly", ["kim", "kimmy", "kimberley"]),
    ("victoria", ["vicky", "vickie"]),
    ("nathaniel", ["nate", "nathan"]),
    ("zachary", ["zach", "zack"]),
    ("joshua", ["josh"]),
    ("abigail", ["abby", "abbie"]),
    ("gabriel", ["gabe"]),
    ("theodore", ["theo"]),
    ("arthur", ["art", "arty"]),
    ("philip", ["phil", "phillip"]),
    ("raymond", ["ray"]),
    ("walter", ["walt"]),
    ("wallace", ["wally"]),
    ("francis", ["frank", "frankie"]),
    ("timothy", ["tim", "timmy"]),
    ("vincent", ["vince", "vinny"]),
    ("barbara", ["barb", "barbie"]),
    ("caroline", ["carol", "carolyn"]),
    ("constance", ["connie"]),
    ("dorothy", ["dot", "dotty"]),
    ("eleanor", ["ellie", "elinor"]),
    ("frances", ["fanny"]),
    ("gerald", ["gerry"]),
    ("jessica", ["jess", "jessie"]),
    ("kaitlyn", ["kaitlin"]),
    ("louis", ["lou", "louie"]),
    ("martin", ["marty"]),
    ("nancy", ["nan"]),
    ("oliver", ["ollie"]),
    ("salvatore", ["sal"]),
    ("terrence", ["terry", "terence"]),
    ("valerie", ["val"]),
    ("gwendolyn", ["wendy"]),
]

SUFFIXES: frozenset[str] = frozenset({"jr", "sr", "ii", "iii", "iv", "2nd", "3rd"})


def _build_canon() -> dict[str, str]:
    """Build the token -> canonical-token lookup from _GROUPS at import time.

    Earlier groups win when a token appears in more than one group (kept deterministic).

    Returns:
        Dict mapping every variant/formal token to the group's formal name.
    """
    canon: dict[str, str] = {}
    for formal, variants in _GROUPS:
        for tok in (formal, *variants):
            canon.setdefault(tok, formal)
    return canon


_CANON: dict[str, str] = _build_canon()


def canon_token(tok: str) -> str:
    """The group's canonical token for `tok`, or `tok` unchanged if it's in no group."""
    if not tok:
        return tok
    return _CANON.get(tok.lower(), tok)


def map_tokens(tokens) -> frozenset[str]:
    """Map an iterable of tokens through canon_token, returning the canonical token set
    (suffix tokens are mapped through unchanged like any non-name token)."""
    return frozenset(canon_token(t) for t in tokens)


def suffixes(tokens) -> set[str]:
    """The generational-suffix tokens (jr/sr/iii/...) present in `tokens`."""
    return {t.lower() for t in tokens if t and t.lower() in SUFFIXES}
