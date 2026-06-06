import requests
import time
import sqlite3
import collections
import re
import json
import html
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

        # Pass 2: count posts containing the candidate using substring match —
        # same method as the viewer's search so the numbers always agree
        topic_counts = collections.Counter()
        lower_map = {topic: topic.lower() for topic in candidates}
        for text in texts:
            text_lower = text.lower()
            for topic, topic_lower in lower_map.items():
                if topic_lower in text_lower:
                    topic_counts[topic] += 1

        filtered = [(w, c) for w, c in topic_counts.most_common(100)
                    if w not in STOP_TOPICS and len(w) > 2]
        _write_trends(filtered[:25], len(texts))
        return

    # Fallback (no spaCy): regex-find capitalized words/phrases, then count posts
    stop_lower = {w.lower() for w in STOP_TOPICS}
    candidates = set()
    for text in texts:
        for phrase in re.findall(r'\b(?:[A-Z][a-z]{2,}\s+){1,3}[A-Z][a-z]{2,}\b', text):
            if not any(w.lower() in stop_lower for w in phrase.split()):
                candidates.add(phrase.strip())
        for w in re.findall(r'\b[A-Z][a-z]{4,}\b', text):
            if w.lower() not in stop_lower:
                candidates.add(w)

    topic_counts = collections.Counter()
    lower_map = {topic: topic.lower() for topic in candidates}
    for text in texts:
        text_lower = text.lower()
        for topic, topic_lower in lower_map.items():
            if topic_lower in text_lower:
                topic_counts[topic] += 1

    _write_trends(topic_counts.most_common(25), len(texts), fallback=True)

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
    print(f"[*] Starting ChudWatch harvest on /{BOARD}/...")
    init_db()
    
    while True:
        try:
            url = f"https://a.4cdn.org/{BOARD}/catalog.json"
            r = requests.get(url, timeout=10)
            threads = []
            for page in r.json():
                threads.extend([t['no'] for t in page.get('threads', [])])
            
            print(f"[*] Found {len(threads)} threads — fetching all posts...")
            all_posts = []
            for i, thread_id in enumerate(threads):
                try:
                    thread_url = f"https://a.4cdn.org/{BOARD}/thread/{thread_id}.json"
                    tr = requests.get(thread_url, timeout=10)
                    posts = tr.json().get('posts', [])
                    for post in posts:
                        post['thread_id'] = thread_id
                    all_posts.extend(posts)
                except:
                    pass
                if (i + 1) % 25 == 0:
                    print(f"[ {i+1}/{len(threads)} threads | {len(all_posts)} posts so far ]")

            print(f"[+] Harvested {len(all_posts)} posts from {len(threads)} threads")
            save_and_analyze(all_posts)
            print("[*] Sleeping 5 minutes before next refresh...")
            time.sleep(300)
        except Exception as e:
            print(f"[!] Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_harvest()