# -*- coding: utf-8 -*-
"""
Omani Dialect Sentiment Data Collector
---------------------------------------
Two-role web app:
  - Participants: open "/", write a dialect sentence, self-tag its sentiment.
  - Admin: open "/admin", log in with a password, see live stats and export CSV.

Storage: PostgreSQL (NOT local SQLite). This is required if you deploy to
Render's free tier (or most PaaS free tiers) — their filesystem is ephemeral,
so a local SQLite file gets wiped on every restart/redeploy/idle-spin-down.
A free hosted Postgres (e.g. https://neon.tech, https://supabase.com) fixes
that: your data lives independently of the app's compute instance.

Run locally:
    pip install -r requirements.txt
    export DATABASE_URL="postgresql://user:pass@host/dbname"   # from Neon/Supabase
    export ADMIN_PASSWORD="choose-a-strong-password"
    export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    python app.py

Then open http://localhost:5000  (participant form)
and    http://localhost:5000/admin  (admin login)

See README.md for deployment instructions.
"""

import csv
import io
import os
import re
import time
from datetime import datetime
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import (
    Flask, request, jsonify, render_template, redirect,
    url_for, session, send_file, g
)

# ---------------------------------------------------------------
# Config
# ---------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-only-insecure-key-change-me")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Get a free Postgres connection string from "
        "https://neon.tech or https://supabase.com and set it as an "
        "environment variable before running this app."
    )

if ADMIN_PASSWORD == "changeme123":
    print("!" * 70)
    print("WARNING: Using the default admin password. Set ADMIN_PASSWORD")
    print("as an environment variable before deploying this publicly.")
    print("!" * 70)

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Very simple in-memory rate limiter: max N submissions per IP per window.
# Resets on server restart. Good enough to blunt casual spam/bot abuse;
# swap for Flask-Limiter + Redis if you need something more robust.
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_SUBMISSIONS = 5
_submission_log = {}  # ip -> list of timestamps

# ---------------------------------------------------------------
# Topics: (key, label, prompt)
# NOTE: keys must be unique — the database groups/validates by this key,
# so two different questions must never share one. ("Generalities" below
# is split into three distinct keys for exactly this reason.)
# ---------------------------------------------------------------
TOPICS = [
    # --- Original set ---
    ("weather", "الطقس", "وش رايك في الجو هالأيام؟ خبرني عنه بجملة."),
    ("food", "الأكل", "شاركنا تجربتك الأخيرة مع أكل جربته أو طبختيه."),
    ("traffic", "الطريق والسواقة", "كيف كانت السواقة والطريق عندك اليوم أو أمس؟"),
    ("shopping", "التسوق", "آخر مرة تسوقت، كيف كانت التجربة؟"),
    ("Occasions", "المناسبات", "وين تسوي عيدك أكثر: بمسقط ولا بالبلاد؟"),
    ("work", "الشغل", "كيف ماشي وضع الشغل أو الدراسة عندك هالفترة؟"),
    ("health", "الصحة", "شاركنا تجربة أخيرة مع مستشفى أو عيادة أو دكتور."),
    ("Traditions", "العادات و التقاليد", "تشوف العادات والتقاليد لسا مهمة إلى الحين؟"),
    ("technology", "التقنية والإنترنت", "كيف الإنترنت أو التطبيقات اللي تستخدمها هالأيام؟"),
    ("government", "الخدمات الحكومية", "آخر معاملة سويتها بجهة حكومية، كيف كانت؟"),
    ("tourism", "السياحة والسفر", "خبرني عن رحلة أو مكان زرته أخيراً."),
    ("sports", "الرياضة", "شاركنا رأيك بمباراة أو نشاط رياضي أخير."),
    ("Generalities_generations", "عموميات", "شنو رايك بجيل اليوم مقارنة بجيل عمك حميد؟"),
    ("wedding_social", "المناسبات الاجتماعية", "خبرني عن عرس أو عزيمة أو مناسبة حضرتها أخيراً."),
    ("Prices", "الأسعار", "تحس الأسعار زادت وايد في عمان هالفترة؟"),
    ("sea_fishing", "البحر والصيد", "إذا تصيد أو تروح البحر، خبرني عن آخر تجربة."),
    ("agriculture", "الزراعة", "إذا عندك مزرعة أو نخل، خبرني عن الموسم هالسنة."),
    ("finance", "الفلوس والبنك", "خبرني عن تجربة أخيرة مع البنك أو مصروف معين."),
    ("entertainment", "الترفيه", "آخر فيلم أو برنامج أو فعالية حضرتها، كيف كانت؟"),
    ("khareef_dhofar", "خريف صلالة", "إذا زرت صلالة وقت الخريف، خبرني عن التجربة."),
    ("Work Mode", "طريقة العمل", "تحس نفسك أنتج وانت تداوم أونلاين ولا حضوري؟"),
    ("environment_dust", "البيئة والغبار", "كيف جودة الهوا أو النظافة بمنطقتك هالأيام؟"),
    ("parking_fines", "المخالفات المرورية", "إذا صارت لك مخالفة أو غرامة، خبرني عن الموقف."),
    ("banking_apps", "تطبيقات البنوك", "كيف تجربتك مع تطبيق بنكك؟"),
    ("real_estate", "السكن والعقار", "إذا تدور بيت أو شقة، خبرني عن آخر بحث سويته."),
    ("Generalities_opportunities", "عموميات", "تشوف الشاب العماني عنده فرص كافية للنجاح ولا يواجه عوايق؟"),
    ("Generalities_quality_of_life", "عموميات", "تشوف الحياة في عمان مريحة وسهلة؟"),
    ("fitness_gym", "الرياضة واللياقة", "كيف تجربتك مع النادي أو التمرين هالفترة؟"),
    ("beach_outings", "الفسحة والشاطئ", "خبرني عن آخر فسحة أو نزهة عالبحر."),
    ("delivery_apps", "تطبيقات التوصيل", "كيف كانت آخر تجربة توصيل طلب لك؟"),
    ("customer_service", "خدمة العملاء", "خبرني عن تجربة أخيرة مع خدمة عملاء شركة."),
    ("utilities", "فواتير الماء والكهرباء", "كيف وضع الفواتير أو الخدمات عندك هالشهر؟"),

    # --- Balance-pass additions (from user-suggested questions + gap-filling) ---
    ("floods_rain", "الأمطار والسيول", "عساكم سيلتوا هالأيام؟ شلون كانت حالة الأمطار عندكم؟"),
    ("stray_livestock", "الحلال السايبة", "ويش رايك في اللي يربي حلال ويسرحه في أراضي الناس بلا رقيب؟"),
    ("youth_behavior_public", "تصرفات بالأماكن العامة", "جالك رحت مكان سياحي وشفت فيه شي ما عجبك من تصرفات بعض الشباب؟"),
    ("salalah_visit_general", "زيارة صلالة", "إذا رحت صلالة هالسنة، كيف كانت زيارتك؟"),
    ("kids_school_break", "إجازة الصغار", "هين سُرتوا في إجازة الصغار، ولا طلعت متعبة عليكم؟"),
    ("public_transport", "المواصلات العامة", "ويش رايك في وضع المواصلات العامة في عمان؟"),
    ("rent_costs", "إيجارات السكن", "شلون شايف إيجارات البيوت والشقق هالأيام، معقولة ولا غالية؟"),
    ("hospital_wait", "الانتظار في المستشفيات", "طولت عليك المواعيد أو الانتظار في مستشفى أو مركز صحي حكومي؟"),
    ("neighbor_noise", "مشاكل مع الجيران", "صار عندك موقف إزعاج أو خلاف مع جيرانك بسبب الضوضاء أو شي ثاني؟"),

    # --- Crowd-focused batch ---
    ("crowd_mall_eid", "الزحمة في السوق قبل العيد", "شلون كانت الزحمة في السوق أو المول قبل العيد هالسنة؟"),
    ("crowd_airport", "الزحمة في المطار", "شفت زحمة في مطار مسقط وقت موسم السفر؟ كيف كانت تجربتك هناك؟"),
    ("crowd_stadium", "زحمة المدرجات بالمباريات", "كيف كان جو الزحمة وأنت بالمدرجات وقت حضورك مباراة؟"),
    ("crowd_beach_weekend", "زحمة الشاطئ نهاية الأسبوع", "كيف كانت زحمة الشاطئ أو المنتزه آخر نهاية أسبوع رحت فيها؟"),
    ("crowd_bank_queue", "طابور البنك", "طولت عليك المدة في طابور البنك أو عند الصراف الآلي آخر مرة؟"),
    ("crowd_mosque_prayer", "زحمة صلاة الجمعة أو التراويح", "كيف زحمة صلاة الجمعة أو التراويح في مسجدكم هالفترة؟"),
]

# Build lookup structures from TOPICS itself, so editing the list above is
# the only thing you ever need to do — nothing else needs to change in sync.
TOPIC_BY_KEY = {t[0]: t for t in TOPICS}
TOPIC_KEYS = set(TOPIC_BY_KEY.keys())

assert len(TOPIC_KEYS) == len(TOPICS), (
    "Duplicate topic keys found in TOPICS — every key must be unique, "
    "even if two questions share the same display label."
)


def region_for(topic_key: str) -> str:
    return "Dhofar (Salalah)" if topic_key == "khareef_dhofar" else "Oman - General/Northern"


# ---------------------------------------------------------------
# Database helpers (PostgreSQL)
# ---------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS responses (
                    id SERIAL PRIMARY KEY,
                    sentence TEXT NOT NULL,
                    question TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    region TEXT NOT NULL,
                    style TEXT NOT NULL DEFAULT 'community_submitted',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
    conn.close()


# ---------------------------------------------------------------
# Validation
# ---------------------------------------------------------------
ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


def is_mostly_arabic(text: str) -> bool:
    arabic_chars = len(ARABIC_RE.findall(text))
    return arabic_chars >= max(6, int(len(text) * 0.4))


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limited(ip: str) -> bool:
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    timestamps = [t for t in _submission_log.get(ip, []) if t > window_start]
    _submission_log[ip] = timestamps
    if len(timestamps) >= RATE_LIMIT_MAX_SUBMISSIONS:
        return True
    timestamps.append(now)
    _submission_log[ip] = timestamps
    return False


# ---------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------
def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------
# Participant routes
# ---------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", topics=TOPICS)


@app.route("/submit", methods=["POST"])
def submit():
    ip = client_ip()
    if rate_limited(ip):
        return jsonify({"ok": False, "error": "rate_limited",
                         "message": "إرسال كثير بوقت قصير، حاول بعد شوي."}), 429

    data = request.get_json(silent=True) or {}
    sentence = (data.get("sentence") or "").strip()
    sentiment = (data.get("sentiment") or "").strip()
    topic = (data.get("topic") or "").strip()

    if len(sentence) < 6 or len(sentence) > 500:
        return jsonify({"ok": False, "error": "bad_length",
                         "message": "اكتب جملة بطول مناسب (6-500 حرف)."}), 400
    if not is_mostly_arabic(sentence):
        return jsonify({"ok": False, "error": "not_arabic",
                         "message": "الرجاء الكتابة بالعربية (لهجتك العمانية)."}), 400
    if sentiment not in {"positive", "negative", "neutral"}:
        return jsonify({"ok": False, "error": "bad_sentiment",
                         "message": "اختر شعورك تجاه الجملة."}), 400
    if topic not in TOPIC_KEYS:
        return jsonify({"ok": False, "error": "bad_topic",
                         "message": "موضوع غير صالح."}), 400

    # Trust the server's own copy of the question text for this topic key,
    # rather than whatever the client sent — the client can't be trusted
    # to send the exact current wording, and this keeps it tamper-proof.
    question_text = TOPIC_BY_KEY[topic][2]

    db = get_db()
    with db:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO responses (sentence, question, sentiment, topic, region, style) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (sentence, question_text, sentiment, topic, region_for(topic), "community_submitted"),
            )
            cur.execute("SELECT COUNT(*) FROM responses")
            total = cur.fetchone()[0]

    return jsonify({"ok": True, "total": total})


@app.route("/count")
def count():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM responses")
        total = cur.fetchone()[0]
    return jsonify({"total": total})


# ---------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password and password == ADMIN_PASSWORD:
            session["is_admin"] = True
            next_url = request.args.get("next") or url_for("admin_dashboard")
            return redirect(next_url)
        error = "كلمة المرور غير صحيحة."
        time.sleep(1)  # slow down brute-force guessing
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT COUNT(*) AS c FROM responses")
        total = cur.fetchone()["c"]

        cur.execute("SELECT sentiment, COUNT(*) AS c FROM responses GROUP BY sentiment")
        sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0}
        for row in cur.fetchall():
            sentiment_counts[row["sentiment"]] = row["c"]

        cur.execute("SELECT topic, COUNT(*) AS c FROM responses GROUP BY topic ORDER BY c DESC")
        by_topic = cur.fetchall()

        cur.execute(
            "SELECT id, sentence, question, sentiment, topic, created_at FROM responses "
            "ORDER BY id DESC LIMIT 30"
        )
        recent = cur.fetchall()

    # topic_labels maps key -> display label, built fresh from TOPICS every
    # time, so it always reflects your current list even for old rows saved
    # under a topic you've since renamed the label of.
    topic_labels = {t[0]: t[1] for t in TOPICS}

    return render_template(
        "admin_dashboard.html",
        total=total,
        sentiment_counts=sentiment_counts,
        by_topic=by_topic,
        topic_labels=topic_labels,
        recent=recent,
    )


@app.route("/admin/export.csv")
@admin_required
def admin_export_csv():
    db = get_db()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, sentence, question, sentiment, topic, region, style, created_at "
            "FROM responses ORDER BY id ASC"
        )
        rows = cur.fetchall()

    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM so Excel opens Arabic text correctly
    writer = csv.writer(buf)
    writer.writerow(["id", "sentence", "question", "sentiment", "topic", "region", "style", "created_at"])
    for r in rows:
        writer.writerow([r["id"], r["sentence"], r["question"], r["sentiment"],
                          r["topic"], r["region"], r["style"], r["created_at"]])

    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    filename = f"omani_dialect_submissions_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv"
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)


@app.route("/admin/delete/<int:response_id>", methods=["POST"])
@admin_required
def admin_delete(response_id):
    db = get_db()
    with db:
        with db.cursor() as cur:
            cur.execute("DELETE FROM responses WHERE id = %s", (response_id,))
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
else:
    init_db()
