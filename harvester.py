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
            for fname in ('posts.json', 'trends.json', 'metrics.json', 'progress.json'):
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

    # Trend analysis on the raw current batch — real mention counts, not DB queries
    raw_comments = [p['com'] for p in posts if p.get('com')]
    update_trends(raw_comments)

    # Write ALL posts from the current batch so viewer search matches trend counts exactly
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
            for page in r.json():
                threads.extend([t['no'] for t in page.get('threads', [])])

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