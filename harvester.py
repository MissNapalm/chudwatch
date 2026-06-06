import requests
import time
import sqlite3
import collections
import re
import json
import html
import threading
import concurrent.futures
from http.server import HTTPServer, SimpleHTTPRequestHandler
from datetime import datetime, timezone
import sys
import os

# Suppress hashlib warnings from pyenv
import warnings
warnings.filterwarnings("ignore")
os.environ['PYTHONWARNINGS'] = 'ignore'

import spacy

# CONFIG
BOARD = 'pol'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(SCRIPT_DIR, 'chudwatch.db')
ARCHIVE_THREADS = 600   # recent closed threads to pull from archive in addition to live catalog
FETCH_WORKERS   = 8     # concurrent thread-fetch connections

try:
    nlp = spacy.load("en_core_web_sm")
except Exception:
    nlp = None

# Words that look like topics but aren't
STOP_TOPICS = {
    # Pronouns / determiners
    'They', 'This', 'That', 'What', 'There', 'These', 'Those', 'Which', 'Who',
    'It', 'He', 'She', 'We', 'You', 'Your', 'Their', 'Our', 'His', 'Her', 'Its',
    'Them', 'Him', 'Us', 'Me', 'My', 'Yourself', 'Himself', 'Herself', 'Itself',
    'Themselves', 'Ourselves', 'Yourselves', 'Whose', 'Whom',
    # Common adverbs / conjunctions that get mis-tagged
    'Even', 'Just', 'Also', 'Yeah', 'When', 'Because', 'Though', 'Although',
    'However', 'Therefore', 'Thus', 'Hence', 'Indeed', 'Actually', 'Really',
    'Already', 'Still', 'Now', 'Then', 'Here', 'Where', 'Why', 'How', 'Plus',
    'Maybe', 'Probably', 'Literally', 'Basically', 'Essentially', 'Especially',
    'Ally', 'Allied', 'Alliance', 'Allies',
    # Generic adjectives that aren't topics
    'Good', 'Bad', 'Big', 'Small', 'New', 'Old', 'First', 'Last', 'Many', 'Much',
    'Same', 'Different', 'Other', 'More', 'Most', 'Less', 'Least', 'Very', 'Every',
    'Some', 'Any', 'Each', 'Both', 'Either', 'Neither', 'Another', 'Such',
    # Common verbs that slip through
    'Says', 'Said', 'Think', 'Know', 'Want', 'Make', 'Made', 'Need', 'Look',
    'Come', 'Goes', 'Going', 'Having', 'Being', 'Getting', 'Take', 'Give',
    'Should', 'Would', 'Could', 'Will', 'Shall', 'Might', 'Must', 'May',
    'Better', 'Worse', 'Best', 'Worst', 'Higher', 'Lower', 'Longer',
    'Allowed', 'Allow', 'Allowing', 'Currently', 'Legally', 'Simply',
    'Clearly', 'Basically', 'Voting', 'Voted', 'Saying', 'Said', 'Means',
    'Learn', 'Learned', 'Learning', 'Main', 'Chang', 'Brown', 'Think',
    'Rump', 'Rumps', 'Mans', 'Euro', 'Euros',
    # HTML / URL garbage
    'Class', 'Href', 'Quotelink', 'Span', 'Quote', 'Quot', 'Br', 'Div',
    'Http', 'Https', 'Src', 'Alt', 'Img', 'Www', 'Com', 'Net', 'Org',
    # Chan-specific noise
    'Anonymous', 'Anon', 'Thread', 'Post', 'Board', 'Reply', 'Bump',
    # Contraction fragments
    'Don', 'Doesn', 'Didn', 'Isn', 'Aren', 'Wasn', 'Weren', 'Won', 'Can',
    'Couldn', 'Wouldn', 'Shouldn', 'Hasn', 'Haven', 'Hadn',
    # Expletives / generic insults — not topics (slurs are kept, these are just noise)
    'Shit', 'Shits', 'Fuck', 'Fucking', 'Fuckin', 'Fucked', 'Fucker',
    'Damn', 'Crap', 'Hell', 'Ass', 'Bitch', 'Retard', 'Retards',
    # Internet expressions / reactions — not topics
    'Lmao', 'Lmfao', 'Lol', 'Kek', 'Kek', 'Omg', 'Omfg', 'Wtf', 'Smh',
    # Common adverbs / discourse markers that slip through
    'Down', 'Again', 'Need', 'Needs', 'Cor', 'Well', 'Okay', 'Fine',
    'Sure', 'True', 'False', 'Real', 'Right', 'Wrong', 'Back', 'Away',
    'Over', 'Under', 'After', 'Before', 'During', 'Since',
    'Like', 'Just', 'Even', 'Only', 'Never', 'Always', 'Often', 'Stand',
    # Generic nouns that aren't specific topics
    'World', 'People', 'Thing', 'Things', 'Time', 'Times', 'Love', 'Life',
    'Country', 'Government', 'Society', 'Problem', 'Problems', 'Point', 'Fact',
    'Today', 'Yesterday', 'Week', 'Year', 'Years', 'Money', 'Power', 'Mans',
    'Round', 'East', 'West', 'North', 'South', 'State', 'States', 'Oman',
    'Cause', 'Causes', 'Caused', 'Effect', 'Result', 'Results', 'Reason', 'Reasons',
    'chang', 'thou',
}

# Collapsed topic groups: variant terms → single canonical label.
# A post is counted for the group if it contains ANY variant.
TOPIC_GROUPS = {
    'Indians':   ['india', 'indian', 'indians', 'jeet', 'jeets', 'pajeet', 'pajeets', 'poo in loo', 'poos'],
    'Jews':      ['jew', 'jews', 'jewish', 'jewry', 'kike', 'kikes', 'heeb', 'heebs'],
    'White':     ['white', 'whites', 'whiteness', 'white people', 'white man', 'white men',
                  'white woman', 'white women', 'white race', 'aryan', 'aryans'],
    'Trans':     ['transgender', 'trans', 'troon', 'trooned', 'tranny', 'trannies', 'transsexual', 'transsexuals'],
    'Black People':    ['black', 'blacks', 'nigger', 'niggers', 'nigga', 'niggas'],
    'Women':           ['woman', 'women', 'female', 'females', 'foid', 'foids', 'roastie', 'roasties'],
    'Muslims':   ['muslim', 'muslims', 'islam', 'islamic', 'islamist', 'islamists'],
    'America':   ['america', 'american', 'americans', 'usa', 'u.s.', 'merican'],
    'Trump':     ['trump', 'donald trump', 'drumpf', 'maga', 'trumpism', 'trumpist'],
    'Russia':    ['russia', 'russian', 'russians'],
    'China':     ['china', 'chinese', 'chink', 'chinks'],
    'Israel':    ['israel', 'israeli', 'israelis', 'zionist', 'zionists', 'zionism'],
    'Christian': ['christian', 'christians', 'christ', 'christkek', 'christkeks', 'christcuck', 'christcucks'],
    'Ukraine':   ['ukraine', 'ukrainian', 'ukrainians'],
    'Latinos':   ['latino', 'latinos', 'latina', 'latinas', 'hispanic', 'hispanics',
                  'mexican', 'mexicans', 'spic', 'spics'],
}

# Flat set of all variants that belong to a group (for fast exclusion)
_ALL_GROUP_VARIANTS = {v for variants in TOPIC_GROUPS.values() for v in variants}

# Precompiled word-boundary patterns (one per group) — prevents "trans" matching
# "translate", "chris" matching "christmas", etc.
_GROUP_PATTERNS = {
    canonical: re.compile(
        '|'.join(r'(?<!\w)' + re.escape(v) + r'(?!\w)' for v in variants)
    )
    for canonical, variants in TOPIC_GROUPS.items()
}

# --- HTTP server & refresh signal -------------------------------------------

PORT = 8080
refresh_event = threading.Event()

def write_progress(status, current, total, posts, message):
    pct = int(current / total * 100) if total > 0 else 0
    data = {"status": status, "current": current, "total": total,
            "posts": posts, "pct": pct, "message": message, "ts": time.time()}
    tmp = os.path.join(SCRIPT_DIR, 'progress.json.tmp')
    dest = os.path.join(SCRIPT_DIR, 'progress.json')
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, dest)

class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def end_headers(self):
        # Prevent browser from caching viewer.html so JS changes always take effect
        if self.path.split('?')[0].endswith('.html'):
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('Pragma', 'no-cache')
        super().end_headers()

    def do_POST(self):
        if self.path == '/refresh':
            _clear_generated_files()
            refresh_event.set()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.end_headers()

    def log_message(self, *args):
        pass  # silence request logs

def _start_server():
    server = HTTPServer(('', PORT), _Handler)
    server.serve_forever()

# ----------------------------------------------------------------------------

# --- "Someone Should" signal detection ---------------------------------------
#
# Signal tiers:
#   Tier 1 — Deniability / contextualization markers (tracks plausible-deniability framing)
#   Tier 2 — Dehumanization + passive incitement + coded violence (stepping-stone rhetoric)
#   Tier 3 — Direct incitement / violence delegation / doxxing
#   Tier 4 — Operational / mass-casualty / martyrdom glorification
#
# Patterns use word-boundary and negation guards to minimize false positives.
# Labels map to display categories in the viewer.
#
_RAW_PATTERNS = [
    # ── Tier 1: Deniability / hypothetical framing ─────────────────────────────
    (r"\bin minecraft\b",                                                                   "minecraft disclaimer",         1),
    (r"\bhypothetically (speaking|enough)\b",                                               "hypothetical framing",         1),
    (r"\bfor (a |the )?(story|novel|book|fiction|writing)\b",                               "fiction disclaimer",           1),
    (r"\b(just |only )?(as |a )?(joke|hypothetical|thought experiment|roleplay|rp)\b",      "joke disclaimer",              1),
    (r"\bnot (serious|saying|advocating|condoning)\b.{0,25}(kill|shoot|hang|gas|murder)",   "not-serious disclaimer",       1),
    (r"\bfor legal reasons\b",                                                              "legal disclaimer",             1),

    # ── Tier 2: Dehumanization ─────────────────────────────────────────────────
    # Subhuman / not-human framing
    (r"\b(sub-?humans?|subhumans?)\b",                                                      "dehumanization",               2),
    (r"\bnot (really |fully |even |actual )?humans?\b",                                     "dehumanization",               2),
    (r"\b(barely|hardly|less than|not quite) humans?\b",                                    "dehumanization",               2),
    (r"\b(animals?|beasts?|creatures?) (like|that) (them|these people|they)\b",             "dehumanization",               2),
    # Vermin / parasite metaphors
    (r"\b(parasites?|vermin|cockroaches?|rats?|locusts?|leeches?|bloodsuckers?)\b",         "vermin rhetoric",              2),
    (r"\b(infestation|infesting|infested)\b",                                               "vermin rhetoric",              2),
    (r"\b(exterminate|eradicate|purge).{0,30}\b(vermin|parasites?|cockroaches?|rats?)\b",   "vermin rhetoric",              2),
    # Disease/cancer metaphor
    (r"\b(cancer|plague|disease|infection|virus|rot|blight) of (the )?(west|society|nation|world|country|civilization)",
                                                                                            "disease rhetoric",             2),
    (r"\b(society|nation|country|the west|civilization) (is )?(infested|infected|rotting|dying) (with|because of)\b",
                                                                                            "disease rhetoric",             2),
    # Scum / filth framing
    (r"\b(absolute |total |utter )?(scum|filth|degenerates?|trash|garbage|vermin) (of|that) (society|the earth|humanity)\b",
                                                                                            "dehumanization",               2),
    (r"\b(human )?(scum|filth|trash|garbage|waste)\b",                                     "dehumanization",               2),

    # ── Tier 2: Passive incitement ─────────────────────────────────────────────
    (r"\bsomeone (should|needs? to|ought to|has to|must) (kill|shoot|stab|attack|bomb|murder|hang|gas|execute|eliminate|hurt|harm|take out|get rid of)\b",
                                                                                            "passive incitement",           2),
    (r"\bsomebody (should|needs? to|must) (kill|shoot|stab|attack|bomb|murder|hang|gas|execute|eliminate|hurt|harm)\b",
                                                                                            "passive incitement",           2),
    (r"\bif only (someone|somebody|a person) would (kill|shoot|eliminate|deal with|get rid of|stop|remove)\b",
                                                                                            "passive incitement",           2),
    (r"\bwish (someone|somebody) would (kill|shoot|hang|gas|remove|eliminate|take care of)\b",
                                                                                            "passive incitement",           2),
    (r"\b(too bad|shame|pity|unfortunate) (nobody|no one) (has|will|would) (kill|shoot|stop|deal with|remove|eliminate)\b",
                                                                                            "passive incitement",           2),
    (r"\b(rope[- ]ready|ripe for (hanging|the rope)|worthy of (a rope|the noose))\b",      "passive incitement",           2),

    # ── Tier 2: Veiled threats ─────────────────────────────────────────────────
    (r"\bbe a (real )?shame if (something happened|an accident|they got|he got|she got|it (got|was) (damaged|destroyed|burned|killed))\b",
                                                                                            "veiled threat",                2),
    (r"\bshame if (something|an accident|something bad) (happened?|were to happen|occurs?)\b",
                                                                                            "veiled threat",                2),
    (r"\baccidents? (can |do )?happen\b",                                                   "veiled threat",                2),
    (r"\bwon'?t be around (long|much longer|forever)\b",                                    "veiled threat",                2),
    (r"\bwouldn'?t (be|last) (long|much longer|much)\b",                                    "veiled threat",                2),
    (r"\b(watch (your|their|his|her) back|eyes? in the back of (your|their|his|her) head)\b",
                                                                                            "veiled threat",                2),
    (r"\b(something|things|it) (will|might|could) happen (to|for) (you|them|him|her|those|these)\b",
                                                                                            "veiled threat",                2),

    # ── Tier 2: Coded / meme violence references ───────────────────────────────
    # Pinochet helicopter-ride meme (political murder reference)
    (r"\b(free )?(helicopter|heli) (ride|rides|trip|trips)\b",                             "coded violence (helicopter)",  2),
    (r"\bpinochet.{0,20}(helicopter|heli|drop|drops|threw|throw)\b",                       "coded violence (helicopter)",  2),
    # Wood chipper / violence memes
    (r"\b(wood ?chipper|orc grinding|into the chipper)\b",                                 "coded violence (chipper)",     2),
    # "Against the wall" shooting-squad reference
    (r"\b(put|place|line|take|march).{0,15}\b(them|him|her|these (people|guys|fucks?)).{0,20}(against|up against|facing) the wall\b",
                                                                                            "coded violence (wall)",        2),
    # Ovens / Holocaust threat reuse
    (r"\b(back to|into|send.{0,10}to|straight to) the ovens?\b",                           "coded violence (ovens)",       2),
    (r"\bgas (the|all|every|those).{0,30}\b(kikes?|jews?|n+i+g+g+[ae]+r+s?|blacks?|muslims?|trannies|trans)\b",
                                                                                            "coded violence (gas)",         2),
    # "The rope" used as execution threat
    (r"\b(get|deserve|need|earn).{0,15}(the rope|a rope|the noose|the gallows)\b",         "coded violence (rope)",        2),
    (r"\b(rope|noose) for (them|him|her|all|every|those)\b",                               "coded violence (rope)",        2),
    # "Remove kebab" (anti-Muslim violence meme)
    (r"\bremove (kebab|kikes?|jews?|niggers?|trannies|faggots?)\b",                        "eliminationist rhetoric",      2),

    # ── Tier 2: Eliminationist / cleansing language ────────────────────────────
    (r"\b(ethnic |racial |cultural )?(cleansing|purification)\b",                          "eliminationist rhetoric",      2),
    (r"\b(cleanse|purge|purify).{0,30}(society|nation|country|the west|civilization|world)\b",
                                                                                            "eliminationist rhetoric",      2),
    (r"\b(rid|purge|cleanse|free).{0,20}(the world|our country|society|civilization).{0,20}(of|from) (them|these people|jews?|blacks?|muslims?|trannies?)\b",
                                                                                            "eliminationist rhetoric",      2),
    (r"\b(final solution|endlösung)\b",                                                    "eliminationist rhetoric",      2),
    (r"\b(demographic|racial|white) (replacement|genocide|erasure).{0,40}(must be stopped|or else|won't stand|will fight)\b",
                                                                                            "eliminationist rhetoric",      2),

    # ── Tier 3: Direct incitement ──────────────────────────────────────────────
    (r"\bneeds? to be (killed|shot|eliminated|exterminated|hanged?|gassed|executed|murdered|put down|taken out|liquidated|disposed of)\b",
                                                                                            "direct incitement",            3),
    (r"\bshould (all )?be (killed|shot|hanged?|gassed|executed|murdered|exterminated|put down|taken out|liquidated)\b",
                                                                                            "direct incitement",            3),
    # Negation guard for "deserve to die"
    (r"(?<!didn't )(?<!doesn't )(?<!never )(?<!not )deserves? to (die|be killed|be shot|be hanged?|be executed|hang|burn|suffer)\b",
                                                                                            "direct incitement",            3),
    # "do X a favour and [harm verb]"
    (r"\b(do|did) (the world|everyone|society|us all) a (favor|favour) and (kill|shoot|remove|eliminate|get rid of|take out|murder|execute|hang)\b",
                                                                                            "direct incitement",            3),
    (r"\bsomeone (deal|deals|dealt|dealing) with (him|her|them|this|that)\b",              "direct incitement",            3),
    (r"\bhas (this |it )?coming( to (him|her|them))?\b",                                   "direct incitement",            3),
    (r"\bnot going to end well for (him|her|them|these people)\b",                         "direct incitement",            3),
    # Explicit hanging/stringing up
    (r"\b(string|hang|lynch|strung|hanged?|lynched?).{0,20}(them|him|her|these|those|all (of )?(them|you))\b",
                                                                                            "direct incitement",            3),
    # Shooting / stabbing with target
    (r"\b(shoot|stab|knife|gun down|open fire on|mow down).{0,20}(them|him|her|these people|those people|all (of )?them)\b",
                                                                                            "direct incitement",            3),
    # Burning / firebombing
    (r"\b(burn (down|alive)|set (fire|alight)|firebomb).{0,20}(their|his|her|the).{0,20}(house|home|building|mosque|synagogue|church|school)\b",
                                                                                            "direct incitement",            3),
    # Exterminate / liquidate / annihilate groups
    (r"\b(exterminate|liquidate|annihilate|wipe out|erase).{0,20}(them|all (of )?them|every (last )?one|the (jews?|blacks?|muslims?|trannies?|gays?|whites?))\b",
                                                                                            "direct incitement",            3),
    # Doxxing / target coordination
    (r"\b(find|post|share|get|here('?s| is)).{0,20}(his|her|their).{0,20}(address|location|doxx|home address|workplace|school)\b",
                                                                                            "doxxing",                      3),
    (r"\b(lives? at|works? at|goes? to|found (him|her|them) at).{0,50}\b(street|avenue|road|drive|lane|boulevard|court)\b",
                                                                                            "doxxing",                      3),
    (r"\bpersonal info.{0,30}(posted|dropped|leaked|shared)\b",                            "doxxing",                      3),

    # ── Tier 4: Martyrdom glorification / mass-casualty / operational ──────────
    # Glorifying named attackers as "saints"
    (r"\b(saint|based|hero|martyr|legend).{0,20}(tarrant|breivik|roof|crusius|earnest|gendron|bowers|mateen|lanza)\b",
                                                                                            "martyrdom glorification",      4),
    (r"\b(tarrant|breivik|roof|crusius|earnest|gendron|bowers|mateen|lanza).{0,20}(did nothing wrong|was right|had the right idea|is a hero|based)\b",
                                                                                            "martyrdom glorification",      4),
    # "High score" mass-casualty gamification
    (r"\b(high score|highscore|beat (the|his|their) (score|record|count|number))\b",       "mass-casualty gamification",   4),
    (r"\b(body count|kill count|death toll).{0,20}(record|high|score|beat|break)\b",       "mass-casualty gamification",   4),
    # Operational/planning language
    (r"\b(manifesto|written manifesto|posted (his|a|the) manifesto)\b",                    "operational signal",           4),
    (r"\b(target[- ]rich environment)\b",                                                  "operational signal",           4),
    (r"\blone wolf.{0,20}(attack|strike|operation|act)\b",                                 "operational signal",           4),
    (r"\b(mass (shooting|casualty|attack|stabbing)).{0,20}(plan|planning|when|how|where)\b",
                                                                                            "operational signal",           4),
    # Accelerationist trigger language
    (r"\b(race war (now|soon|is coming|is inevitable|start|begin))\b",                     "accelerationist call",         4),
    (r"\b(boogaloo|big igloo|big luau).{0,15}(soon|now|start|begin|time|when)\b",          "accelerationist call",         4),
    (r"\b(collapse.{0,20}(society|civilization|system)|burn.{0,20}(it all|everything|the system) down)\b",
                                                                                            "accelerationist call",         4),
    (r"\baccelerationis[mt].{0,30}(only way|the answer|will|right|correct|necessary)\b",   "accelerationist call",         4),
    (r"\bday of the rope\b",                                                                "accelerationist call",         4),
    (r"\brahowa\b",                                                                         "accelerationist call",         4),
]

SIGNAL_PATTERNS = [
    (re.compile(pat, re.IGNORECASE), label, tier)
    for pat, label, tier in _RAW_PATTERNS
]

# --- Radicalization jargon tiers ---------------------------------------------

JARGON_TIERS = {
    1: {
        "label": "Entry Level",
        "desc":  "General chan slang and mainstream skepticism — gateway vocabulary",
        "terms": ["msm", "mainstream media", "fake news", "woke", "deep state",
                  "sheeple", "normie", "normies", "bluepilled", "blue pill",
                  "red pill", "redpill", "wake up", "they don't want you to know",
                  "based", "cuck", "npc", "soyjak", "soy boy", "clown world",
                  "blackpilled", "black pill"],
    },
    2: {
        "label": "Intermediate",
        "desc":  "Explicit ideological commitment — in-group doctrine terms",
        "terms": ["redpilled", "globalist", "race realist", "race realism",
                  "ethnostate", "white nationalist", "great replacement",
                  "demographic replacement", "replacement theory",
                  "civic nationalist", "race traitor", "white genocide"],
    },
    3: {
        "label": "Deep End",
        "desc":  "Extremist / pre-violence terminology",
        "terms": ["zog", "day of the rope", "dotr", "rwds", "14 words", "1488",
                  "race war", "rahowa", "accelerate", "accelerationism",
                  "turner diaries", "saint tarrant", "saint breivik", "saint roof",
                  "final solution"],
    },
}

# --- Conspiracy theory definitions -------------------------------------------
# Each entry: id → (keyword_patterns, display_label, tier)
# Tier 1 = entry-point (gateway),  2 = intermediate,  3 = deep end

CONSPIRACY_DEFS = {
    "media_bias":        (["mainstream media", "msm", "fake news", "media lies",
                           "media is lying", "they control the media"],
                          "Media Bias", 1),
    "crime_stats":       (["black crime", "crime statistics", "crime stats",
                           "they hide the", "unreported crime"],
                          "Hidden Crime Stats", 1),
    "deep_state":        (["deep state", "shadow government", "the establishment",
                           "they control", "behind the scenes"],
                          "Deep State", 1),
    "great_replacement": (["replacement", "replacing us", "demographic replacement",
                           "they're replacing", "replaced by"],
                          "Great Replacement", 1),
    "globalism":         (["globalist", "globalism", "new world order", "nwo",
                           "global elite", "george soros", "open borders agenda"],
                          "Globalism / NWO", 2),
    "jewish_control":    (["jewish control", "jews control", "jewish media",
                           "jewish owned", "jewish influence", "jewish power",
                           "the jews run", "jewish agenda"],
                          "Jewish Control", 2),
    "zog":               (["zog", "zionist occupied", "zionist agenda",
                           "israel controls", "jewish state controls"],
                          "ZOG / Zionism", 2),
    "white_genocide":    (["white genocide", "genocide of whites", "anti-white",
                           "anti white", "extermination of whites",
                           "whites are being"],
                          "White Genocide", 2),
    "iq_race":           (["race and iq", "racial iq", "iq differences",
                           "iq by race", "african iq", "average iq"],
                          "Race & IQ", 2),
    "immigration_plot":  (["replacement immigration", "immigration agenda",
                           "open borders", "they want immigrants",
                           "importing voters", "demographic weapon"],
                          "Immigration Plot", 2),
    "accelerationism":   (["accelerate", "accelerationism", "let it burn",
                           "hasten the collapse", "collapse the system",
                           "boogaloo"],
                          "Accelerationism", 3),
    "race_war":          (["race war", "race war now", "civil war 2",
                           "rahowa", "it's coming", "revolution is"],
                          "Race War", 3),
    "turner_diaries":    (["turner diaries", "day of the rope", "dotr",
                           "rwds", "14 words", "1488", "88",
                           "saint tarrant", "saint breivik"],
                          "Turner Diaries / 1488", 3),
}

# Precompile conspiracy patterns
_CONSPIRACY_COMPILED = {
    cid: ([re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE) for kw in kws], label, tier)
    for cid, (kws, label, tier) in CONSPIRACY_DEFS.items()
}

# --- AI integration ---------------------------------------------------------

try:
    import anthropic as _anthropic_mod
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

_AI_CLIENT = None

def _get_ai_client():
    global _AI_CLIENT
    if _AI_CLIENT is not None:
        return _AI_CLIENT
    if not _ANTHROPIC_AVAILABLE:
        return None
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        return None
    _AI_CLIENT = _anthropic_mod.Anthropic(api_key=key)
    return _AI_CLIENT


def ai_classify_signals(signal_results):
    """
    Re-classify regex-flagged posts with Claude to strip false positives.
    Adds 'ai_verdict' ("confirmed" / "ambiguous" / "false_positive") and
    'ai_reason' to each result. Returns the list unchanged if AI unavailable.
    """
    client = _get_ai_client()
    if not client or not signal_results:
        return signal_results

    candidates = signal_results[:40]
    payload = [{"id": str(s['post_id']), "text": s['snippet']} for s in candidates]

    prompt = (
        "You are a researcher studying extremist online communities. "
        "These posts from 4chan's /pol/ board were flagged by keyword patterns "
        "for potential incitement language.\n\n"
        "For each post classify as ONE of:\n"
        "- \"confirmed\": clearly expresses desire for real-world violence against a specific target\n"
        "- \"ambiguous\": could be threatening but lacks a clear target or reads as rhetorical flourish\n"
        "- \"false_positive\": generic trash talk, political commentary, or innocent use of the phrase\n\n"
        "Posts:\n" + json.dumps(payload) + "\n\n"
        "Respond with a JSON array only — "
        "[{\"id\": \"...\", \"verdict\": \"...\", \"reason\": \"one sentence\"}]. "
        "No markdown, no text outside the JSON."
    )

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        verdicts = json.loads(msg.content[0].text.strip())
        verdict_map = {v['id']: v for v in verdicts}
        for s in candidates:
            v = verdict_map.get(str(s['post_id']))
            if v:
                s['ai_verdict'] = v['verdict']
                s['ai_reason']  = v.get('reason', '')
        confirmed = sum(1 for s in candidates if s.get('ai_verdict') == 'confirmed')
        fp        = sum(1 for s in candidates if s.get('ai_verdict') == 'false_positive')
        print(f"[AI] Signals: {confirmed} confirmed, {fp} false-positives flagged")
    except Exception as e:
        print(f"[AI] Signal classification failed: {e}")

    return signal_results


def generate_pulse(trends, posts_sample):
    """
    Generate a 2-3 sentence analytical narrative of the current harvest.
    Returns the text string, or None if AI is unavailable.
    """
    client = _get_ai_client()
    if not client:
        return None

    topics_str   = ', '.join(f"{t[0]} ({t[1]})" for t in trends[:12])
    sample_texts = []
    for p in posts_sample[:30]:
        raw = p.get('com', '')
        if not raw:
            continue
        # strip HTML inline since clean_text is defined later in the file
        t = re.sub(r'<[^>]+>', ' ', raw)
        t = html.unescape(t)
        t = re.sub(r'https?://\S+', '', t)
        t = re.sub(r'\s+', ' ', t).strip()[:200]
        if t:
            sample_texts.append(t)

    prompt = (
        "You are analyzing scraped data from 4chan's /pol/ board for academic research "
        "on online extremism and radicalization.\n\n"
        f"Trending topics: {topics_str}\n\n"
        "Sample posts (truncated):\n"
        + "\n".join(f"- {t}" for t in sample_texts[:25]) + "\n\n"
        "Write a 2-3 sentence analytical 'pulse' covering: what narratives dominate, "
        "the overall emotional tenor, and any notable escalation or coordination patterns. "
        "Be analytical and detached. No warnings, disclaimers, or moralizing."
    )

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        print(f"[AI] Pulse generated ({len(text)} chars)")
        return text
    except Exception as e:
        print(f"[AI] Pulse generation failed: {e}")
        return None


def detect_astroturf(posts):
    """
    Multi-signal coordinated inauthentic behaviour detection across live + archived posts.

    Signals:
      1. URL bombing           — same URL pushed across many threads
      2. Near-duplicate text   — copy-paste reply campaigns (Jaccard similarity)
      3. Injected phrases      — verbatim 6-word phrases appearing across many threads
      4. OP template flooding  — multiple thread OPs with near-identical text
      5. Narrative velocity    — topics spiking in live vs archive baseline
      6. AI synthesis          — Claude assesses all signals together
    """
    client      = _get_ai_client()
    live_posts  = [p for p in posts if p.get('source') == 'live']
    arch_posts  = [p for p in posts if p.get('source') == 'archive']

    def _clean(raw):
        t = re.sub(r'<[^>]+>', ' ', raw)
        t = html.unescape(t)
        t = re.sub(r'https?://\S+', '', t)
        return re.sub(r'\s+', ' ', t).strip()

    _STOPS = {
        'this','that','with','from','they','have','will','been','were','just',
        'like','your','what','when','there','their','about','would','could',
        'should','which','more','some','than','then','also','even','only','very',
        'said','says','dont','cant','wont','isnt','arent','doesnt',
    }

    def _fp(text):
        return set(w for w in re.findall(r'\b[a-z]{4,}\b', text.lower()) if w not in _STOPS)

    # ── 1. URL bombing ────────────────────────────────────────────────────────
    url_re      = re.compile(r'https?://\S+')
    url_threads = collections.defaultdict(set)
    url_live    = collections.defaultdict(int)
    url_samples = collections.defaultdict(list)

    for p in posts:
        raw = p.get('com', '')
        if not raw:
            continue
        tid = p.get('thread_id')
        for m in url_re.finditer(html.unescape(re.sub(r'<[^>]+>', ' ', raw))):
            url = m.group(0).rstrip('.,)"\'').lower()[:120]
            url_threads[url].add(tid)
            if p.get('source') == 'live':
                url_live[url] += 1
            if len(url_samples[url]) < 4:
                url_samples[url].append(_clean(raw)[:150])

    url_bombs = sorted(
        [{"url": u, "thread_count": len(ts),
          "live_mentions": url_live[u], "samples": url_samples[u]}
         for u, ts in url_threads.items() if len(ts) >= 4],
        key=lambda x: -x['thread_count']
    )[:20]

    # ── 2. Near-duplicate reply clusters ─────────────────────────────────────
    content = [p for p in posts if len(p.get('com', '')) > 100]
    # sample evenly across live + archive so neither overwhelms
    live_c = [p for p in content if p.get('source') == 'live'][:400]
    arch_c = [p for p in content if p.get('source') == 'archive'][:400]
    content = live_c + arch_c
    fps = [_fp(_clean(p.get('com', ''))) for p in content]

    visited, clusters = set(), []
    for i, fp_i in enumerate(fps):
        if i in visited or not fp_i:
            continue
        group = [content[i]]
        for j in range(i + 1, len(fps)):
            if j in visited or not fps[j]:
                continue
            union = fp_i | fps[j]
            if union and len(fp_i & fps[j]) / len(union) >= 0.55:
                group.append(content[j])
                visited.add(j)
        if len(group) >= 3:
            visited.add(i)
            clusters.append(group)

    clusters.sort(key=lambda g: -len(g))
    dup_clusters = [
        {
            "size":        len(g),
            "live_count":  sum(1 for p in g if p.get('source') == 'live'),
            "arch_count":  sum(1 for p in g if p.get('source') == 'archive'),
            "thread_ids":  list({p.get('thread_id') for p in g if p.get('thread_id')}),
            "samples":     [_clean(p.get('com', ''))[:200] for p in g[:4]],
        }
        for g in clusters[:10]
    ]

    # ── 3. Injected phrase detection ──────────────────────────────────────────
    # 6-word verbatim n-grams appearing across many distinct threads
    phrase_threads  = collections.defaultdict(set)
    phrase_live     = collections.defaultdict(int)
    phrase_examples = collections.defaultdict(list)
    ngram_n = 6

    for p in posts:
        raw = p.get('com', '')
        if not raw or len(raw) < 40:
            continue
        text = _clean(raw).lower()
        tid  = p.get('thread_id')
        words = re.findall(r"[a-z']{3,}", text)
        for k in range(len(words) - ngram_n + 1):
            phrase = ' '.join(words[k:k + ngram_n])
            if len(phrase) < 28:   # skip very short/trivial phrases
                continue
            phrase_threads[phrase].add(tid)
            if p.get('source') == 'live':
                phrase_live[phrase] += 1
            if len(phrase_examples[phrase]) < 3:
                phrase_examples[phrase].append(_clean(raw)[:180])

    injected_phrases = sorted(
        [{"phrase": ph, "thread_count": len(ts),
          "live_count": phrase_live[ph], "samples": phrase_examples[ph]}
         for ph, ts in phrase_threads.items() if len(ts) >= 5],
        key=lambda x: (-x['thread_count'], -x['live_count'])
    )[:20]

    # ── 4. OP template flooding ───────────────────────────────────────────────
    # Cluster opening posts (no == thread_id) by similarity — same template = coordinated
    ops = [p for p in posts if str(p.get('no')) == str(p.get('thread_id')) and p.get('com')]
    op_fps = [(_fp(_clean(p.get('com', ''))), p) for p in ops]
    op_fps = [(fp, p) for fp, p in op_fps if len(fp) >= 6]

    op_visited, op_clusters = set(), []
    for i, (fp_i, p_i) in enumerate(op_fps):
        if i in op_visited:
            continue
        group = [p_i]
        for j in range(i + 1, len(op_fps)):
            if j in op_visited:
                continue
            fp_j = op_fps[j][0]
            union = fp_i | fp_j
            if union and len(fp_i & fp_j) / len(union) >= 0.50:
                group.append(op_fps[j][1])
                op_visited.add(j)
        if len(group) >= 3:
            op_visited.add(i)
            op_clusters.append(group)

    op_clusters.sort(key=lambda g: -len(g))
    op_templates = [
        {
            "size":       len(g),
            "live_count": sum(1 for p in g if p.get('source') == 'live'),
            "arch_count": sum(1 for p in g if p.get('source') == 'archive'),
            "thread_ids": [p.get('thread_id') for p in g],
            "samples":    [_clean(p.get('com', ''))[:200] for p in g[:4]],
        }
        for g in op_clusters[:10]
    ]

    # ── 5. Narrative velocity spikes ─────────────────────────────────────────
    # Compare topic group rates in live vs archive to spot sudden pushes
    def _rate(post_list):
        total = max(len([p for p in post_list if p.get('com')]), 1)
        counts = {}
        for canonical, pattern in _GROUP_PATTERNS.items():
            n = sum(1 for p in post_list if p.get('com') and
                    pattern.search(p['com'].lower()))
            counts[canonical] = n / total
        return counts, total

    live_rates, live_total = _rate(live_posts)
    arch_rates, arch_total = _rate(arch_posts) if arch_posts else ({}, 1)

    velocity_spikes = []
    if arch_posts:
        for topic, live_r in live_rates.items():
            arch_r = arch_rates.get(topic, 0)
            if arch_r > 0 and live_r / arch_r >= 1.8 and live_r * live_total >= 10:
                velocity_spikes.append({
                    "topic":      topic,
                    "live_rate":  round(live_r * 100, 2),
                    "arch_rate":  round(arch_r * 100, 2),
                    "multiplier": round(live_r / arch_r, 1),
                    "live_posts": int(live_r * live_total),
                })
        velocity_spikes.sort(key=lambda x: -x['multiplier'])

    # ── 6. AI synthesis ───────────────────────────────────────────────────────
    ai_assessment = None
    signals_found = bool(url_bombs or dup_clusters or injected_phrases or
                         op_templates or velocity_spikes)
    if client and signals_found:
        lines = []
        for u in url_bombs[:3]:
            lines.append(f"URL bombed across {u['thread_count']} threads "
                         f"({u['live_mentions']} live mentions): {u['url']}")
        for c in dup_clusters[:3]:
            lines.append(f"Copy-paste cluster: {c['size']} near-identical posts "
                         f"({c['live_count']} live / {c['arch_count']} archived) "
                         f"across {len(c['thread_ids'])} threads — \"{c['samples'][0][:100]}...\"")
        for ph in injected_phrases[:3]:
            lines.append(f"Phrase injected into {ph['thread_count']} threads "
                         f"({ph['live_count']} live): \"{ph['phrase']}\"")
        for ot in op_templates[:2]:
            lines.append(f"OP template: {ot['size']} near-identical thread-starters "
                         f"({ot['live_count']} live / {ot['arch_count']} archived) — "
                         f"\"{ot['samples'][0][:100]}...\"")
        for v in velocity_spikes[:3]:
            lines.append(f"Velocity spike: '{v['topic']}' at {v['multiplier']}x normal rate "
                         f"({v['live_posts']} live posts vs {v['arch_rate']}% archive baseline)")

        prompt = (
            "You are analyzing 4chan /pol/ data for academic research on coordinated "
            "inauthentic behaviour and information operations.\n\n"
            f"Live harvest: {live_total} posts. Archive baseline: {arch_total} posts.\n\n"
            "Detected signals:\n"
            + "\n".join(f"- {l}" for l in lines) + "\n\n"
            "In 3-4 sentences, assess: which signals look like genuine coordination "
            "vs organic behaviour? What narrative or agenda is being pushed? "
            "Does the live/archive comparison suggest an active ongoing campaign "
            "or a fading one? Be specific and analytical."
        )
        try:
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            ai_assessment = msg.content[0].text.strip()
            print(f"[AI] Astroturf assessment generated")
        except Exception as e:
            print(f"[AI] Astroturf assessment failed: {e}")

    # ── 7. OP image recycling ──────────────────────────────────────────────────
    # Same image MD5 used to start multiple threads → seeding from a template
    op_img_map = collections.defaultdict(list)
    for p in posts:
        if str(p.get('no')) == str(p.get('thread_id')) and p.get('md5'):
            op_img_map[p['md5']].append({
                "thread_id": p['thread_id'],
                "source":    p.get('source', ''),
                "filename":  p.get('filename', ''),
                "ext":       p.get('ext', '.jpg'),
                "tim":       p.get('tim', ''),
            })

    op_image_recycling = sorted(
        [
            {
                "md5":          md5,
                "thread_count": len(ts),
                "live_count":   sum(1 for t in ts if t['source'] == 'live'),
                "arch_count":   sum(1 for t in ts if t['source'] == 'archive'),
                "filename":     ts[0]['filename'],
                "thumb_url":    f"https://i.4cdn.org/{BOARD}/{ts[0]['tim']}s.jpg"
                                if ts[0].get('tim') else '',
                "threads":      [t['thread_id'] for t in ts[:10]],
            }
            for md5, ts in op_img_map.items() if len(ts) >= 2
        ],
        key=lambda x: -x['thread_count']
    )[:20]

    out = {
        "updated":             utcnow(),
        "live_post_count":     live_total,
        "archive_post_count":  len(arch_posts),
        "url_bombs":           url_bombs,
        "duplicate_clusters":  dup_clusters,
        "injected_phrases":    injected_phrases,
        "op_templates":        op_templates,
        "velocity_spikes":     velocity_spikes,
        "op_image_recycling":  op_image_recycling,
        "ai_assessment":       ai_assessment,
    }
    dest = os.path.join(SCRIPT_DIR, 'astroturf.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    print(f"[+] Astroturf: {len(url_bombs)} URL bombs, {len(dup_clusters)} dup clusters, "
          f"{len(injected_phrases)} injected phrases, {len(op_templates)} OP templates, "
          f"{len(velocity_spikes)} velocity spikes, {len(op_image_recycling)} recycled OP images")


# ---------------------------------------------------------------------------

def detect_signals(posts):
    """
    Scan current batch for passive-incitement language.
    Returns list of signal dicts sorted by score desc.
    """
    results = []
    for p in posts:
        raw = p.get('com', '')
        if not raw:
            continue
        text = clean_text(raw)
        if len(text) < 10:
            continue

        matched_categories = []
        matched_triggers   = []
        max_tier = 0

        for pattern, label, tier in SIGNAL_PATTERNS:
            m = pattern.search(text)
            if m:
                matched_categories.append(label)
                matched_triggers.append(m.group(0).strip())
                if tier > max_tier:
                    max_tier = tier

        if not matched_triggers:
            continue

        # Score = sum of tier values for all matched patterns
        score = sum(tier for pat, _label, tier in SIGNAL_PATTERNS
                    if pat.search(text))

        # Snippet: 120 chars around first match for preview
        first_match = SIGNAL_PATTERNS[0][0].search(text)
        for pat, _, _ in SIGNAL_PATTERNS:
            m = pat.search(text)
            if m:
                start = max(0, m.start() - 80)
                end   = min(len(text), m.end() + 80)
                snippet = ('...' if start > 0 else '') + text[start:end] + ('...' if end < len(text) else '')
                break

        results.append({
            "post_id":    p['no'],
            "thread_id":  p.get('thread_id'),
            "score":      score,
            "max_tier":   max_tier,
            "categories": list(dict.fromkeys(matched_categories)),  # deduped, ordered
            "triggers":   list(dict.fromkeys(t.lower() for t in matched_triggers)),
            "snippet":    snippet,
            "comment":    text,
        })

    results.sort(key=lambda x: (-x['score'], -x['max_tier']))
    results = ai_classify_signals(results)
    # Re-sort: confirmed AI verdicts bubble to top, false-positives sink
    _verdict_rank = {"confirmed": 0, "ambiguous": 1, "false_positive": 2, None: 1}
    results.sort(key=lambda x: (_verdict_rank.get(x.get('ai_verdict')), -x['score'], -x['max_tier']))

    now = utcnow()
    out = {"updated": now, "count": len(results), "signals": results[:200]}
    dest = os.path.join(SCRIPT_DIR, 'signals.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    t4 = sum(1 for r in results if r['max_tier'] == 4)
    t3 = sum(1 for r in results if r['max_tier'] == 3)
    print(f"[+] Signals: {len(results)} posts matched (T4={t4} operational, T3={t3} direct incitement)")


def detect_jargon(posts):
    """Classify posts by radicalization vocabulary tier and track term frequency."""
    tier_counts   = {1: 0, 2: 0, 3: 0}
    tier_term_hits = {1: collections.Counter(), 2: collections.Counter(), 3: collections.Counter()}

    for p in posts:
        raw = p.get('com', '')
        if not raw:
            continue
        text = clean_text(raw).lower()
        post_max_tier = 0
        for tier, info in JARGON_TIERS.items():
            for term in info['terms']:
                if term in text:
                    tier_term_hits[tier][term] += 1
                    if tier > post_max_tier:
                        post_max_tier = tier
        if post_max_tier > 0:
            tier_counts[post_max_tier] += 1

    total_posts = sum(1 for p in posts if p.get('com'))
    out = {
        "updated":     utcnow(),
        "total_posts": total_posts,
        "tiers": {
            str(t): {
                "label":      JARGON_TIERS[t]["label"],
                "desc":       JARGON_TIERS[t]["desc"],
                "post_count": tier_counts[t],
                "pct":        round(tier_counts[t] / total_posts * 100, 1) if total_posts else 0,
                "top_terms":  tier_term_hits[t].most_common(15),
            }
            for t in (1, 2, 3)
        },
    }
    dest = os.path.join(SCRIPT_DIR, 'jargon.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    print(f"[+] Jargon: T1={tier_counts[1]} T2={tier_counts[2]} T3={tier_counts[3]} posts")


def detect_conspiracies(posts):
    """
    Detect conspiracy theory co-occurrence across posts.
    Outputs nodes (theories) and edges (co-occurrence pairs) for network viz.
    """
    theory_posts  = {cid: [] for cid in CONSPIRACY_DEFS}  # posts per theory
    theory_counts = collections.Counter()

    for p in posts:
        raw = p.get('com', '')
        if not raw:
            continue
        text = clean_text(raw).lower()
        present = []
        for cid, (pats, label, tier) in _CONSPIRACY_COMPILED.items():
            if any(pat.search(text) for pat in pats):
                present.append(cid)
                theory_counts[cid] += 1
        for cid in present:
            theory_posts[cid].append(p['no'])

    # Co-occurrence edges: count posts where both theories appear
    cids = [cid for cid in CONSPIRACY_DEFS if theory_counts[cid] > 0]
    edges = []
    for i, a in enumerate(cids):
        a_set = set(theory_posts[a])
        for b in cids[i+1:]:
            overlap = len(a_set & set(theory_posts[b]))
            if overlap > 0:
                edges.append({"source": a, "target": b, "weight": overlap})

    nodes = [
        {
            "id":    cid,
            "label": CONSPIRACY_DEFS[cid][1],
            "tier":  CONSPIRACY_DEFS[cid][2],
            "count": theory_counts[cid],
        }
        for cid in CONSPIRACY_DEFS if theory_counts[cid] > 0
    ]
    nodes.sort(key=lambda n: -n["count"])
    edges.sort(key=lambda e: -e["weight"])

    out = {"updated": utcnow(), "nodes": nodes, "edges": edges[:100]}
    dest = os.path.join(SCRIPT_DIR, 'conspiracies.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    print(f"[+] Conspiracies: {len(nodes)} theories, {len(edges)} co-occurrence links")


def detect_memes(posts):
    """Track most-reposted images by MD5 hash. Same file = same meme."""
    md5_info = {}
    for p in posts:
        if not p.get('md5') or not p.get('tim'):
            continue
        md5 = p['md5']
        if md5 not in md5_info:
            md5_info[md5] = {
                'count': 0, 'tim': p['tim'],
                'ext': p.get('ext', '.jpg'),
                'filename': p.get('filename', ''),
                'threads': set(),
            }
        md5_info[md5]['count'] += 1
        if p.get('thread_id'):
            md5_info[md5]['threads'].add(p['thread_id'])

    results = [
        {
            'md5':          md5,
            'count':        d['count'],
            'thread_count': len(d['threads']),
            'filename':     d['filename'],
            'url':          f"https://i.4cdn.org/{BOARD}/{d['tim']}{d['ext']}",
            'thumb':        f"https://i.4cdn.org/{BOARD}/{d['tim']}s.jpg",
        }
        for md5, d in md5_info.items()
    ]
    results.sort(key=lambda x: -x['count'])

    out = {"updated": utcnow(), "memes": results[:150]}
    dest = os.path.join(SCRIPT_DIR, 'memes.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    print(f"[+] Memes: {len(results)} recurring images")


def detect_trans(posts):
    """
    Find all posts mentioning trans-related terms, classify each by type of
    anti-trans content via Claude, and write trans.json.
    """
    trans_pat = _GROUP_PATTERNS['Trans']
    matched = []
    for p in posts:
        raw = p.get('com', '')
        if not raw:
            continue
        text = clean_text(raw)
        if not trans_pat.search(text.lower()):
            continue
        matched.append({
            "post_id":   p['no'],
            "thread_id": p.get('thread_id'),
            "snippet":   text[:400],
        })

    # AI classification ---------------------------------------------------
    categories = {
        "threat": 0, "dehumanization": 0, "political": 0,
        "mockery": 0, "neutral": 0, "unclassified": 0,
    }
    ai_summary = ""
    client = _get_ai_client()

    if client and matched:
        # Classify up to 60 posts with haiku
        batch = matched[:60]
        payload = [{"id": str(r["post_id"]), "text": r["snippet"]} for r in batch]
        classify_prompt = (
            "You are a researcher studying anti-trans rhetoric in online extremist communities. "
            "These posts are from 4chan's /pol/ board and all mention trans-related terms.\n\n"
            "Classify each post into ONE of these categories:\n"
            "- \"threat\": explicit or veiled violence/harm directed at trans people\n"
            "- \"dehumanization\": denies trans identity, frames being trans as mental illness, "
            "groomer/predator narratives, or calls for elimination of trans people\n"
            "- \"political\": opposes trans policies (sports, bathrooms, medicine) without "
            "personal attacks or dehumanization\n"
            "- \"mockery\": slurs, jokes, mocking trans identity without escalating to threats\n"
            "- \"neutral\": informational, ambiguous, or does not express hostility\n\n"
            "Posts:\n" + json.dumps(payload) + "\n\n"
            "Respond with a JSON array only — "
            "[{\"id\": \"...\", \"category\": \"...\", \"reason\": \"5 words max\"}]. "
            "No markdown, no text outside the JSON."
        )
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=3000,
                messages=[{"role": "user", "content": classify_prompt}],
            )
            verdicts = json.loads(msg.content[0].text.strip())
            vmap = {v["id"]: v for v in verdicts}
            for r in batch:
                v = vmap.get(str(r["post_id"]))
                if v and v.get("category") in categories:
                    r["category"] = v["category"]
                    r["ai_reason"] = v.get("reason", "")
                    categories[v["category"]] += 1
                else:
                    r["category"] = "unclassified"
                    categories["unclassified"] += 1
            print(f"[AI] Trans: classified {len(verdicts)} posts")
        except Exception as e:
            print(f"[AI] Trans classification failed: {e}")
            for r in batch:
                r["category"] = "unclassified"
                categories["unclassified"] += len(batch)

        # Mark anything not in batch as unclassified
        for r in matched[60:]:
            r["category"] = "unclassified"

        # Narrative summary with sonnet
        top_snips = [r["snippet"][:200] for r in matched[:20]]
        cat_str = ", ".join(f"{k}: {v}" for k, v in categories.items() if v > 0)
        summary_prompt = (
            "You are analyzing anti-trans rhetoric scraped from 4chan's /pol/ board "
            "for academic research on online extremism.\n\n"
            f"Total trans-related posts this harvest: {len(matched)}\n"
            f"Category breakdown: {cat_str}\n\n"
            "Sample posts:\n" + "\n".join(f"- {s}" for s in top_snips) + "\n\n"
            "Write 2-3 sentences describing: the dominant rhetorical frames being used, "
            "the emotional tenor, and any notable narratives or escalation patterns. "
            "Be analytical and detached. No disclaimers or moralizing."
        )
        try:
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                messages=[{"role": "user", "content": summary_prompt}],
            )
            ai_summary = msg.content[0].text.strip()
            print(f"[AI] Trans summary generated ({len(ai_summary)} chars)")
        except Exception as e:
            print(f"[AI] Trans summary failed: {e}")
    else:
        for r in matched:
            r["category"] = "unclassified"

    out = {
        "updated":    utcnow(),
        "count":      len(matched),
        "categories": categories,
        "ai_summary": ai_summary,
        "posts":      matched[:300],
    }
    dest = os.path.join(SCRIPT_DIR, 'trans.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    print(f"[+] Trans sentiment: {len(matched)} posts")


def detect_flags(posts):
    """
    Aggregate country/board flags and compute country × topic correlation.
    Countries posting disproportionately about a topic vs their overall share
    get a high ratio score — useful for spotting foreign influence operations.
    """
    country_counts  = collections.Counter()
    country_names   = {}
    troll_counts    = collections.Counter()
    troll_names     = {}
    # country → topic → post count
    country_topic   = collections.defaultdict(lambda: collections.Counter())

    total_posts_with_com = sum(1 for p in posts if p.get('com'))
    # overall topic rates
    overall_topic_counts = {}
    for canonical, pattern in _GROUP_PATTERNS.items():
        overall_topic_counts[canonical] = sum(
            1 for p in posts if p.get('com') and pattern.search(p['com'].lower())
        )

    for p in posts:
        if not p.get('com'):
            continue
        cc  = p.get('country', '').upper().strip()
        cn  = p.get('country_name', '').strip()
        bf  = p.get('board_flag', '').upper().strip()
        bfn = p.get('board_flag_name', '').strip()
        if cc:
            country_counts[cc] += 1
            if cn:
                country_names[cc] = cn
            text_lower = p['com'].lower()
            for canonical, pattern in _GROUP_PATTERNS.items():
                if pattern.search(text_lower):
                    country_topic[cc][canonical] += 1
        if bf:
            troll_counts[bf] += 1
            if bfn:
                troll_names[bf] = bfn

    # Build correlation matrix for countries with enough posts
    correlation = []
    for cc, total in country_counts.most_common(30):
        if total < 15:
            continue
        topics_out = {}
        for canonical, ct_count in country_topic[cc].items():
            overall_rate = overall_topic_counts.get(canonical, 0) / max(total_posts_with_com, 1)
            country_rate = ct_count / total
            ratio = round(country_rate / overall_rate, 2) if overall_rate > 0 else 0
            topics_out[canonical] = {
                "count": ct_count,
                "pct":   round(country_rate * 100, 1),
                "ratio": ratio,   # >2 = disproportionate focus
            }
        correlation.append({
            "code":        cc.lower(),
            "name":        country_names.get(cc, cc),
            "total_posts": total,
            "topics":      topics_out,
        })

    out = {
        "updated":               utcnow(),
        "total_with_country":    sum(country_counts.values()),
        "total_with_troll_flag": sum(troll_counts.values()),
        "countries": [
            {"code": cc.lower(), "name": country_names.get(cc, cc), "count": n}
            for cc, n in country_counts.most_common(40)
        ],
        "troll_flags": [
            {"code": bf.lower(), "name": troll_names.get(bf, bf), "count": n}
            for bf, n in troll_counts.most_common(30)
        ],
        "country_topic_matrix": correlation,
    }

    dest = os.path.join(SCRIPT_DIR, 'flags.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    print(f"[+] Flags: {len(country_counts)} countries, {len(troll_counts)} troll flags, "
          f"{len(correlation)} countries in correlation matrix")


def detect_timeheat(posts):
    """
    Build hourly and day-of-week posting heatmaps for live vs archive posts.
    Bots and coordinated campaigns show unnatural flat distributions or
    off-hours spikes inconsistent with the claimed origin country.
    Also computes per-topic hourly distributions so researchers can see
    what times specific narratives are pushed most aggressively.
    """
    live_hour    = [0] * 24
    archive_hour = [0] * 24
    live_dow     = [0] * 7   # 0=Mon … 6=Sun
    archive_dow  = [0] * 7
    topic_hours  = {k: [0]*24 for k in TOPIC_GROUPS}
    topic_dow    = {k: [0]*7  for k in TOPIC_GROUPS}

    for p in posts:
        ts = p.get('time')
        if not ts:
            continue
        try:
            dt   = datetime.utcfromtimestamp(int(ts))
            h    = dt.hour
            dow  = dt.weekday()
            if p.get('source') == 'live':
                live_hour[h]    += 1
                live_dow[dow]   += 1
            else:
                archive_hour[h] += 1
                archive_dow[dow] += 1
            text = (p.get('comment') or '').lower()
            for canonical, pattern in _GROUP_PATTERNS.items():
                if pattern.search(text):
                    topic_hours[canonical][h] += 1
                    topic_dow[canonical][dow]  += 1
        except Exception:
            pass

    # Only include topics that actually appeared in the corpus
    topic_hours = {k: v for k, v in topic_hours.items() if any(v)}
    topic_dow   = {k: v for k, v in topic_dow.items()   if any(v)}

    out = {
        "updated":          utcnow(),
        "live_by_hour":     live_hour,
        "archive_by_hour":  archive_hour,
        "live_by_dow":      live_dow,
        "archive_by_dow":   archive_dow,
        "topic_hours":      topic_hours,
        "topic_dow":        topic_dow,
    }
    dest = os.path.join(SCRIPT_DIR, 'timeheat.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    live_total = sum(live_hour)
    arch_total = sum(archive_hour)
    print(f"[+] Time heatmap: {live_total} live posts, {arch_total} archive posts")


def detect_tripcodes(posts):
    """
    Track non-anonymous posters (named users and tripcodes).
    On /pol/ most posts are anonymous; named/tripcoded posters are often
    influential or coordinating — track what topics they push.
    """
    identity_data = collections.defaultdict(lambda: {
        "posts": 0, "topics": collections.Counter(),
        "countries": collections.Counter(), "samples": [],
    })

    for p in posts:
        name = p.get('name', 'Anonymous').strip()
        trip = p.get('trip', '').strip()
        if name == 'Anonymous' and not trip:
            continue
        identity = f"{name}{trip}".strip()
        raw = p.get('com', '')
        if not raw:
            continue
        text_lower = raw.lower()
        d = identity_data[identity]
        d["posts"] += 1
        cc = p.get('country', '').upper()
        if cc:
            d["countries"][cc] += 1
        for canonical, pattern in _GROUP_PATTERNS.items():
            if pattern.search(text_lower):
                d["topics"][canonical] += 1
        if len(d["samples"]) < 3:
            cleaned = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', raw)).strip()
            d["samples"].append(cleaned[:200])

    posters = sorted(
        [
            {
                "identity":    ident,
                "post_count":  d["posts"],
                "top_topics":  d["topics"].most_common(5),
                "countries":   [{"code": cc.lower(), "count": n}
                                for cc, n in d["countries"].most_common(5)],
                "samples":     d["samples"],
            }
            for ident, d in identity_data.items()
            if d["posts"] >= 2
        ],
        key=lambda x: -x["post_count"]
    )[:100]

    out = {
        "updated":      utcnow(),
        "total_named":  len(identity_data),
        "posters":      posters,
    }
    dest = os.path.join(SCRIPT_DIR, 'tripcodes.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    print(f"[+] Tripcodes: {len(identity_data)} non-anonymous identities, "
          f"{len(posters)} with 2+ posts")


def detect_cooccurrence(posts):
    """
    Compute pairwise topic co-occurrence: which topics appear together in
    the same post most often. Reveals bundled narratives and framing campaigns
    (e.g. trans + groomer + children always appearing together).
    """
    topics    = list(TOPIC_GROUPS.keys())
    n_topics  = len(topics)
    # individual counts
    solo = {t: 0 for t in topics}
    # pairwise counts
    pair = collections.Counter()

    for p in posts:
        raw = p.get('com', '')
        if not raw:
            continue
        tl = raw.lower()
        present = [t for t in topics if _GROUP_PATTERNS[t].search(tl)]
        for t in present:
            solo[t] += 1
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                a, b = present[i], present[j]
                pair[(a, b)] += 1

    total_posts = max(sum(1 for p in posts if p.get('com')), 1)

    pairs_out = []
    for (a, b), count in pair.most_common(60):
        min_solo = min(solo[a], solo[b])
        if min_solo == 0:
            continue
        pairs_out.append({
            "topic_a":    a,
            "topic_b":    b,
            "count":      count,
            "pct_of_a":   round(count / max(solo[a], 1) * 100, 1),
            "pct_of_b":   round(count / max(solo[b], 1) * 100, 1),
            "lift":       round(count / max(solo[a], 1) / max(solo[b] / total_posts, 1e-9), 2),
        })

    out = {
        "updated":     utcnow(),
        "total_posts": total_posts,
        "solo_counts": solo,
        "pairs":       pairs_out,
    }
    dest = os.path.join(SCRIPT_DIR, 'cooccur.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    print(f"[+] Co-occurrence: {len(pairs_out)} topic pairs")


def build_thread_summaries(catalog_threads, posts):
    """
    Build per-thread summaries using catalog metadata + post counts.
    catalog_threads: list of thread objects from the catalog JSON.
    posts: full harvested post list (to count replies per thread).
    """
    reply_counts = collections.Counter(p['thread_id'] for p in posts if p.get('thread_id'))

    summaries = []
    for t in catalog_threads:
        tid    = t.get('no')
        sub    = t.get('sub', '')
        com    = clean_text(t.get('com', '')) if t.get('com') else ''
        # Truncate OP text for display
        preview = com[:300] + ('…' if len(com) > 300 else '')
        summaries.append({
            "thread_id":  tid,
            "subject":    sub,
            "preview":    preview,
            "replies":    reply_counts.get(tid, 0),
            "images":     t.get('images', 0),
            "time":       t.get('time', 0),
        })

    summaries.sort(key=lambda x: -x['replies'])
    out = {"updated": utcnow(), "threads": summaries}
    dest = os.path.join(SCRIPT_DIR, 'threads.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    print(f"[+] Threads: {len(summaries)} summaries written")

# ----------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS posts
                 (post_id INTEGER PRIMARY KEY, thread_id INTEGER, name TEXT,
                  time TEXT, comment TEXT, timestamp DATETIME)''')
    conn.commit()
    conn.close()

def utcnow():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

def save_and_analyze(posts):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    now = utcnow()
    for p in posts:
        c.execute("INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?)",
                  (p['no'], p.get('thread_id'), p.get('name', 'Anonymous'),
                   p.get('now', ''), p.get('com', ''), now))
    conn.commit()

    # Velocity = posts with actual content in this batch
    velocity = sum(1 for p in posts if p.get('com'))

    metrics_file = os.path.join(SCRIPT_DIR, 'metrics.json')
    tmp = metrics_file + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({"velocity": velocity, "updated": now}, f)
    os.replace(tmp, metrics_file)

    # Trend analysis on the raw current batch
    raw_comments = [p['com'] for p in posts if p.get('com')]
    update_trends(raw_comments)

    # AI pulse — read trends back from the file we just wrote
    try:
        with open(os.path.join(SCRIPT_DIR, 'trends.json')) as f:
            current_trends = json.load(f)
    except Exception:
        current_trends = []
    pulse_text = generate_pulse(current_trends, posts)
    pulse_dest = os.path.join(SCRIPT_DIR, 'pulse.json')
    pulse_tmp  = pulse_dest + '.tmp'
    with open(pulse_tmp, 'w') as f:
        json.dump({"updated": now, "text": pulse_text or ""}, f)
    os.replace(pulse_tmp, pulse_dest)

    # Analysis features
    detect_signals(posts)
    detect_jargon(posts)
    detect_conspiracies(posts)
    detect_memes(posts)
    detect_astroturf(posts)
    detect_trans(posts)
    detect_flags(posts)
    detect_timeheat(posts)
    detect_tripcodes(posts)
    detect_cooccurrence(posts)

    # Write ALL posts from the current batch (include flag fields for the viewer)
    all_posts = [
        {
            "post_id":         p['no'],
            "thread_id":       p.get('thread_id'),
            "name":            p.get('name', 'Anonymous'),
            "time":            p.get('now', ''),
            "comment":         p.get('com', ''),
            "country":         p.get('country', '').upper(),
            "country_name":    p.get('country_name', ''),
            "board_flag":      p.get('board_flag', '').upper(),
            "board_flag_name": p.get('board_flag_name', ''),
        }
        for p in posts if p.get('com')
    ]
    posts_file = os.path.join(SCRIPT_DIR, 'posts.json')
    tmp = posts_file + '.tmp'
    print(f"[+] Writing {posts_file} ({len(all_posts)} posts)")
    with open(tmp, 'w') as f:
        json.dump(all_posts, f)
    os.replace(tmp, posts_file)

    conn.close()


def save_thread_summaries(catalog_threads, posts):
    build_thread_summaries(catalog_threads, posts)

def clean_text(raw):
    text = re.sub(r'<[^>]+>', ' ', raw)
    text = html.unescape(text)           # decode &#039; → ' so contractions stay intact
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def update_trends(raw_comments):
    """
    Two-pass approach:
      Pass 1 — NLP on a sample to discover candidate topic names.
      Pass 2 — Raw string count of every candidate across ALL post text.
    This gives true mention counts, not NLP detection weights.
    """
    # Clean every post
    texts = []
    for raw in raw_comments:
        cleaned = clean_text(raw)
        if len(cleaned) > 5:
            texts.append(cleaned)

    if not texts:
        return

    # Full corpus as a single lowercase string for fast counting
    full_lower = ' '.join(texts).lower()

    # NORP = Nationalities/Religious/Political groups (Jews, Muslims, Democrats, etc.)
    # Kept intentionally for antisemitism/sentiment monitoring
    target_labels = {'GPE', 'PERSON', 'ORG', 'NORP', 'EVENT', 'FAC', 'PRODUCT', 'LOC', 'WORK_OF_ART'}

    if nlp:
        # Pass 1: NLP on up to 2000 posts to find candidate topic names
        candidates = set()
        sample = texts[:2000]
        for doc in nlp.pipe(sample, batch_size=64):
            for ent in doc.ents:
                if ent.label_ not in target_labels:
                    continue
                topic = re.sub(r'\s+', ' ', ent.text).strip()
                if len(topic) < 4:
                    continue
                normalized = topic.title()
                if normalized not in STOP_TOPICS:
                    candidates.add(normalized)

            for chunk in doc.noun_chunks:
                if chunk.root.pos_ in ('PRON', 'DET') or chunk.root.is_stop:
                    continue
                words = [t for t in chunk if not t.is_stop and not t.is_punct and len(t.text) > 2]
                if len(words) < 2:
                    continue
                phrase = ' '.join(t.text for t in words).strip().title()
                if len(phrase) > 5 and phrase not in STOP_TOPICS:
                    candidates.add(phrase)

        _write_trends(_count_topics(candidates, texts), len(texts))
        return

    # Fallback (no spaCy): regex-find capitalized words/phrases as candidates
    stop_lower = {w.lower() for w in STOP_TOPICS}
    candidates = set()
    for text in texts:
        for phrase in re.findall(r'\b(?:[A-Z][a-z]{2,}\s+){1,3}[A-Z][a-z]{2,}\b', text):
            if not any(w.lower() in stop_lower for w in phrase.split()):
                candidates.add(phrase.strip())
        for w in re.findall(r'\b[A-Z][a-z]{4,}\b', text):
            if w.lower() not in stop_lower:
                candidates.add(w)

    _write_trends(_count_topics(candidates, texts), len(texts), fallback=True)


def _count_topics(candidates, texts):
    """
    Count posts per topic with grouping:
    - Grouped topics (Jews, White, Indians, etc.) aggregate all variant spellings.
    - Ungrouped NLP candidates count normally.
    - Returns sorted list of (topic, count) pairs.
    """
    texts_lower = [t.lower() for t in texts]
    topic_counts = collections.Counter()

    # Grouped topics — count posts containing ANY variant (word-boundary matched)
    for canonical, pattern in _GROUP_PATTERNS.items():
        count = sum(1 for tl in texts_lower if pattern.search(tl))
        if count > 0:
            topic_counts[canonical] = count

    # Ungrouped candidates — skip anything that's already a group variant
    ungrouped = {c for c in candidates
                 if c.lower() not in _ALL_GROUP_VARIANTS
                 and c not in TOPIC_GROUPS
                 and c not in STOP_TOPICS}
    lower_map = {c: c.lower() for c in ungrouped}
    for tl in texts_lower:
        for topic, topic_lower in lower_map.items():
            if topic_lower in tl:
                topic_counts[topic] += 1

    return [(w, c) for w, c in topic_counts.most_common(100) if len(w) > 2][:25]

def _write_trends(trends_out, post_count, fallback=False):
    label = "fallback" if fallback else f"from {post_count} posts"
    print(f"[+] Trending topics ({len(trends_out)}, {label}):")
    for topic, count in trends_out[:10]:
        print(f"      {count:4d}  {topic}")
    trends_file = os.path.join(SCRIPT_DIR, 'trends.json')
    tmp_file = trends_file + '.tmp'
    with open(tmp_file, 'w') as f:
        json.dump(trends_out, f)
    os.replace(tmp_file, trends_file)  # atomic — viewer never sees a partial file

_GENERATED_FILES = (
    'posts.json', 'trends.json', 'metrics.json', 'progress.json',
    'signals.json', 'jargon.json', 'conspiracies.json',
    'threads.json', 'memes.json', 'astroturf.json', 'pulse.json',
    'trans.json', 'flags.json', 'timeheat.json', 'tripcodes.json', 'cooccur.json',
)

def _clear_generated_files():
    for fname in _GENERATED_FILES:
        try:
            os.remove(os.path.join(SCRIPT_DIR, fname))
        except FileNotFoundError:
            pass
    try:
        os.remove(DB_FILE)
    except FileNotFoundError:
        pass
    print("[*] Cleared all cached data")

def run_harvest(archive_count=ARCHIVE_THREADS):
    threading.Thread(target=_start_server, daemon=True).start()
    print(f"[*] ChudWatch viewer → http://localhost:{PORT}/viewer.html")
    print(f"[*] Harvest mode: live threads + {archive_count} archive threads")
    _clear_generated_files()
    init_db()

    def _fetch_thread(thread_id):
        try:
            r = requests.get(
                f"https://a.4cdn.org/{BOARD}/thread/{thread_id}.json", timeout=10)
            if not r.ok:
                return []
            posts = r.json().get('posts', [])
            for p in posts:
                p['thread_id'] = thread_id
            return posts
        except:
            return []

    while True:
        refresh_event.clear()
        try:
            write_progress("starting", 0, 0, 0, "Fetching /pol/ catalog...")
            print(f"[*] Starting harvest on /{BOARD}/...")

            # Live catalog
            url = f"https://a.4cdn.org/{BOARD}/catalog.json"
            r = requests.get(url, timeout=10)
            catalog_threads = []
            live_ids = []
            for page in r.json():
                for t in page.get('threads', []):
                    live_ids.append(t['no'])
                    catalog_threads.append(t)

            # Recent archived threads (most-recent first)
            archive_ids = []
            try:
                ar = requests.get(
                    f"https://a.4cdn.org/{BOARD}/archive.json", timeout=10)
                if ar.ok:
                    all_archived = ar.json()
                    # archive.json is oldest-first; reverse to get most recent first
                    archive_ids = list(reversed(all_archived))[:archive_count]
                    print(f"[*] Archive: pulling {len(archive_ids)} recent closed threads")
            except Exception as e:
                print(f"[!] Archive fetch failed: {e}")

            # Combine, deduplicate (live takes priority)
            live_set  = set(live_ids)
            thread_ids = live_ids + [tid for tid in archive_ids if tid not in live_set]
            total = len(thread_ids)
            print(f"[*] {len(live_ids)} live + {len(archive_ids)} archived = {total} threads total")
            write_progress("harvesting", 0, total, 0,
                           f"Fetching {total} threads ({FETCH_WORKERS} parallel)...")

            all_posts = []
            done = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
                futures = {ex.submit(_fetch_thread, tid): tid for tid in thread_ids}
                for fut in concurrent.futures.as_completed(futures):
                    if refresh_event.is_set():
                        ex.shutdown(wait=False, cancel_futures=True)
                        break
                    batch = fut.result()
                    # Tag each post so astroturf can compare live vs archive
                    for p in batch:
                        p['source'] = 'live' if p['thread_id'] in live_set else 'archive'
                    all_posts.extend(batch)
                    done += 1
                    if done % 20 == 0:
                        msg = f"Downloading threads: {done}/{total} — {len(all_posts):,} posts"
                        print(f"[ {done}/{total} | {len(all_posts):,} posts ]")
                        write_progress("harvesting", done, total, len(all_posts), msg)

            if refresh_event.is_set():
                continue

            print(f"[+] Harvested {len(all_posts):,} posts from {total} threads")
            write_progress("analyzing", total, total, len(all_posts),
                           f"Analyzing {len(all_posts):,} posts for trending topics...")
            save_and_analyze(all_posts)
            save_thread_summaries(catalog_threads, all_posts)

            write_progress("sleeping", total, total, len(all_posts),
                           f"Done — {len(all_posts):,} posts indexed. Next refresh in 5 min.")
            print("[*] Sleeping 5 minutes before next refresh...")
            refresh_event.wait(timeout=300)

        except Exception as e:
            print(f"[!] Error: {e}")
            write_progress("error", 0, 0, 0, f"Error: {e}. Retrying in 60s...")
            refresh_event.wait(timeout=60)

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description='ChudWatch — 4chan /pol/ monitor')
    mg = ap.add_mutually_exclusive_group()
    mg.add_argument('--minimal', action='store_true',
                    help='Live catalog only (~150 threads, fastest)')
    mg.add_argument('--medium',  action='store_true',
                    help='Live + 200 archive threads (~350 total)')
    mg.add_argument('--full',    action='store_true',
                    help='Live + 600 archive threads (~750 total, default)')
    args = ap.parse_args()

    if args.minimal:
        n_archive = 0
    elif args.medium:
        n_archive = 200
    else:
        n_archive = ARCHIVE_THREADS  # 600

    run_harvest(archive_count=n_archive)