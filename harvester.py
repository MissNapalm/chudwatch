import requests
import time
import sqlite3
import re
import json
from datetime import datetime
import sys
import os
import html
import collections

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
    
    def clean_comment(text):
        if not text:
            return ""
        # Remove HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        # Remove HTML entities
        text = re.sub(r'&[a-zA-Z0-9#]+;', '', text)
        # Remove URLs
        text = re.sub(r'https?://\S+', '', text)
        # Remove quote refs
        text = re.sub(r'>>\d+', '', text)
        return text.strip()
    
    for p in posts:
        raw = p.get('com', '')
        cleaned_comment = clean_comment(raw)
        print(f"[DEBUG] Raw: {raw[:50]}... → Clean: {cleaned_comment[:50]}...")
        c.execute("INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?)", 
                  (p['no'], p.get('thread_id'), p.get('name', 'Anonymous'), 
                   p.get('now', ''), cleaned_comment, datetime.now().isoformat()))
    
    c.execute("SELECT count(*) FROM posts WHERE timestamp > datetime('now', '-10 minutes')")
    velocity = c.fetchone()[0]
    
    conn.commit()
    
    metrics_file = os.path.join(SCRIPT_DIR, 'metrics.json')
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
    c.execute("SELECT comment FROM posts ORDER BY post_id DESC LIMIT 5000")
    rows = c.fetchall()

    noun_counts = collections.Counter()
    
    stop_words = {
        'class', 'href', 'span', 'quote', 'quot', 'div', 'onclick', 'target', 'rel', 'com', 'http', 'https',
        'br', 'thing', 'way', 'time', 'year', 'day', 'man', 'woman', 'people', 'person', 'guy', 'shit', 'crap',
        'stuff', 'fuck', 'ass', 'hell', 'bullshit', 'point', 'fact', 'reason', 'case', 'bit',
        'lot', 'kind', 'type', 'part', 'work', 'life', 'hand', 'head', 'body', 'eye', 'face', 'word',
        'line', 'number', 'group', 'set', 'system', 'level', 'area', 'side', 'end', 'your', 'their', 'what',
        'will', 'them', 'because', 'more', 'even', 'than', 'only', 'like', 'just', 'know', 'don', 'get',
        'thing', 'say', 'make', 'use', 'take', 'come', 'see', 'go', 'have', 'does'
    }
    
    for row in rows:
        comment = row[0]
        if not comment:
            continue
        
        # Step 1: Decode HTML entities (&#039; → ', &quot; → ", etc)
        comment = html.unescape(comment)
        
        # Step 2: Remove ALL HTML tags
        comment = re.sub(r'<[^>]+>', '', comment)
        
        # Step 3: Remove URLs
        comment = re.sub(r'https?://\S+', '', comment)
        
        # Step 4: Extract words (letters only, 3+ chars)
        words = re.findall(r'\b[a-z]{3,}\b', comment.lower())
        
        # Step 5: Count non-stop words
        for word in words:
            if word not in stop_words:
                noun_counts[word] += 1
    
    trends_out = noun_counts.most_common(20)
    print(f"[+] Trends: {[t[0] for t in trends_out]}")
    
    trends_file = os.path.join(SCRIPT_DIR, 'trends.json')
    with open(trends_file, 'w') as f:
        json.dump(trends_out, f)

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