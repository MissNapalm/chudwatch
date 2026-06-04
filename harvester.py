import requests
import time
import sqlite3
import collections
import re
import json
from datetime import datetime
import sys
import os

# Suppress hashlib warnings from pyenv
import warnings
warnings.filterwarnings("ignore")
os.environ['PYTHONWARNINGS'] = 'ignore'

import spacy

# CONFIG
BOARD = 'pol'
DB_FILE = 'chudwatch.db'

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    nlp = spacy.load("en_core_web_sm")
except Exception:
    nlp = None

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS posts 
                 (post_id INTEGER PRIMARY KEY, thread_id INTEGER, name TEXT, 
                  time TEXT, comment TEXT, timestamp DATETIME)''')
    conn.commit()
    conn.close()

def save_and_analyze(posts):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    for p in posts:
        c.execute("INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?)", 
                  (p['no'], p.get('thread_id'), p.get('name', 'Anonymous'), 
                   p.get('now', ''), p.get('com', ''), datetime.now().isoformat()))
    
    c.execute("SELECT count(*) FROM posts WHERE timestamp > datetime('now', '-10 minutes')")
    velocity = c.fetchone()[0]
    
    conn.commit()
    
    metrics_file = os.path.join(SCRIPT_DIR, 'metrics.json')
    print(f"[+] Writing {metrics_file}")
    with open(metrics_file, 'w') as f:
        json.dump({"velocity": velocity}, f)
    
    update_trends(conn)
    
    c.execute("SELECT post_id, thread_id, name, time, comment FROM posts ORDER BY post_id DESC LIMIT 1000")
    all_posts = [{"post_id": r[0], "thread_id": r[1], "name": r[2], "time": r[3], "comment": r[4]} for r in c.fetchall()]
    
    posts_file = os.path.join(SCRIPT_DIR, 'posts.json')
    print(f"[+] Writing {posts_file} ({len(all_posts)} posts)")
    with open(posts_file, 'w') as f:
        json.dump(all_posts, f)
    
    conn.close()

def update_trends(conn):
    c = conn.cursor()
    c.execute("SELECT comment FROM posts ORDER BY post_id DESC LIMIT 3000")
    rows = c.fetchall()

    if nlp:
        entity_counts = collections.Counter()
        noun_counts = collections.Counter()
        target_labels = {'GPE', 'PERSON', 'ORG', 'NORP'}

        for row in rows:
            text = row[0]
            if not text:
                continue
            # Strip ALL HTML tags and entities - FIXED REGEX
            cleaned = re.sub(r'<[^>]+>', ' ', text)
            cleaned = re.sub(r'&[a-zA-Z0-9#]+;', ' ', cleaned)  # Fixed: uppercase + numbers
            cleaned = re.sub(r'https?://\S+', ' ', cleaned)
            
            doc = nlp(cleaned)
            
            # Extract named entities
            for ent in doc.ents:
                if ent.label_ in target_labels:
                    clean_ent = ent.text.strip().title()
                    if len(clean_ent) > 2:
                        entity_counts[clean_ent] += 1
            
            # Extract proper nouns (PROPN) only
            for token in doc:
                if token.pos_ == 'PROPN' and len(token.text) > 3 and not token.is_stop:
                    clean_token = token.text.strip().title()
                    noun_counts[clean_token] += 1

        # Combine and filter out HTML garbage
        combined = entity_counts + noun_counts
        html_garbage = {'Class', 'Href', 'Quotelink', 'Span', 'Quote', 'Quot', 'Br', 'Div', 'Http', 'Https'}
        filtered = [(word, count) for word, count in combined.most_common(50) if word not in html_garbage]
        trends_out = filtered[:25]
        
        print(f"[+] Writing trends.json ({len(trends_out)} topics)")
        trends_file = os.path.join(SCRIPT_DIR, 'trends.json')
        with open(trends_file, 'w') as f:
            json.dump(trends_out, f)
        return

    # Fallback: extract capitalized words
    words = []
    for row in rows:
        if row[0]:
            cleaned = re.sub(r'<[^>]+>', ' ', row[0])
            cleaned = re.sub(r'&[a-zA-Z0-9#]+;', ' ', cleaned)  # Fixed: uppercase + numbers
            words.extend(re.findall(r'\b[A-Z][a-z]{3,}\b', cleaned))

    html_garbage = {'class', 'href', 'quotelink', 'span', 'quote', 'quot', 'br', 'div', 'http', 'https'}
    counts = [(w, c) for w, c in collections.Counter(words).most_common(50) if w.lower() not in html_garbage]
    
    trends_file = os.path.join(SCRIPT_DIR, 'trends.json')
    print(f"[+] Writing trends.json ({len(counts[:25])} topics)")
    with open(trends_file, 'w') as f:
        json.dump(counts[:25], f)

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
            
            all_posts = []
            for i, thread_id in enumerate(threads[:50]):
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
                    print(f"[ Progress: {i+1}/{len(threads[:50])} threads downloaded ]")
            
            print(f"[+] Harvested {len(all_posts)} posts")
            save_and_analyze(all_posts)
            print("[*] Sleeping for 3 minutes before refresh...")
            time.sleep(180)
        except Exception as e:
            print(f"[!] Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_harvest()