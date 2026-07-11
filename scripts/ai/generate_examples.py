#!/usr/bin/env python3
"""
AI Generate Examples for zh_dictionary (Supabase B)
=====================================================

Schema thực tế (zh_dictionary):
  id, word, pinyin, pinyin_plain, meaning, hv, level, examples (jsonb),
  tags, popularity, book_rank, movie_rank, traditional, word_chars,
  created_at, updated_at

Workflow:
  1. Query Supabase B: fetch tất cả words (paginate 1000/page)
  2. Filter trong Python: examples IS NULL OR examples = '[]'
  3. Chia theo worker_index/worker_total (hash theo id)
  4. Mỗi batch gọi Mistral AI generate N ví dụ
  5. UPDATE Supabase B ngay sau mỗi batch (để có thể resume nếu timeout)

Usage:
  python generate_examples.py --limit 50 --model mistral-small-latest \
    --examples-per-word 2 --batch-size 5 --delay 2.0 \
    --worker-index 1 --worker-total 10
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

LOG_FILE = None
FAILED_FILE = None
PROGRESS_FILE = None

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


def fetch_words_needing_examples(limit: int, worker_index: int, worker_total: int) -> list:
    """Fetch all words, filter empty examples in Python, partition by worker."""
    url = f"{SUPABASE_URL}/rest/v1/zh_dictionary"
    params = {
        'select': 'id,word,pinyin,pinyin_plain,meaning,hv,level,examples',
        'order': 'id.asc',
    }
    all_data = []
    offset = 0
    PAGE_SIZE = 1000
    while True:
        params['limit'] = PAGE_SIZE
        params['offset'] = offset
        log.info(f"Fetching page offset={offset}...")
        resp = requests.get(url, headers=SUPABASE_HEADERS, params=params, timeout=30)
        if resp.status_code != 200:
            log.error(f"HTTP {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        all_data.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    log.info(f"Total words in DB: {len(all_data)}")

    def is_empty_examples(ex):
        if ex is None:
            return True
        if isinstance(ex, list) and len(ex) == 0:
            return True
        if isinstance(ex, str) and ex.strip() in ('', '[]'):
            return True
        return False

    needing = [w for w in all_data if is_empty_examples(w.get('examples'))]
    log.info(f"Words needing examples: {len(needing)}/{len(all_data)}")

    if worker_total > 1:
        def word_hash(w):
            return (w.get('id', 0) % worker_total) + 1
        my_words = [w for w in needing if word_hash(w) == worker_index]
        log.info(f"Worker {worker_index}/{worker_total}: claimed {len(my_words)}/{len(needing)} words")
    else:
        my_words = needing

    if limit > 0:
        my_words = my_words[:limit]
        log.info(f"Limited to first {limit} words")

    for w in my_words:
        w.pop('examples', None)

    return my_words


def update_examples_batch(updates: list) -> int:
    """Update multiple words' examples. Returns count of successful updates."""
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
                log.warning(f"Update failed for id={u['id']} word={u.get('word','?')}: {resp.status_code}")
        except Exception as e:
            log.warning(f"Update exception for id={u['id']} word={u.get('word','?')}: {e}")
    return success


# ============================================================================
# MISTRAL AI
# ============================================================================

SYSTEM_PROMPT = """Bạn là chuyên gia biên soạn ví dụ tiếng Trung cho từ điển Hán-Việt.

Nhiệm vụ: Với mỗi từ được cho, viết N câu ví dụ tiếng Trung kèm dịch tiếng Việt.

Quy tắc:
1. Mỗi ví dụ là MỘT câu hoàn chỉnh, ngắn gọn (5-15 chữ Hán).
2. Phải DÙNG CHÍNH từ được cho trong câu.
3. Câu ví dụ phải tự nhiên, đúng ngữ pháp.
4. Dịch tiếng Việt chính xác, tự nhiên.
5. Độ khó phù hợp với level HSK.
6. Mỗi từ cần N ví dụ khác nhau (không trùng ý).
7. Trả về JSON: {"<word>": [{"jp": "câu tiếng Trung", "vn": "câu tiếng Việt"}, ...]}

Ví dụ cho "你好" (HSK1), 2 ví dụ:
{"你好": [{"jp": "你好，我叫小明。", "vn": "Xin chào, tôi tên là Minh."}, {"jp": "老师你好！", "vn": "Cô chào thầy ạ!"}]}"""


def build_user_prompt(batch: list, examples_per_word: int) -> str:
    items = []
    for r in batch:
        meaning = r.get('meaning', '')
        if len(meaning) > 80:
            meaning = meaning[:80] + '...'
        items.append(f"{r['word']}\t{r.get('pinyin', '')}\t{r.get('hv', '')}\t{meaning}\t{r.get('level', '')}")
    body = '\n'.join(items)
    return f"""Với mỗi dòng dưới đây (tab-separated): từ, pinyin, Hán-Việt, nghĩa, level HSK.
Hãy viết ĐÚNG {examples_per_word} câu ví dụ cho từng từ.

{body}

Trả về JSON: {{"<word>": [{{"jp": "câu tiếng Trung", "vn": "câu tiếng Việt"}}, ...]}}"""


def extract_json(content: str) -> dict:
    cleaned = content.strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'```\s*$', '', cleaned).strip()
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start < 0 or end < 0 or end < start:
        raise ValueError(f"No JSON: {cleaned[:200]}")
    return json.loads(cleaned[start:end+1])


def call_mistral(user_prompt: str, model: str = 'mistral-small-latest') -> str:
    api_key = os.environ.get('MISTRAL_API_KEY', '')
    if not api_key:
        raise RuntimeError("Missing MISTRAL_API_KEY")
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


def validate_examples(examples: list, word: str, expected_count: int) -> list:
    if not isinstance(examples, list):
        return []
    valid = []
    for ex in examples:
        if not isinstance(ex, dict):
            continue
        jp = (ex.get('jp') or '').strip()
        vn = (ex.get('vn') or '').strip()
        if len(jp) < 3 or len(vn) < 3:
            continue
        if word not in jp:
            continue
        valid.append({'jp': jp, 'vn': vn})
        if len(valid) >= expected_count:
            break
    return valid


# ============================================================================
# PROGRESS TRACKING (resume sau timeout)
# ============================================================================

def load_progress() -> set:
    """Load set of word IDs đã xử lý (để skip nếu resume)."""
    if PROGRESS_FILE and PROGRESS_FILE.exists():
        try:
            return set(json.load(open(PROGRESS_FILE, 'r', encoding='utf-8')))
        except Exception:
            pass
    return set()

def save_progress(done_ids: set):
    if PROGRESS_FILE:
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(done_ids), f)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='AI Generate Examples for zh_dictionary')
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--model', default='mistral-small-latest',
                        choices=['mistral-small-latest', 'mistral-large-latest', 'open-mistral-nemo', 'open-mixtral-8x7b'])
    parser.add_argument('--examples-per-word', type=int, default=2)
    parser.add_argument('--batch-size', type=int, default=5)
    parser.add_argument('--delay', type=float, default=2.0)
    parser.add_argument('--max-retries', type=int, default=3)
    parser.add_argument('--worker-index', type=int, default=1)
    parser.add_argument('--worker-total', type=int, default=1)
    args = parser.parse_args()

    global LOG_FILE, FAILED_FILE, PROGRESS_FILE
    if args.worker_total > 1:
        LOG_FILE = SCRIPT_DIR / f'examples_worker_{args.worker_index}.log'
        FAILED_FILE = SCRIPT_DIR / f'failed_words_worker_{args.worker_index}.json'
        PROGRESS_FILE = SCRIPT_DIR / f'progress_worker_{args.worker_index}.json'
        logging.getLogger().addHandler(
            logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
        )

    log.info(f"=== AI Examples Generator started at {datetime.now().isoformat()} ===")
    log.info(f"Provider: Mistral AI ({args.model}), Worker: {args.worker_index}/{args.worker_total}")
    log.info(f"Batch: {args.batch_size}, Delay: {args.delay}s, Limit: {args.limit}")
    log.info(f"Examples per word: {args.examples_per_word}")

    # Step 1: Fetch words
    words = fetch_words_needing_examples(args.limit, args.worker_index, args.worker_total)
    if not words:
        log.info("✅ Nothing to do.")
        return

    # Load progress (resume support)
    done_ids = load_progress()
    if done_ids:
        log.info(f"Resume: {len(done_ids)} words already processed, skipping...")
        words = [w for w in words if w['id'] not in done_ids]
        log.info(f"Remaining: {len(words)} words")

    if not words:
        log.info("✅ All words in partition already processed.")
        return

    log.info(f"Will process {len(words)} words")

    # Step 2: Process
    failed = []
    total_success = 0
    processed = 0

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

                updates = []
                for w in batch:
                    word = w['word']
                    if word in parsed:
                        examples = validate_examples(parsed[word], word, args.examples_per_word)
                        if examples:
                            updates.append({'id': w['id'], 'word': word, 'examples': examples})
                            done_ids.add(w['id'])
                        else:
                            failed.append({'id': w['id'], 'word': word, 'reason': 'no_valid_examples'})
                    else:
                        failed.append({'id': w['id'], 'word': word, 'reason': 'not_in_response'})

                if updates:
                    n = update_examples_batch(updates)
                    total_success += n
                    log.info(f"✓ Updated {n}/{len(updates)} words (total: {total_success})")

                # Save progress sau mỗi batch thành công
                save_progress(done_ids)
                processed += len(batch)
                break

            except Exception as e:
                msg = str(e)[:200]
                is_429 = '429' in msg or 'rate' in msg.lower()
                wait = 30 * attempt if is_429 else 5 * attempt
                log.warning(f"Batch {batch_num} attempt {attempt} failed: {msg}")
                if attempt < args.max_retries:
                    log.info(f"  Retry in {wait}s...")
                    time.sleep(wait)
                else:
                    log.error(f"Batch {batch_num} PERMANENT FAIL")
                    for w in batch:
                        failed.append({'id': w['id'], 'word': w['word'], 'reason': f'api_fail: {msg}'})

        # Delay giữa batches
        if i + args.batch_size < len(words):
            time.sleep(args.delay)

        # Log progress định kỳ
        if processed % 50 == 0:
            log.info(f"📊 Progress: {processed}/{len(words)} words, success: {total_success}")

    if failed:
        with open(FAILED_FILE, 'w', encoding='utf-8') as f:
            json.dump(failed, f, ensure_ascii=False, indent=2)

    log.info(f"\n{'=' * 60}")
    log.info(f"=== SUMMARY ===")
    log.info(f"Worker: {args.worker_index}/{args.worker_total}")
    log.info(f"Processed: {processed}, Success: {total_success}, Failed: {len(failed)}")
    log.info(f"=== Done at {datetime.now().isoformat()} ===")


if __name__ == '__main__':
    main()
