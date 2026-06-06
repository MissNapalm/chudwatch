import requests
import time
import sqlite3
import collections
import re
import json
import html
import threading
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
    'Maybe', 'Probably', 'Literally', 'Basically', 'Essentially',
    # Generic adjectives that aren't topics
    'Good', 'Bad', 'Big', 'Small', 'New', 'Old', 'First', 'Last', 'Many', 'Much',
    'Same', 'Different', 'Other', 'More', 'Most', 'Less', 'Least', 'Very', 'Every',
    'Some', 'Any', 'Each', 'Both', 'Either', 'Neither', 'Another', 'Such',
    # Common verbs that slip through
    'Says', 'Said', 'Think', 'Know', 'Want', 'Make', 'Made', 'Need', 'Look',
    'Come', 'Goes', 'Going', 'Having', 'Being', 'Getting', 'Take', 'Give',
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
    'Like', 'Just', 'Even', 'Only', 'Never', 'Always', 'Often',
    # Generic nouns that aren't specific topics
    'World', 'People', 'Thing', 'Things', 'Time', 'Times', 'Love', 'Life',
    'Country', 'Government', 'Society', 'Problem', 'Problems', 'Point', 'Fact',
    'Today', 'Yesterday', 'Week', 'Year', 'Years', 'Money', 'Power',
}

# Collapsed topic groups: variant terms → single canonical label.
# A post is counted for the group if it contains ANY variant.
TOPIC_GROUPS = {
    'Indians':   ['india', 'indian', 'indians', 'jeet', 'jeets', 'pajeet', 'pajeets', 'poo in loo'],
    'Jews':      ['jew', 'jews', 'jewish', 'jewry', 'kike', 'kikes', 'heeb', 'heebs'],
    'White':     ['white', 'whites', 'whiteness', 'white people', 'white man', 'white men',
                  'white woman', 'white women', 'white race', 'aryan', 'aryans'],
    'Trans':     ['transgender', 'tran', 'trans', 'troon', 'trooned', 'tranny', 'trannies'],
    'Blacks':    ['black', 'blacks', 'nigger', 'niggers', 'nigga', 'niggas'],
    'Muslims':   ['muslim', 'muslims', 'islam', 'islamic', 'islamist', 'islamists'],
    'America':   ['america', 'american', 'americans', 'usa', 'u.s.'],
    'Russia':    ['russia', 'russian', 'russians'],
    'China':     ['china', 'chinese', 'chink', 'chinks'],
    'Israel':    ['israel', 'israeli', 'israelis', 'zionist', 'zionists', 'zionism'],
    'Ukraine':   ['ukraine', 'ukrainian', 'ukrainians'],
    'Latinos':   ['latino', 'latinos', 'latina', 'latinas', 'hispanic', 'hispanics',
                  'mexican', 'mexicans', 'spic', 'spics'],
}

# Flat set of all variants that belong to a group (for fast exclusion)
_ALL_GROUP_VARIANTS = {v for variants in TOPIC_GROUPS.values() for v in variants}

# --- HTTP server & refresh signal -------------------------------------------

PORT = 8080
refresh_event = threading.Event()

def write_progress(status, current, total, posts, message):
    pct = int(current / total * 100) if total > 0 else 0
    data = {"status": status, "current": current, "total": total,
            "posts": posts, "pct": pct, "message": message}
    tmp = os.path.join(SCRIPT_DIR, 'progress.json.tmp')
    dest = os.path.join(SCRIPT_DIR, 'progress.json')
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, dest)

class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def do_POST(self):
        if self.path == '/refresh':
            for fname in ('posts.json', 'trends.json', 'metrics.json', 'progress.json',
                          'signals.json', 'jargon.json', 'conspiracies.json',
                          'threads.json', 'memes.json'):
                try:
                    os.remove(os.path.join(SCRIPT_DIR, fname))
                except FileNotFoundError:
                    pass
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
# Patterns are organised by escalation tier. Each entry is:
#   (compiled_regex, category_label, tier)
# Tier 1 = ambient noise / plausible deniability language
# Tier 2 = passive mobilisation / wishing harm
# Tier 3 = direct indirect incitement (highest signal)
#
_RAW_PATTERNS = [
    # T1 — deniability / hedging
    (r"i'?m not (saying|suggesting|advocating|calling for|telling anyone)",    "deniability",          1),
    (r"not (a threat|threatening|inciting)",                                    "deniability",          1),
    (r"just (asking|saying|putting it out there|thinking out loud)",            "deniability",          1),
    (r"for (legal|obvious|educational) (reasons?|purposes?)",                   "deniability",          1),
    (r"hypothetically (speaking)?",                                             "hypothetical",         1),
    (r"what if (someone|somebody|a person|they) (were? to|would|could)",        "hypothetical",         1),
    (r"imagine if (someone|somebody|they|a person)",                            "hypothetical",         1),

    # T2 — passive mobilisation / wishing
    (r"someone (should|needs? to|ought to|has to|must|could easily)",           "passive mobilization", 2),
    (r"somebody (should|needs? to|ought to|has to|must)",                       "passive mobilization", 2),
    (r"if only (someone|somebody|a person) would",                              "passive mobilization", 2),
    (r"the right (person|people|individual|group) (could|would|should)",        "passive mobilization", 2),
    (r"wouldn'?t it be (funny|great|nice|a shame|sad|terrible|unfortunate)",    "wishing",              2),
    (r"would be a (real )?(shame|pity|tragedy|loss|waste)",                     "shame language",       2),
    (r"shame if (something|anything|an accident|something bad)",                "shame language",       2),
    (r"accidents? (can |do )?happen",                                           "shame language",       2),
    (r"i (wonder|bet|hope) (when|how long before|if someone)",                  "wishing",              2),
    (r"sooner or later (someone|they|he|she|people)",                           "inevitability",        2),
    (r"it'?s only a matter of time",                                            "inevitability",        2),
    (r"won'?t (last|be around) (long|much longer|forever)",                     "inevitability",        2),

    # T3 — indirect incitement (highest)
    (r"needs? to be (stopped|dealt with|removed|eliminated|handled|taken care of|silenced|put down)", "indirect incitement", 3),
    (r"deserves? (what'?s coming|everything coming|to (die|suffer|burn|hang))", "indirect incitement",  3),
    (r"(get|gets|getting) what (they|he|she).{0,20}deserv",                     "indirect incitement",  3),
    (r"someone (deal|deals|dealt|dealing) with (him|her|them|this|that)",        "indirect incitement",  3),
    (r"has (this |it )?coming( to (him|her|them))?",                            "indirect incitement",  3),
    (r"(do|did) (the world|everyone|society|us all) a (favor|favour)",          "indirect incitement",  3),
    (r"not going to end well for (him|her|them|these people)",                  "indirect incitement",  3),
]

SIGNAL_PATTERNS = [
    (re.compile(pat, re.IGNORECASE), label, tier)
    for pat, label, tier in _RAW_PATTERNS
]

# --- Radicalization jargon tiers ---------------------------------------------

JARGON_TIERS = {
    1: {
        "label": "Entry Level",
        "desc":  "Mainstream skepticism — the on-ramp",
        "terms": ["msm", "mainstream media", "fake news", "woke", "deep state",
                  "sheeple", "normie", "normies", "bluepilled", "blue pill",
                  "red pill", "redpill", "wake up", "they don't want you to know"],
    },
    2: {
        "label": "Intermediate",
        "desc":  "Active ideology — in-group vocabulary",
        "terms": ["redpilled", "based", "npc", "clown world", "globalist",
                  "race realist", "race realism", "ethnostate", "white nationalist",
                  "great replacement", "demographic replacement", "replacement theory",
                  "civic nationalist", "race traitor", "cuck", "soy", "soyjak",
                  "black pill", "blackpilled"],
    },
    3: {
        "label": "Deep End",
        "desc":  "Extremist / pre-violence terminology",
        "terms": ["zog", "day of the rope", "dotr", "rwds", "14 words", "1488",
                  "race war", "rahowa", "accelerate", "accelerationism",
                  "turner diaries", "saint tarrant", "saint breivik", "saint roof",
                  "final solution", "white genocide must", "it's time"],
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

    now = utcnow()
    out = {"updated": now, "count": len(results), "signals": results[:200]}
    dest = os.path.join(SCRIPT_DIR, 'signals.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    print(f"[+] Signals: {len(results)} posts matched ({sum(1 for r in results if r['max_tier']==3)} tier-3)")


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
        for md5, d in md5_info.items() if d['count'] >= 2
    ]
    results.sort(key=lambda x: -x['count'])

    out = {"updated": utcnow(), "memes": results[:60]}
    dest = os.path.join(SCRIPT_DIR, 'memes.json')
    tmp  = dest + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f)
    os.replace(tmp, dest)
    print(f"[+] Memes: {len(results)} recurring images")


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

    # Analysis features
    detect_signals(posts)
    detect_jargon(posts)
    detect_conspiracies(posts)
    detect_memes(posts)

    # Write ALL posts from the current batch
    all_posts = [
        {"post_id": p['no'], "thread_id": p.get('thread_id'), "name": p.get('name', 'Anonymous'),
         "time": p.get('now', ''), "comment": p.get('com', '')}
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

    # Grouped topics — count posts containing ANY variant
    for canonical, variants in TOPIC_GROUPS.items():
        count = sum(1 for tl in texts_lower if any(v in tl for v in variants))
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

def run_harvest():
    threading.Thread(target=_start_server, daemon=True).start()
    print(f"[*] ChudWatch viewer → http://localhost:{PORT}/viewer.html")
    init_db()

    while True:
        refresh_event.clear()
        try:
            write_progress("starting", 0, 0, 0, "Fetching /pol/ catalog...")
            print(f"[*] Starting harvest on /{BOARD}/...")

            url = f"https://a.4cdn.org/{BOARD}/catalog.json"
            r = requests.get(url, timeout=10)
            threads = []
            catalog_threads = []
            for page in r.json():
                for t in page.get('threads', []):
                    threads.append(t['no'])
                    catalog_threads.append(t)

            total = len(threads)
            print(f"[*] Found {total} threads — fetching all posts...")
            write_progress("harvesting", 0, total, 0,
                           f"Found {total} threads. Starting download...")

            all_posts = []
            for i, thread_id in enumerate(threads):
                if refresh_event.is_set():
                    break
                try:
                    thread_url = f"https://a.4cdn.org/{BOARD}/thread/{thread_id}.json"
                    tr = requests.get(thread_url, timeout=10)
                    posts = tr.json().get('posts', [])
                    for post in posts:
                        post['thread_id'] = thread_id
                    all_posts.extend(posts)
                except:
                    pass
                if (i + 1) % 10 == 0:
                    msg = f"Downloading threads: {i+1}/{total} — {len(all_posts):,} posts collected"
                    print(f"[ {i+1}/{total} threads | {len(all_posts):,} posts ]")
                    write_progress("harvesting", i + 1, total, len(all_posts), msg)

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
    run_harvest()