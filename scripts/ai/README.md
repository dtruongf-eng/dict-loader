# AI Generate Hanzi Mnemonics — GitHub Action (Mistral AI, 10x Parallel)

Workflow chạy **10 job song song**, mỗi job dùng 1 Mistral API key khác nhau → tốc độ tăng 10 lần.

## Cách hoạt động

```
                        Supabase A (zh_kanji_builder)
                                  ↓
                    SELECT WHERE mnemonics IS NULL
                                  ↓
                  ┌───────────────┴───────────────┐
                  ↓                               ↓
           Python script                   Python script
         (worker 1, key 1)               (worker 2, key 2)
                  ↓                               ↓
           Mistral API                     Mistral API
                  ↓                               ↓
           UPDATE Supabase                 UPDATE Supabase
                  ↓                               ↓
                  └───────────────┬───────────────┘
                                  ↓
                          10 workers song song
                          (x10 tốc độ)
```

**Cơ chế chia việc:**
- Mỗi chữ Hán được hash (modulo 10) để gán cho 1 worker cố định
- Worker N chỉ xử lý các chữ có `hash(hanzi) % 10 == N-1`
- → Không bao giờ trùng lặp, không cần lock DB

## Setup

### 1. Tạo 10 Mistral API Keys

Vào https://console.mistral.ai/api-keys/ — tạo 10 keys (có thể cùng account, mỗi key có rate limit riêng).

Hoặc dùng 10 account khác nhau (free tier mỗi account = 1 key).

Lưu lại 10 keys vào GitHub Secrets (bước 2).

### 2. Tạo GitHub Secrets

Vào repo GitHub → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Secret name | Value | Lấy từ đâu |
|-------------|-------|------------|
| `SUPABASE_URL` | `https://zpkesloqadbqpwrgjrwi.supabase.co` | Supabase A Dashboard → Project Settings → API |
| `SUPABASE_SERVICE_KEY` | `eyJhbGciOi...` | Supabase A Dashboard → `service_role` key |
| `MISTRAL_API_KEY_1` | `key1...` | https://console.mistral.ai/api-keys/ |
| `MISTRAL_API_KEY_2` | `key2...` | |
| `MISTRAL_API_KEY_3` | `key3...` | |
| `MISTRAL_API_KEY_4` | `key4...` | |
| `MISTRAL_API_KEY_5` | `key5...` | |
| `MISTRAL_API_KEY_6` | `key6...` | |
| `MISTRAL_API_KEY_7` | `key7...` | |
| `MISTRAL_API_KEY_8` | `key8...` | |
| `MISTRAL_API_KEY_9` | `key9...` | |
| `MISTRAL_API_KEY_10` | `key10...` | |

### 3. Commit & push

```bash
git add .github/workflows/generate-mnemonics.yml scripts/ai/
git commit -m "Add 10-worker parallel Mistral AI mnemonics generator"
git push
```

### 4. Chạy workflow

Vào tab **Actions** → **"AI Generate Hanzi Mnemonics"** → **Run workflow**:

- **total_limit**: TỔNG số chữ cần process (default: 100). Workflow sẽ chia đều cho 10 workers.
  - VD: nhập `100` → mỗi worker xử lý 10 chữ
  - VD: nhập `0` → mỗi worker xử lý TẤT CẢ chữ trong partition của nó
- **model**: `mistral-small-latest` (default) / `mistral-large-latest` / `open-mistral-nemo` / `open-mixtral-8x7b`
- **batch_size**: số chữ / API call (default 5)

## Tốc độ ước tính

| Setup | 1000 chữ | 3000 chữ |
|-------|----------|----------|
| 1 worker | ~17 phút | ~50 phút |
| 10 workers | ~1.7 phút | ~5 phút |

**Chi phí:** Không đổi (cùng số API call), chỉ nhanh hơn.

## Logs & Monitoring

Mỗi worker tạo artifact riêng:
- `mnemonics-log-worker-1-{run_id}` → `mnemonics_worker_1.log` + `failed_chars_worker_1.json`
- `mnemonics-log-worker-2-{run_id}` → `mnemonics_worker_2.log` + ...
- ... (worker 3-10)
- Summary job in tổng số chars còn thiếu sau khi tất cả worker xong

Vào Actions → run cụ thể → thấy 10 jobs chạy song song:

```
prepare    ✓ (3s)
generate (1)  ✓ (1m 30s)   ─┐
generate (2)  ✓ (1m 28s)    │
generate (3)  ✓ (1m 31s)    │
generate (4)  ✓ (1m 29s)    ├─ 10 jobs chạy song song
generate (5)  ✓ (1m 30s)    │
generate (6)  ✓ (1m 32s)    │
generate (7)  ✓ (1m 27s)    │
generate (8)  ✓ (1m 31s)    │
generate (9)  ✓ (1m 29s)    │
generate (10) ✓ (1m 30s)   ─┘
summary    ✓ (5s)
```

## Cấu trúc files

```
zh-app/
├── .github/
│   └── workflows/
│       └── generate-mnemonics.yml         # Workflow với matrix 10 workers
└── scripts/
    └── ai/
        ├── generate_mnemonics.py          # Python script (--worker-index, --worker-total)
        └── README.md
```

## Test local (1 worker)

```bash
export SUPABASE_URL="https://zpkesloqadbqpwrgjrwi.supabase.co"
export SUPABASE_SERVICE_KEY="eyJ..."
export MISTRAL_API_KEY="key1..."

# Chạy với 5 chữ, worker 1/1
python scripts/ai/generate_mnemonics.py --limit 5 --model mistral-small-latest
```

## Test local (10 workers, mô phỏng GitHub Action)

```bash
export SUPABASE_URL="https://zpkesloqadbqpwrgjrwi.supabase.co"
export SUPABASE_SERVICE_KEY="eyJ..."

# 10 keys khác nhau
KEYS=("key1..." "key2..." "key3..." "key4..." "key5..." "key6..." "key7..." "key8..." "key9..." "key10...")

# Chạy 10 process song song
for i in 1 2 3 4 5 6 7 8 9 10; do
  MISTRAL_API_KEY="${KEYS[$((i-1))]}" \
    python scripts/ai/generate_mnemonics.py \
      --limit 10 \
      --model mistral-small-latest \
      --worker-index $i \
      --worker-total 10 &
done
wait
echo "All workers done"

# Xem log từng worker
for i in 1 2 3 4 5 6 7 8 9 10; do
  echo "=== Worker $i ==="
  tail -5 scripts/ai/mnemonics_worker_$i.log
done
```

## Troubleshooting

**Lỗi: "❌ MISTRAL_API_KEY_N chưa được set"**
→ Worker N không có key. Kiểm tra secrets `MISTRAL_API_KEY_1` đến `MISTRAL_API_KEY_10` đã được set đúng tên.

**1 worker fail, 9 worker vẫn chạy?**
→ Có. Workflow dùng `fail-fast: false`, 9 worker khác vẫn hoàn thành. Xem log worker fail để biết lý do.

**Muốn đổi số worker (5, 20, v.v.)**
→ Sửa trong `generate-mnemonics.yml`:
```yaml
strategy:
  matrix:
    worker_index: [1, 2, 3, 4, 5]   # 5 workers
```
Và update `WORKER_TOTAL: 5` trong env.

**Worker xử lý trùng chữ?**
→ Không. Hàm `hanzi_hash` chia đều theo `sum(ord(c)) % worker_total`, đảm bảo mỗi chữ chỉ thuộc 1 worker.

**Muốn chạy lại cho TẤT CẢ 3088 chữ (overwrite)**
→ Sửa query trong `generate_mnemonics.py` (xem comment trong code), bỏ filter `or=(mnemonics.is.null,mnemonics.eq.)`.
