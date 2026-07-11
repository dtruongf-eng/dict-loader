#!/usr/bin/env python3
"""
AI Generate Examples for zh_dictionary (Supabase B)
=====================================================

Workflow:
  1. Query Supabase B: SELECT words WHERE examples IS NULL OR examples = '[]'
  2. For each batch of N words, call Mistral AI to generate Vietnamese example sentences
  3. UPDATE Supabase B with new examples (JSONB array: [{"jp": "...", "vn": "..."}])

Usage:
  python generate_examples.py --limit 50 --model mistral-small-latest \
    --examples-per-word 3 --batch-size 5 --delay 2.0 \
    --worker-index 1 --worker-total 10

Environment variables (must be set):
  SUPABASE_DICT_URL          Supabase B project URL
  SUPABASE_DICT_SERVICE_KEY  Service role key (bypass RLS)
  MISTRAL_API_KEY            API key from https://console.mistral.ai/api-keys/

Output:
  - Updates Supabase B directly
  - Writes log to scripts/ai/examples_worker_N.log
  - Writes failed words to scripts/ai/failed_words_worker_N.json
"""

import argparse
import json
import os
import re
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

import requests

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_DIR = Path(__file__).parent

# Setup logging (file handler sẽ được setup sau khi biết worker_index)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

LOG_FILE = None
FAILED_FILE = None

# ============================================================================
# SUPABASE B
# ============================================================================

SUPABASE_URL = os.environ.get('SUPABASE_DICT_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_DICT_SERVICE_KEY', '')

if not SUPABASE_URL or not SUPABASE_KEY:
    log.error("Missing SUPABASE_DICT_URL or SUPABASE_DICT_SERVICE_KEY environment variable")
    sys.exit(1)

SUPABASE_HEADERS = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'return=minimal',
}


def fetch_words_needing_examples(limit: int, worker_index: int = 1, worker_total: int = 1) -> list:
    """
    Fetch words from zh_dictionary where examples is NULL or empty array.
    Phân bổ theo worker_index/worker_total để 10 workers không trùng nhau.
    """
    url = f"{SUPABASE_URL}/rest/v1/zh_dictionary"
    # Filter: examples IS NULL OR examples = '[]' (empty array)
    # PostgREST: ?or=(examples.is.null,examples.eq.[])
    params = {
        'select': 'id,word,reading,pinyin_no_tones,meaning,hv,level',
        'or': '(examples.is.null,examples.eq.[])',
        'order': 'popularity.desc,word.asc',
    }
    # Paginate vì Supabase default limit 1000
    all_data = []
    offset = 0
    PAGE_SIZE = 1000
    while True:
        params['limit'] = PAGE_SIZE
        params['offset'] = offset
        log.info(f"Fetching page offset={offset}...")
        resp = requests.get(url, headers=SUPABASE_HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        all_data.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    
    log.info(f"Total words needing examples in DB: {len(all_data)}")
    
    # Filter theo worker (hash theo word để phân bổ đều)
    if worker_total > 1:
        def word_hash(w):
            # Hash theo id (BIGINT) — stable và đều
            return (w.get('id', 0) % worker_total) + 1
        my_words = [w for w in all_data if word_hash(w) == worker_index]
        log.info(f"Worker {worker_index}/{worker_total}: claimed {len(my_words)}/{len(all_data)} words")
    else:
        my_words = all_data
    
    if limit > 0:
        my_words = my_words[:limit]
        log.info(f"Limited to first {limit} words")
    
    return my_words


def update_examples_batch(updates: list) -> int:
    """Update multiple words' examples in Supabase. Returns count of successful updates."""
    if not updates:
        return 0
    
    success = 0
    for u in updates:
        try:
            url = f"{SUPABASE_URL}/rest/v1/zh_dictionary"
            params = {'id': f'eq.{u["id"]}'}
            payload = {'examples': u['examples']}
            resp = requests.patch(url, headers=SUPABASE_HEADERS, params=params, json=payload, timeout=15)
            if resp.status_code == 204:
                success += 1
            else:
                log.warning(f"Update failed for id={u['id']} word={u.get('word','?')}: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            log.warning(f"Update exception for id={u['id']} word={u.get('word','?')}: {e}")
    
    return success


# ============================================================================
# MISTRAL AI
# ============================================================================

SYSTEM_PROMPT = """Bạn là chuyên gia biên soạn ví dụ tiếng Trung cho từ điển Hán-Việt, dành cho người Việt học tiếng Trung.

Nhiệm vụ: Với mỗi từ được cho, viết N câu ví dụ tiếng Trung kèm dịch tiếng Việt.

Quy tắc:
1. Mỗi ví dụ là MỘT câu hoàn chỉnh, ngắn gọn (5-15 chữ Hán).
2. Phải DÙNG CHÍNH từ được cho trong câu.
3. Câu ví dụ phải tự nhiên, đúng ngữ pháp, hữu ích cho người học.
4. Dịch tiếng Việt phải chính xác, tự nhiên, giữ nguyên ý nghĩa gốc.
5. Độ khó phù hợp với level HSK của từ (HSK1-2: câu đơn giản, HSK5-6: câu phức tạp hơn).
6. Mỗi từ cần N ví dụ khác nhau (không trùng ý).
7. Trả về JSON object: {"<word>": [{"jp": "câu tiếng Trung", "vn": "câu tiếng Việt"}, ...]}

Ví dụ cho từ "你好" (HSK1, nghĩa: xin chào), 2 ví dụ:
{"你好": [{"jp": "你好，我叫小明。", "vn": "Xin chào, tôi tên là Minh."}, {"jp": "老师你好！", "vn": "Cô chào thầy ạ!"}]}

Ví dụ cho từ "学习" (HSK1, nghĩa: học tập), 2 ví dụ:
{"学习": [{"jp": "我每天学习汉语。", "vn": "Tôi học tiếng Trung mỗi ngày."}, {"jp": "他学习很努力。", "vn": "Anh ấy học tập rất chăm chỉ."}]}"""


def build_user_prompt(batch: list, examples_per_word: int) -> str:
    items = []
    for r in batch:
        meaning = r.get('meaning', '')
        if len(meaning) > 80:
            meaning = meaning[:80] + '...'
        items.append(f"{r['word']}\t{r.get('reading', '')}\t{r.get('hv', '')}\t{meaning}\t{r.get('level', '')}")
    body = '\n'.join(items)
    return f"""Với mỗi dòng dưới đây (tab-separated): từ, pinyin, Hán-Việt, nghĩa, level HSK.
Hãy viết ĐÚNG {examples_per_word} câu ví dụ cho từng từ.

{body}

Trả về JSON object: {{"<word>": [{{"jp": "câu tiếng Trung", "vn": "câu tiếng Việt"}}, ...]}}"""


def extract_json(content: str) -> dict:
    cleaned = content.strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'```\s*$', '', cleaned).strip()
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start < 0 or end < 0 or end < start:
        raise ValueError(f"No JSON found in: {cleaned[:200]}")
    return json.loads(cleaned[start:end+1])


def call_mistral(user_prompt: str, model: str = 'mistral-small-latest') -> str:
    api_key = os.environ.get('MISTRAL_API_KEY', '')
    if not api_key:
        raise RuntimeError("Missing MISTRAL_API_KEY environment variable")
    
    resp = requests.post(
        'https://api.mistral.ai/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': model,
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': user_prompt},
            ],
            'temperature': 0.7,
            'response_format': {'type': 'json_object'},
        },
        timeout=90,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Mistral API {resp.status_code}: {resp.text[:200]}")
    return resp.json()['choices'][0]['message']['content']


# ============================================================================
# VALIDATION
# ============================================================================

def validate_examples(examples: list, word: str, expected_count: int) -> list:
    """Validate examples from AI. Returns cleaned list or [] if invalid."""
    if not isinstance(examples, list):
        return []
    
    valid = []
    for ex in examples:
        if not isinstance(ex, dict):
            continue
        jp = (ex.get('jp') or '').strip()
        vn = (ex.get('vn') or '').strip()
        # Câu phải có ít nhất 3 ký tự
        if len(jp) < 3 or len(vn) < 3:
            continue
        # Câu tiếng Trung phải chứa từ
        if word not in jp:
            continue
        valid.append({'jp': jp, 'vn': vn})
        if len(valid) >= expected_count:
            break
    
    return valid


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='AI Generate Examples for zh_dictionary (Supabase B)')
    parser.add_argument('--limit', type=int, default=50, help='Max words to process (0 = all)')
    parser.add_argument('--model', default='mistral-small-latest',
                        choices=['mistral-small-latest', 'mistral-large-latest', 'open-mistral-nemo', 'open-mixtral-8x7b'])
    parser.add_argument('--examples-per-word', type=int, default=3, help='Number of examples per word')
    parser.add_argument('--batch-size', type=int, default=5, help='Words per API call')
    parser.add_argument('--delay', type=float, default=2.0, help='Delay between batches (seconds)')
    parser.add_argument('--max-retries', type=int, default=3)
    parser.add_argument('--worker-index', type=int, default=1, help='Worker index (1-based)')
    parser.add_argument('--worker-total', type=int, default=1, help='Total number of workers')
    args = parser.parse_args()
    
    # Setup log file riêng cho worker
    global LOG_FILE, FAILED_FILE
    if args.worker_total > 1:
        LOG_FILE = SCRIPT_DIR / f'examples_worker_{args.worker_index}.log'
        FAILED_FILE = SCRIPT_DIR / f'failed_words_worker_{args.worker_index}.json'
        logging.getLogger().addHandler(
            logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
        )
    
    log.info(f"=== AI Examples Generator started at {datetime.now().isoformat()} ===")
    log.info(f"Provider: Mistral AI, Model: {args.model}")
    log.info(f"Worker: {args.worker_index}/{args.worker_total}")
    log.info(f"Batch size: {args.batch_size}, Delay: {args.delay}s, Limit: {args.limit}")
    log.info(f"Examples per word: {args.examples_per_word}")
    
    # Step 1: Fetch words needing examples
    words = fetch_words_needing_examples(args.limit, args.worker_index, args.worker_total)
    if not words:
        log.info("✅ All words (in my partition) already have examples. Nothing to do.")
        return
    
    log.info(f"This worker will process {len(words)} words")
    
    # Step 2: Process in batches
    failed = []
    total_success = 0
    
    for i in range(0, len(words), args.batch_size):
        batch = words[i:i + args.batch_size]
        batch_num = i // args.batch_size + 1
        total_batches = (len(words) + args.batch_size - 1) // args.batch_size
        log.info(f"\n--- Batch {batch_num}/{total_batches} ---")
        log.info(f"Words: {[w['word'] for w in batch]}")
        
        user_prompt = build_user_prompt(batch, args.examples_per_word)
        
        for attempt in range(1, args.max_retries + 1):
            try:
                content = call_mistral(user_prompt, model=args.model)
                parsed = extract_json(content)
                log.info(f"API returned examples for {len(parsed)} words")
                
                # Validate
                updates = []
                for w in batch:
                    word = w['word']
                    if word in parsed:
                        examples = validate_examples(parsed[word], word, args.examples_per_word)
                        if examples:
                            updates.append({
                                'id': w['id'],
                                'word': word,
                                'examples': examples,
                            })
                        else:
                            log.warning(f"  [{word}] no valid examples after validation")
                            failed.append({'id': w['id'], 'word': word, 'reason': 'no_valid_examples', **w})
                    else:
                        log.warning(f"  [{word}] not in API response")
                        failed.append({'id': w['id'], 'word': word, 'reason': 'not_in_response', **w})
                
                # Update Supabase
                if updates:
                    n = update_examples_batch(updates)
                    total_success += n
                    log.info(f"✓ Updated {n}/{len(updates)} words in Supabase")
                
                break  # success, move to next batch
                
            except Exception as e:
                msg = str(e)[:200]
                is_rate_limit = '429' in msg or 'rate' in msg.lower()
                wait = 30 * attempt if is_rate_limit else 5 * attempt
                log.warning(f"Batch {batch_num} attempt {attempt} failed: {msg}")
                if attempt < args.max_retries:
                    log.info(f"  Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    log.error(f"Batch {batch_num} PERMANENT FAIL")
                    for w in batch:
                        failed.append({'id': w['id'], 'word': w['word'], 'reason': f'api_fail: {msg}', **w})
        
        # Delay between batches
        if i + args.batch_size < len(words):
            time.sleep(args.delay)
    
    # Save failed list
    if failed:
        with open(FAILED_FILE, 'w', encoding='utf-8') as f:
            json.dump(failed, f, ensure_ascii=False, indent=2)
        log.info(f"\nFailed words saved to {FAILED_FILE}")
    
    # Summary
    log.info(f"\n{'=' * 60}")
    log.info(f"=== SUMMARY ===")
    log.info(f"Provider: Mistral AI ({args.model})")
    log.info(f"Worker: {args.worker_index}/{args.worker_total}")
    log.info(f"Total words processed: {len(words)}")
    log.info(f"Successfully updated: {total_success}")
    log.info(f"Failed: {len(failed)}")
    log.info(f"=== Done at {datetime.now().isoformat()} ===")


if __name__ == '__main__':
    main()
