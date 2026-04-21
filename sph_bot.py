import os
import json
import logging
import requests as http_requests
import base64
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
import copy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")          # Google Sheets utama (DB_UTAMA_PT3D)
RS_SPREADSHEET_ID = "1EbPsEeNzZRPSWvXw6ZIeXlZUlfxmvvbaM_ptSKGMfPE"  # Database RS
PRODUK_SPREADSHEET_ID = "155FVVVuN9hWzR_TvKvvQFHx4-45xWFV6_j8OMRm83Bo"  # Database Produk
TEMPLATE_DOC_ID = os.environ.get("TEMPLATE_DOC_ID")        # Google Docs template SPH
SPH_FOLDER_ID = os.environ.get("SPH_FOLDER_ID")            # Google Drive folder output

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]

# ─── GOOGLE AUTH ──────────────────────────────────────────────────────────────
def get_google_creds():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    creds_dict = json.loads(creds_json)
    return Credentials.from_service_account_info(creds_dict, scopes=SCOPES)

def get_sheets():
    creds = get_google_creds()
    return gspread.Client(auth=creds)

# ─── SESSION STORE (in-memory) ────────────────────────────────────────────────
sessions = {}

def get_session(user_id):
    session = sessions.get(str(user_id), {"step": "idle"})
    if "items" not in session:
        session["items"] = []
    return session

def set_session(user_id, data):
    sessions[str(user_id)] = data

def clear_session(user_id):
    sessions[str(user_id)] = {"step": "idle"}

# ─── GOOGLE SHEETS HELPERS ────────────────────────────────────────────────────
def lookup_sales(telegram_id):
    gc = get_sheets()
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet("Sales_Mapping")
    records = ws.get_all_records()
    for r in records:
        if str(r.get("Telegram_ID")) == str(telegram_id):
            return r
    return None

def search_rs(query):
    gc = get_sheets()
    # Cari sheet RS - sesuaikan nama sheet
    ws = gc.open_by_key(RS_SPREADSHEET_ID).worksheet("Sheet1")
    records = ws.get_all_records()
    query_lower = query.lower()
    return [r for r in records if query_lower in str(r.get("NAMA RS", "")).lower()][:8]

def get_all_products():
    gc = get_sheets()
    ws = gc.open_by_key(PRODUK_SPREADSHEET_ID).worksheet("Sheet1")
    values = ws.get_all_values()
    if not values:
        return []
    headers = values[0]
    records = []
    for row in values[1:]:
        record = {}
        for i, val in enumerate(row):
            if i < len(headers):
                key = headers[i] if headers[i] else f"col_{i}"
                if key not in record:
                    record[key] = val
        records.append(record)
    return records

def get_products_by_merk(merk):
    products = get_all_products()
    return [p for p in products if p.get("Merek") == merk]

def get_sph_counter(sales_kode):
    gc = get_sheets()
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet("Sales_Counter")
    records = ws.get_all_records()
    now = datetime.now()
    for r in records:
        if str(r.get("Kode")) == str(sales_kode):
            if r.get("Bulan") == now.month and r.get("Tahun") == now.year:
                return int(r.get("Counter", 0))
    return 0

BULAN_ROMAWI = ["I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII"]

def update_sph_counter(sales_kode, new_counter):
    gc = get_sheets()
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet("Sales_Counter")
    now = datetime.now()
    records = ws.get_all_records()
    for i, r in enumerate(records):
        if str(r.get("Kode")) == str(sales_kode):
            row = i + 2
            ws.update(f"A{row}:E{row}", [[
                sales_kode,
                now.month,
                BULAN_ROMAWI[now.month - 1],
                now.year,
                new_counter
            ]])
            return
    ws.append_row([sales_kode, now.month, BULAN_ROMAWI[now.month - 1], now.year, new_counter])

def log_sph(no_sph, tanggal, sales_kode, sales_nama, nama_rs, total_items):
    gc = get_sheets()
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet("SPH_Log")
    ws.append_row([no_sph, tanggal, sales_kode, sales_nama, nama_rs, total_items, "New",
                   f"https://drive.google.com/drive/folders/{SPH_FOLDER_ID}"])

# ─── GENERATE SPH PDF ─────────────────────────────────────────────────────────
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "https://script.google.com/macros/s/AKfycbwTCl9VHk-nDHTj8evOesEWM3Tkk6t4GWajimz9EzUlqYBFvK7AnpQH7Qz1WNfWNns/exec")

def generate_sph_pdf(session):
    sph_data = session["sph_data"]
    
    # Build replacements per kolom per baris
    replacements = {
        "{{tanggal}}": sph_data["tanggal"],
        "{{noSPHID}}": sph_data["no_sph"],
        "{{nama RS}}": sph_data["nama_rs"],
        "{{namaSales}}": sph_data["sales_nama"],
        "{{posisiSales}}": sph_data["sales_posisi"],
        "{{ttdSales}}": "",
    }
    
    grand_total = 0
    for i, item in enumerate(sph_data["items"], 1):
        harga = float(item.get("harga", 0))
        qty = int(item.get("qty", 0))
        jumlah = harga * qty
        grand_total += jumlah
        replacements[f"{{{{no_{i}}}}}"] = str(i)
        replacements[f"{{{{id_{i}}}}}"] = str(item.get("id", ""))
        replacements[f"{{{{nama_{i}}}}}"] = str(item.get("nama", ""))
        replacements[f"{{{{unit_{i}}}}}"] = str(item.get("unit", ""))
        replacements[f"{{{{harga_{i}}}}}"] = f"Rp {harga:,.0f}".replace(",", ".")
        replacements[f"{{{{qty_{i}}}}}"] = str(qty)
        replacements[f"{{{{jumlah_{i}}}}}"] = f"Rp {jumlah:,.0f}".replace(",", ".")
        replacements[f"{{{{link_{i}}}}}"] = str(item.get("link", ""))
    
    # Kosongkan placeholder yang tidak terpakai
    for j in range(len(sph_data["items"]) + 1, 21):
        for field in ["no", "id", "nama", "unit", "harga", "qty", "jumlah", "link"]:
            replacements[f"{{{{{field}_{j}}}}}"] = ""
    
    replacements["{{total_grand}}"] = f"Rp {grand_total:,.0f}".replace(",", ".")
    
    # Kirim data ke Apps Script
    payload = {
        "no_sph": sph_data["no_sph"],
        "sales_kode": sph_data.get("sales_kode", ""),
        "replacements": replacements
    }
    
    response = http_requests.post(APPS_SCRIPT_URL, json=payload, timeout=60)
    result = response.json()
    
    if not result.get("success"):
        raise Exception(f"Apps Script error: {result.get('error', 'Unknown error')}")
    
    # Decode PDF dari base64
    pdf_data = base64.b64decode(result["pdf_base64"])
    return pdf_data, sph_data["no_sph"]

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Halo! Saya bot PT3D.\n\nKetik /sph untuk buat Surat Penawaran Harga.")

async def cmd_sph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sales = lookup_sales(user_id)
    if not sales:
        await update.message.reply_text("❌ Telegram ID kamu belum terdaftar. Hubungi admin PT3D.")
        return

    set_session(user_id, {
        "step": "waiting_rs",
        "user_id": str(user_id),
        "chat_id": update.effective_chat.id,
        "sales": {
            "kode": sales.get("Kode"),
            "nama": sales.get("Nama_Lengkap"),
            "posisi": sales.get("Posisi"),
        },
        "rs": {},
        "items": []
    })

    await update.message.reply_text(
        f"Halo *{sales.get('Nama_Lengkap')}*! 👋\n\n"
        f"Saya akan bantu buat *Surat Penawaran Harga (SPH)*.\n\n"
        f"*Langkah 1: Pilih Customer (RS)*\n\n"
        f"Ketik nama Rumah Sakit (minimal 3 huruf):",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    session = get_session(user_id)
    step = session.get("step", "idle")

    if step == "waiting_rs":
        results = search_rs(text)
        if not results:
            await update.message.reply_text("❌ RS tidak ditemukan. Coba ketik ulang (minimal 3 huruf):")
            return

        keyboard = [[InlineKeyboardButton(
            f"🏥 {r['NAMA RS']} - {r.get('KAB/KOTA', '')}",
            callback_data=f"rs:{r['KODE RS']}:{r['NAMA RS'][:30]}:{r.get('KAB/KOTA','')[:20]}"
        )] for r in results]

        await update.message.reply_text(
            f"✅ Ditemukan *{len(results)}* RS. Pilih yang sesuai:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif step == "waiting_qty":
        try:
            qty = int(text)
            if qty <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Masukkan angka yang valid:")
            return

        pending = session.get("pending_item")
        session["items"].append({
            "id": pending["id"],
            "nama": pending["nama"],
            "unit": pending["unit"],
            "harga": pending["harga"],
            "qty": qty,
            "link": pending.get("link", "")
        })
        session.pop("pending_item", None)

        # Tanya tambah lagi atau generate
        keyboard = [
            [InlineKeyboardButton("➕ Tambah Item Lagi", callback_data="action:add_more")],
            [InlineKeyboardButton("✅ Generate SPH", callback_data="action:generate")]
        ]
        await update.message.reply_text(
            f"✅ Item ditambahkan! Total: *{len(session['items'])}* item.\n\nMau tambah lagi?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        set_session(user_id, session)

    else:
        # KB fallback - bisa tambahkan AI agent di sini nanti
        await update.message.reply_text(
            "Gunakan /sph untuk membuat Surat Penawaran Harga.\n\nUntuk pertanyaan produk, fitur KB segera hadir."
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    session = get_session(user_id)

    # Pilih RS
    if data.startswith("rs:"):
        parts = data.split(":")
        rs_kode, rs_nama, rs_kota = parts[1], parts[2], parts[3] if len(parts) > 3 else ""
        session["rs"] = {"kode": rs_kode, "nama": rs_nama, "kota": rs_kota}
        session["step"] = "waiting_merk"
        set_session(user_id, session)

        products = get_all_products()
        merks = sorted(set(p["Merek"] for p in products if p.get("Merek")))
        keyboard = []
        for i in range(0, len(merks), 2):
            row = [InlineKeyboardButton(merks[i], callback_data=f"merk:{merks[i]}")]
            if i+1 < len(merks):
                row.append(InlineKeyboardButton(merks[i+1], callback_data=f"merk:{merks[i+1]}"))
            keyboard.append(row)

        await query.edit_message_text(
            f"✅ RS dipilih: *{rs_nama}*\n\n*Langkah 2: Pilih Merk Produk*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # Pilih Merk
    elif data.startswith("merk:"):
        merk = data[5:]
        session["current_merk"] = merk
        session["selected_merk"] = merk
        session["step"] = "waiting_item"
        set_session(user_id, session)

        items = get_products_by_merk(merk)
        # Simpan items ke session supaya index konsisten saat dipilih
        session["current_items"] = items
        set_session(user_id, session)
        keyboard = []
        for idx, p in enumerate(items[:50]):
            item_name = p.get('Item Name', '')[:40]
            cb = f"itx:{idx}"
            keyboard.append([InlineKeyboardButton(item_name, callback_data=cb)])

        await query.edit_message_text(
            f"*Merk: {merk}*\n\nPilih produk:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # Pilih Item
    elif data.startswith("itx:"):
        # Pakai items dari session untuk konsistensi index
        items = session.get("current_items")
        if not items:
            merk = session.get("selected_merk", session.get("current_merk", ""))
            items = get_products_by_merk(merk)
        try:
            idx = int(data.split(":")[1])
            item = items[idx] if idx < len(items) else None
            item_id = str(item.get("Item ID", "")) if item else ""
        except (ValueError, IndexError):
            item = None
            item_id = ""
        if not item:
            await query.edit_message_text("❌ Item tidak ditemukan.")
            return

        logging.info(f"Item keys: {list(item.keys())}")
        # Cari kolom harga dengan flexible matching (handle spasi di nama kolom)
        harga_raw = None
        for key in item.keys():
            if 'Harga' in key and 'Cat' in key:
                harga_raw = item[key]
                logging.info(f"Found harga key: {key!r} = {harga_raw!r}")
                break
        if harga_raw is None:
            harga_raw = 0
        try:
            harga_clean = str(harga_raw).replace("Rp.", "").replace("Rp", "").replace(".", "").replace(",", "").strip()
            harga_float = float(harga_clean) if harga_clean else 0
        except:
            harga_float = 0
        session["pending_item"] = {
            "id": str(item.get("Item ID", "")),
            "nama": item.get("Item Name", ""),
            "unit": item.get("Unit", ""),
            "harga": harga_float,
            "link": item.get("Link E-katalog V6", "")
        }
        session["step"] = "waiting_qty"
        set_session(user_id, session)

        await query.edit_message_text(
            f"✅ *{session['pending_item']['nama']}*\n"
            f"Harga: Rp {int(session['pending_item']['harga']):,} / {session['pending_item']['unit']}\n\n"
            f"Masukkan *qty*:",
            parse_mode="Markdown"
        )

    # Tambah lagi
    elif data == "action:add_more":
        session["step"] = "waiting_merk"
        set_session(user_id, session)

        products = get_all_products()
        merks = sorted(set(p["Merek"] for p in products if p.get("Merek")))
        keyboard = []
        for i in range(0, len(merks), 2):
            row = [InlineKeyboardButton(merks[i], callback_data=f"merk:{merks[i]}")]
            if i+1 < len(merks):
                row.append(InlineKeyboardButton(merks[i+1], callback_data=f"merk:{merks[i+1]}"))
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("✅ Selesai - Generate SPH", callback_data="action:generate")])

        await query.edit_message_text(
            f"📦 Item: *{len(session['items'])}*\n\nPilih merk berikutnya:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # Generate SPH
    elif data == "action:generate":
        if "sales" not in session:
            await query.edit_message_text("❌ Session expired. Silakan mulai ulang dengan /sph")
            return
        await query.edit_message_text("⏳ Membuat SPH, mohon tunggu...")

        now = datetime.now()
        sales = session["sales"]
        counter = get_sph_counter(sales["kode"]) + 1
        update_sph_counter(sales["kode"], counter)

        no_sph = f"SPH/PT3D/{sales['kode']}/{BULAN_ROMAWI[now.month-1]}/{now.year}/{str(counter).zfill(3)}"

        session["sph_data"] = {
            "no_sph": no_sph,
            "tanggal": now.strftime("%d %B %Y"),
            "nama_rs": session["rs"]["nama"],
            "sales_kode": sales["kode"],
            "sales_nama": sales["nama"],
            "sales_posisi": sales["posisi"],
            "items": session["items"]
        }

        try:
            pdf_bytes, no_sph_label = generate_sph_pdf(session)
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=io.BytesIO(pdf_bytes),
                filename=f"{no_sph_label}.pdf",
                caption=f"✅ *SPH Berhasil Dibuat!*\n\n"
                        f"📄 No: `{no_sph_label}`\n"
                        f"🏥 RS: {session['rs']['nama']}\n"
                        f"📦 Items: {len(session['items'])} produk\n\n"
                        f"Silakan kirim ke customer!",
                parse_mode="Markdown"
            )
            log_sph(no_sph_label, now.strftime("%d/%m/%Y"), sales["kode"], sales["nama"],
                    session["rs"]["nama"], len(session["items"]))
        except Exception as e:
            logger.error(f"Error generate SPH: {e}")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"❌ Error generate SPH: {str(e)}"
            )

        clear_session(user_id)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("sph", cmd_sph))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
