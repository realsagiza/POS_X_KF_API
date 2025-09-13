## POSPOS_API_SALE

Flask API ที่เรียกใช้ REST_API_CI แล้วตอบกลับทรง Generic JSON พร้อม Docker

### การแมป API
- POST `/api/v1/order` → ยิง `REST_API_CI /cashin` แบบ synchronous (รอผล) แล้วตอบกลับทรง `generic/create-sale-success.json` โดย
  - `data.amount` = จำนวนเงินที่ client ส่งมาในคำสั่งซื้อ
  - `data.status` =
    - `succeeded` เมื่อ upstream ตอบ 2xx และ parse ยอดได้ (หรือไม่ได้ก็ถือว่าสำเร็จ)
    - `failed` เมื่อ upstream ตอบ non-2xx (ส่ง HTTP 502)
    - `timeout` เมื่อครบเวลา `HTTP_TIMEOUT_SECONDS` (ดีฟอลต์ 300 วินาที, ส่ง HTTP 504)
    - `error` เมื่อเกิดข้อผิดพลาดอื่น (ส่ง HTTP 500)
- GET `/api/v1/status` → ตอบทรง `generic/get-by-id-success.json` โดย
  - `data.amount` = ยอดจาก `POST /api/v1/order` (ไม่ใช้ socket)
  - `data.cashin` = ค่าที่ parse ได้จาก response ของ `POST {UPSTREAM_BASE}/cashin` เป็นหลัก โดยคำนวณจากผลรวมของ `Cash` ที่ `type == "1"` ดังนี้
    - รวมทุก `Denomination`: `sum( (fv * sum(Piece.value)) ) / 100` หน่วยเป็นบาท (เนื่องจาก `fv` เป็นหน่วยสตางค์)
    - ถ้าไม่มี `Cash` ให้ fallback ไปใช้ `Amount[0].value / 100`
    - ถ้า parse ไม่ได้ ให้ fallback ไปใช้ `GET {UPSTREAM_BASE}/socket/latest` → `inserted_amount_baht`
  - `data.status` = `succeeded` หนึ่งครั้งหลังได้รับ response `/cashin` แล้วเคลียร์สถานะ, มิฉะนั้นเป็น `processing`; `cancelled` เมื่อยกเลิก
- PATCH `/api/v1/cancel/:id` หรือ `/api/v1/cancel` → (พารามิเตอร์ `:id` เป็น optional) เรียก `REST_API_CI /cashin_cancel` แล้วตอบกลับแบบ `generic/cancel-sale-success.json`
- GET `/api/v1/balances` → เรียก `REST_API_CI /inventory` แล้ว map เป็น `generic/get-inventory-success.json` (type 3 → qty, type 4 → inStacker)

ทุก endpoint ใส่ดีเลย์ 1 วินาทีเพื่อจำลองการประมวลผล

### การตั้งค่า
- กำหนดปลายทาง REST_API_CI ด้วย env `UPSTREAM_BASE` (ดีฟอลต์ `http://192.168.1.33:5000` ใน compose)
- ถ้ารันด้วย Docker บน Windows/Mac แล้ว REST_API_CI อยู่บนเครื่องโฮสต์ แนะนำตั้ง `UPSTREAM_BASE=http://host.docker.internal:5000`
- พอร์ตดีฟอลต์: `5115`
- ระยะเวลา timeout ของทุกการเรียก upstream: กำหนดด้วย env `HTTP_TIMEOUT_SECONDS` (ดีฟอลต์ `300` วินาที)
  - ตั้งค่าเป็น `none`/`infinite`/`inf` หรือค่า `<= 0` เพื่อ "รอไม่จำกัดเวลา" (ไม่แนะนำ)

การทำงานสถานะเป็นแบบ HTTP-only ไม่มีการเชื่อมต่อ WebSocket อีกต่อไป

### โครงสร้าง
```
POSPOS_API_SALE/
├─ app.py
├─ requirements.txt
├─ Dockerfile
├─ docker-compose.yml
├─ .dockerignore
└─ pospos_api_sale/
   └─ __init__.py
```

### วิธีรันด้วย Docker
```
cd POSPOS_API_SALE
# ตรวจสอบว่า REST_API_CI รันที่พอร์ต 5000
docker compose up -d --build
```

### ทดลองใช้งาน
```
# Health
curl http://localhost:5115/

# Inventory
curl http://localhost:5115/api/v1/balances

# สร้างออเดอร์
curl -X POST http://localhost:5115/api/v1/order \
  -H "Content-Type: application/json" \
  -d '{"amount": 500, "currency": "THB"}'

# สถานะ
curl http://localhost:5115/api/v1/status

# ยกเลิกออเดอร์
curl -X PATCH http://localhost:5115/api/v1/cancel/sale_123
# หรือหากไม่ระบุ id ก็ได้
curl -X PATCH http://localhost:5115/api/v1/cancel/
```

### การ Log
- ระหว่างเรียก `/cashin` จะมี log:
  - `Calling upstream /cashin url=... payload=...`
  - `Upstream /cashin responded status=... duration_ms=... content_type=... body=...`
  - `Parsed cashin amount from upstream response: ... THB`

### หมายเหตุการคำนวณ cashin จาก /cashin
- โครงสร้างตัวอย่างที่ใช้คำนวณ (ตัดมาบางส่วน):
  - `response.change_response.Body[0].ChangeResponse[0].Cash[*].Denomination[*]`
  - ใช้เฉพาะ `Cash` ที่ `type == "1"`
  - ยอดบาท = `sum( fv * sum(Piece.value) ) / 100`
  - ถ้าไม่มี `Cash` ให้ใช้ `Amount[0].value / 100`

### Troubleshooting
- ได้ `cashin` ไม่ตรงกับที่เครื่องหยอดรับจริง: เปิดดู log ของ `/cashin` และส่งตัวอย่าง payload กลับมาเพื่อปรับตัว parser
- เชื่อม REST_API_CI ไม่ได้: ตรวจ `UPSTREAM_BASE` ให้ชี้ถูก service และเครือข่ายระหว่าง container
