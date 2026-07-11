#!/usr/bin/env python3
"""
AI Generate Mnemonics for zh_kanji_builder (Supabase A)
=========================================================

Workflow:
  1. Query Supabase A: SELECT chars WHERE mnemonics IS NULL OR mnemonics = ''
  2. For each batch of N chars, call Mistral AI to generate Vietnamese story
  3. UPDATE Supabase A with new mnemonics

Usage:
  python generate_mnemonics.py --limit 50 --model mistral-small-latest --batch-size 5 --delay 2.0

Environment variables (must be set):
  SUPABASE_URL           Supabase A project URL
  SUPABASE_SERVICE_KEY   Service role key (bypass RLS)
  MISTRAL_API_KEY        API key từ https://console.mistral.ai/api-keys/

Output:
  - Updates Supabase A directly
  - Writes log to scripts/ai/mnemonics.log
  - Writes failed chars to scripts/ai/failed_chars.json
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
LOG_FILE = SCRIPT_DIR / 'mnemonics.log'
FAILED_FILE = SCRIPT_DIR / 'failed_chars.json'

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ============================================================================
# SUPABASE
# ============================================================================

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')

if not SUPABASE_URL or not SUPABASE_KEY:
    log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY environment variable")
    sys.exit(1)

SUPABASE_HEADERS = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'return=minimal',
}


def fetch_chars_needing_mnemonics(limit: int, worker_index: int = 1, worker_total: int = 1) -> list:
    """
    Fetch chars from zh_kanji_builder where mnemonics is NULL or empty.
    
    Nếu worker_total > 1: chia chữ cho các worker bằng modulo (hash theo hanzi).
    Mỗi worker chỉ xử lý 1/worker_total số chữ (theo hash của hanzi).
    Điều này đảm bảo 10 worker không bao giờ lấy cùng 1 chữ.
    """
    url = f"{SUPABASE_URL}/rest/v1/zh_kanji_builder"
    # Lấy tất cả chars thiếu mnemonics (không limit ở DB, sẽ filter ở Python)
    params = {
        'select': 'hanzi,pinyin,hv,meaning,level,components,stroke_count,structure_type',
        'or': '(mnemonics.is.null,mnemonics.eq.)',
        'order': 'level.asc,hanzi.asc',
    }
    # Note: Supabase REST API có limit mặc định 1000, cần paginate nếu nhiều hơn
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
    
    log.info(f"Total chars needing mnemonics in DB: {len(all_data)}")
    
    # Filter theo worker (dùng modulo của ord(hanzi[0]) để phân bổ đều)
    if worker_total > 1:
        # Dùng hash của toàn bộ hanzi string để phân bổ đều cho chars nhiều byte
        def hanzi_hash(h):
            return sum(ord(c) for c in h) % worker_total + 1  # 1..worker_total
        my_chars = [c for c in all_data if hanzi_hash(c['hanzi']) == worker_index]
        log.info(f"Worker {worker_index}/{worker_total}: claimed {len(my_chars)}/{len(all_data)} chars")
    else:
        my_chars = all_data
    
    # Apply limit (sau khi đã filter)
    if limit > 0:
        my_chars = my_chars[:limit]
        log.info(f"Limited to first {limit} chars")
    
    return my_chars


def update_mnemonics_batch(updates: list) -> int:
    """Update multiple chars' mnemonics in Supabase. Returns count of successful updates."""
    if not updates:
        return 0
    
    success = 0
    for u in updates:
        try:
            url = f"{SUPABASE_URL}/rest/v1/zh_kanji_builder"
            params = {'hanzi': f'eq.{u["hanzi"]}'}
            payload = {'mnemonics': u['mnemonics']}
            resp = requests.patch(url, headers=SUPABASE_HEADERS, params=params, json=payload, timeout=15)
            if resp.status_code == 204:
                success += 1
            else:
                log.warning(f"Update failed for {u['hanzi']}: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            log.warning(f"Update exception for {u['hanzi']}: {e}")
    
    return success


# ============================================================================
# MISTRAL AI
# ============================================================================

SYSTEM_PROMPT = """Bạn là chuyên gia biên soạn câu chuyện ghi nhớ (mnemonics) cho chữ Hán, dành cho người Việt học tiếng Trung.

Nhiệm vụ: Với mỗi chữ Hán được cho, viết MỘT câu chuyện ngắn (80-200 chữ) giúp người học ghi nhớ chữ đó.

Quy tắc:
1. Câu chuyện phải DỰA VÀO HÌNH DÁNG chữ Hán hoặc cấu trúc thành phần (components) của chữ.
2. Phải NHẮC LẠI nghĩa tiếng Việt và/hoặc âm Hán-Việt trong câu chuyện.
3. Phải chèn chính chữ Hán đó vào câu chuyện (trong ngoặc đơn).
4. Câu chuyện phải dễ nhớ, gợi hình, có thể hơi hài hước hoặc nhân hóa.
5. Dùng tiếng Việt, có thể kèm Hán-Việt trong ngoặc nhọn {}.
6. KHÔNG dùng dấu "/" — viết thành câu văn hoàn chỉnh.
7. Trả về JSON object: {"<hanzi>": "<câu chuyện>"}
8. KHÔNG markdown, không lời dẫn.

Ví dụ cho chữ 人 (ren2, nhân, nghĩa: con người):
"Hình dáng đơn giản của một người (人) đang đứng thẳng, với hai chân vững chãi, là biểu tượng mạnh mẽ và dễ nhận biết nhất cho con người (人), đại diện cho mỗi cá nhân trong xã hội chúng ta."

Ví dụ cho chữ 大 (da4, đại, nghĩa: to lớn):
"Một người (大) dang rộng vòng tay và chân, tạo thành một hình ảnh khổng lồ, làm cho bản thân trông thật lớn (大) và hùng vĩ, biểu trưng cho sự vĩ đại của mọi thứ."

Ví dụ cho chữ 休 (xiu1, hưu, nghĩa: nghỉ ngơi):
"Một người (亻) đứng dựa vào gốc cây (木), tạo thành chữ休, gợi hình ảnh người nông dân mệt mỏi sau giờ lao động, tìm bóng mát để nghỉ ngơi (休) thanh thản.\""""


def build_user_prompt(batch: list) -> str:
    items = []
    for r in batch:
        comps = r.get('components', '')
        if isinstance(comps, list):
            comps = ', '.join(comps)
        if len(comps) > 80:
            comps = comps[:80] + '...'
        meaning = r.get('meaning', '')
        if len(meaning) > 150:
            meaning = meaning[:150] + '...'
        items.append(f"{r['hanzi']}\t{r.get('pinyin', '')}\t{r.get('hv', '')}\t{comps}\t{meaning}")
    body = '\n'.join(items)
    return f"""Với mỗi dòng dưới đây (tab-separated): chữ Hán, pinyin, Hán-Việt, components, nghĩa.
Hãy viết một câu chuyện ghi nhớ (80-200 chữ) cho từng chữ.

{body}

Trả về JSON object: {{"<hanzi>": "<câu chuyện>"}}"""


def extract_json(content: str) -> dict:
    cleaned = content.strip()
    # Strip markdown fences
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'```\s*$', '', cleaned).strip()
    # Find first { and last }
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start < 0 or end < 0 or end < start:
        raise ValueError(f"No JSON found in: {cleaned[:200]}")
    return json.loads(cleaned[start:end+1])


def call_mistral(user_prompt: str, model: str = 'mistral-small-latest') -> str:
    """
    Call Mistral AI chat completions API.
    API docs: https://docs.mistral.ai/api/#tag/chat
    """
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
            'temperature': 0.6,
            # Mistral supports JSON mode for reliable structured output
            'response_format': {'type': 'json_object'},
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Mistral API {resp.status_code}: {resp.text[:200]}")
    return resp.json()['choices'][0]['message']['content']


# ============================================================================
# MAIN
# ============================================================================

def is_good_story(story: str) -> bool:
    if not story or not isinstance(story, str):
        return False
    s = story.strip()
    if len(s) < 60:
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description='AI Generate Mnemonics for zh_kanji_builder (Mistral AI)')
    parser.add_argument('--limit', type=int, default=50, help='Max chars to process (0 = all)')
    parser.add_argument('--model', default='mistral-small-latest',
                        choices=['mistral-small-latest', 'mistral-large-latest', 'open-mistral-nemo', 'open-mixtral-8x7b'],
                        help='Mistral model to use')
    parser.add_argument('--batch-size', type=int, default=5, help='Chars per API call')
    parser.add_argument('--delay', type=float, default=2.0, help='Delay between batches (seconds)')
    parser.add_argument('--max-retries', type=int, default=3)
    parser.add_argument('--worker-index', type=int, default=1, help='Worker index (1-based) for parallel execution')
    parser.add_argument('--worker-total', type=int, default=1, help='Total number of workers')
    args = parser.parse_args()
    
    # Override log file path để mỗi worker có log riêng (nếu worker_total > 1)
    global LOG_FILE, FAILED_FILE
    if args.worker_total > 1:
        LOG_FILE = SCRIPT_DIR / f'mnemonics_worker_{args.worker_index}.log'
        FAILED_FILE = SCRIPT_DIR / f'failed_chars_worker_{args.worker_index}.json'
        # Re-setup file handler
        for handler in logging.getLogger().handlers[:]:
            if isinstance(handler, logging.FileHandler):
                logging.getLogger().removeHandler(handler)
                handler.close()
        logging.getLogger().addHandler(
            logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
        )
    
    log.info(f"=== AI Mnemonics Generator started at {datetime.now().isoformat()} ===")
    log.info(f"Provider: Mistral AI, Model: {args.model}")
    log.info(f"Worker: {args.worker_index}/{args.worker_total}")
    log.info(f"Batch size: {args.batch_size}, Delay: {args.delay}s, Limit: {args.limit}")
    
    # Step 1: Fetch chars needing mnemonics (chia theo worker)
    chars = fetch_chars_needing_mnemonics(args.limit, args.worker_index, args.worker_total)
    if not chars:
        log.info("✅ All chars (in my partition) already have mnemonics. Nothing to do.")
        return
    
    log.info(f"This worker will process {len(chars)} chars")
    
    # Step 2: Process in batches
    failed = []
    total_success = 0
    total_attempts = 0
    
    for i in range(0, len(chars), args.batch_size):
        batch = chars[i:i + args.batch_size]
        batch_num = i // args.batch_size + 1
        total_batches = (len(chars) + args.batch_size - 1) // args.batch_size
        log.info(f"\n--- Batch {batch_num}/{total_batches} ---")
        log.info(f"Chars: {[c['hanzi'] for c in batch]}")
        
        user_prompt = build_user_prompt(batch)
        
        for attempt in range(1, args.max_retries + 1):
            try:
                content = call_mistral(user_prompt, model=args.model)
                parsed = extract_json(content)
                log.info(f"API returned {len(parsed)} stories")
                
                # Validate
                updates = []
                for c in batch:
                    h = c['hanzi']
                    if h in parsed and is_good_story(parsed[h]):
                        updates.append({'hanzi': h, 'mnemonics': parsed[h].strip()})
                    else:
                        log.warning(f"  [{h}] no valid story returned")
                        failed.append({'hanzi': h, 'reason': 'no_story', 'attempt': attempt, **c})
                
                # Update Supabase
                if updates:
                    n = update_mnemonics_batch(updates)
                    total_success += n
                    log.info(f"✓ Updated {n}/{len(updates)} chars in Supabase")
                
                total_attempts += 1
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
                    for c in batch:
                        failed.append({'hanzi': c['hanzi'], 'reason': f'api_fail: {msg}', **c})
        
        # Delay between batches
        if i + args.batch_size < len(chars):
            time.sleep(args.delay)
    
    # Save failed list
    if failed:
        with open(FAILED_FILE, 'w', encoding='utf-8') as f:
            json.dump(failed, f, ensure_ascii=False, indent=2)
        log.info(f"\nFailed chars saved to {FAILED_FILE}")
    
    # Summary
    log.info(f"\n{'=' * 60}")
    log.info(f"=== SUMMARY ===")
    log.info(f"Provider: Mistral AI ({args.model})")
    log.info(f"Total chars processed: {len(chars)}")
    log.info(f"Successfully updated: {total_success}")
    log.info(f"Failed: {len(failed)}")
    log.info(f"=== Done at {datetime.now().isoformat()} ===")


if __name__ == '__main__':
    main()
