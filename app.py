"""
アポイント管理 + 文字起こし親和性分析 + 営業レポート自動生成ツール
"""
import os
import json
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
import anthropic
from reports_bp import reports_bp

app = Flask(__name__)
app.register_blueprint(reports_bp)
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

# ─────────────────────────────────────────────
# DB初期化
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            company TEXT,
            role TEXT,
            email TEXT,
            phone TEXT,
            industry TEXT,
            categories TEXT,   -- JSON array of tag strings
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER,
            title TEXT NOT NULL,
            date TEXT,
            transcript TEXT,
            summary TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS industries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            raw_transcript TEXT,
            structured_json TEXT,
            appointments_set INTEGER,
            meetings_held INTEGER,
            contracts INTEGER,
            referrals INTEGER,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS aggregate_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            analysis_json TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
    """)

    # デフォルト業種
    default_industries = [
        "IT・ソフトウェア", "製造業", "金融・保険", "不動産",
        "医療・ヘルスケア", "小売・EC", "飲食・食品", "教育",
        "コンサルティング", "広告・マーケティング", "物流・運輸",
        "エネルギー", "建設・土木", "メディア・エンタメ", "人材", "デザイン・Web", "その他"
    ]
    for ind in default_industries:
        c.execute("INSERT OR IGNORE INTO industries(name) VALUES (?)", (ind,))

    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
# 静的ページ
# ─────────────────────────────────────────────
@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as f:
        return f.read()

# ─────────────────────────────────────────────
# 業種 API
# ─────────────────────────────────────────────
@app.route("/api/industries", methods=["GET"])
def get_industries():
    conn = get_db()
    rows = conn.execute("SELECT * FROM industries ORDER BY name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/industries", methods=["POST"])
def add_industry():
    data = request.json
    conn = get_db()
    try:
        conn.execute("INSERT INTO industries(name) VALUES (?)", (data["name"],))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "既に存在します"}), 400
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/industries/<int:iid>", methods=["DELETE"])
def delete_industry(iid):
    conn = get_db()
    conn.execute("DELETE FROM industries WHERE id=?", (iid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# タグ API
# ─────────────────────────────────────────────
@app.route("/api/tags", methods=["GET"])
def get_tags():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/tags", methods=["POST"])
def add_tag():
    data = request.json
    conn = get_db()
    try:
        conn.execute("INSERT INTO tags(name) VALUES (?)", (data["name"],))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "既に存在します"}), 400
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/tags/<int:tid>", methods=["DELETE"])
def delete_tag(tid):
    conn = get_db()
    conn.execute("DELETE FROM tags WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# コンタクト API
# ─────────────────────────────────────────────
@app.route("/api/contacts", methods=["GET"])
def get_contacts():
    industry = request.args.get("industry", "")
    tag = request.args.get("tag", "")
    keyword = request.args.get("q", "")

    conn = get_db()
    query = "SELECT c.*, COUNT(a.id) as appo_count FROM contacts c LEFT JOIN appointments a ON a.contact_id=c.id WHERE 1=1"
    params = []

    if industry:
        query += " AND c.industry=?"
        params.append(industry)
    if keyword:
        query += " AND (c.name LIKE ? OR c.company LIKE ? OR c.role LIKE ?)"
        kw = f"%{keyword}%"
        params += [kw, kw, kw]
    if tag:
        query += " AND c.categories LIKE ?"
        params.append(f'%"{tag}"%')

    query += " GROUP BY c.id ORDER BY c.updated_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        d["categories"] = json.loads(d["categories"]) if d["categories"] else []
        result.append(d)
    return jsonify(result)

@app.route("/api/contacts/<int:cid>", methods=["GET"])
def get_contact(cid):
    conn = get_db()
    row = conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    d = dict(row)
    d["categories"] = json.loads(d["categories"]) if d["categories"] else []
    appointments = conn.execute(
        "SELECT * FROM appointments WHERE contact_id=? ORDER BY date DESC", (cid,)
    ).fetchall()
    conn.close()
    d["appointments"] = [dict(a) for a in appointments]
    return jsonify(d)

@app.route("/api/contacts", methods=["POST"])
def create_contact():
    data = request.json
    categories = json.dumps(data.get("categories", []), ensure_ascii=False)
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO contacts(name,company,role,email,phone,industry,categories,notes)
           VALUES(?,?,?,?,?,?,?,?)""",
        (data.get("name",""), data.get("company",""), data.get("role",""),
         data.get("email",""), data.get("phone",""), data.get("industry",""),
         categories, data.get("notes",""))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"id": new_id})

@app.route("/api/contacts/<int:cid>", methods=["PUT"])
def update_contact(cid):
    data = request.json
    categories = json.dumps(data.get("categories", []), ensure_ascii=False)
    conn = get_db()
    conn.execute(
        """UPDATE contacts SET name=?,company=?,role=?,email=?,phone=?,
           industry=?,categories=?,notes=?,updated_at=datetime('now','localtime')
           WHERE id=?""",
        (data.get("name",""), data.get("company",""), data.get("role",""),
         data.get("email",""), data.get("phone",""), data.get("industry",""),
         categories, data.get("notes",""), cid)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/contacts/<int:cid>", methods=["DELETE"])
def delete_contact(cid):
    conn = get_db()
    conn.execute("DELETE FROM contacts WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# アポイント API
# ─────────────────────────────────────────────
@app.route("/api/appointments", methods=["GET"])
def get_appointments():
    conn = get_db()
    rows = conn.execute(
        """SELECT a.*, c.name as contact_name, c.company, c.industry
           FROM appointments a
           LEFT JOIN contacts c ON c.id=a.contact_id
           ORDER BY a.date DESC"""
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/appointments", methods=["POST"])
def create_appointment():
    data = request.json
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO appointments(contact_id,title,date,transcript,summary) VALUES(?,?,?,?,?)",
        (data.get("contact_id"), data.get("title",""), data.get("date",""),
         data.get("transcript",""), data.get("summary",""))
    )
    conn.commit()
    new_id = cur.lastrowid
    # コンタクトのupdated_atも更新
    if data.get("contact_id"):
        conn.execute("UPDATE contacts SET updated_at=datetime('now','localtime') WHERE id=?",
                     (data["contact_id"],))
        conn.commit()
    conn.close()
    return jsonify({"id": new_id})

@app.route("/api/appointments/<int:aid>", methods=["PUT"])
def update_appointment(aid):
    data = request.json
    conn = get_db()
    conn.execute(
        "UPDATE appointments SET contact_id=?,title=?,date=?,transcript=?,summary=? WHERE id=?",
        (data.get("contact_id"), data.get("title",""), data.get("date",""),
         data.get("transcript",""), data.get("summary",""), aid)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/appointments/<int:aid>", methods=["DELETE"])
def delete_appointment(aid):
    conn = get_db()
    conn.execute("DELETE FROM appointments WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# AI 親和性分析 API
# ─────────────────────────────────────────────
@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.json
    transcript = data.get("transcript", "").strip()
    api_key = data.get("api_key", "").strip()
    filter_industry = data.get("industry", "")  # 絞り込み用（任意）

    if not transcript:
        return jsonify({"error": "文字起こしが空です"}), 400
    if not api_key:
        return jsonify({"error": "Anthropic API Keyを入力してください"}), 400

    # 全コンタクト取得
    conn = get_db()
    query = "SELECT * FROM contacts"
    params = []
    if filter_industry:
        query += " WHERE industry=?"
        params.append(filter_industry)
    contacts = conn.execute(query, params).fetchall()

    # 各コンタクトの最近のアポ情報も取得
    contact_list = []
    for c in contacts:
        cd = dict(c)
        cd["categories"] = json.loads(cd["categories"]) if cd["categories"] else []
        appos = conn.execute(
            "SELECT title, date, summary FROM appointments WHERE contact_id=? ORDER BY date DESC LIMIT 3",
            (c["id"],)
        ).fetchall()
        cd["recent_appointments"] = [dict(a) for a in appos]
        contact_list.append(cd)
    conn.close()

    if not contact_list:
        return jsonify({"error": "コンタクトが登録されていません"}), 400

    # Claude APIで分析
    client = anthropic.Anthropic(api_key=api_key)

    contacts_json = json.dumps(contact_list, ensure_ascii=False, indent=2)

    prompt = f"""あなたは営業・ビジネス開発の専門家です。
以下の「会話の文字起こし」を分析し、登録されているコンタクトリストの中から
**最も親和性の高い人物**をランキング形式でリストアップしてください。

## 会話の文字起こし
---
{transcript}
---

## 登録コンタクト一覧
```json
{contacts_json}
```

## 分析指示
1. 文字起こしから以下を抽出してください：
   - 主要テーマ・トピック
   - 課題・ニーズ・関心事
   - 業界・職種のコンテキスト
   - キーワード・専門用語

2. 各コンタクトとの親和性を評価する観点：
   - 業種・業界の一致度
   - 役職・職種の関連性
   - ノート・過去アポとの共通点
   - 抱えていそうな課題との合致度

3. 出力形式（必ずJSON形式で返してください）：
{{
  "transcript_analysis": {{
    "main_topics": ["テーマ1", "テーマ2"],
    "needs_and_concerns": ["課題1", "課題2"],
    "industry_context": "検出された業界コンテキスト",
    "keywords": ["キーワード1", "キーワード2"]
  }},
  "ranked_contacts": [
    {{
      "rank": 1,
      "contact_id": <id>,
      "name": "<名前>",
      "company": "<会社名>",
      "affinity_score": <1-100の数値>,
      "affinity_level": "<高/中/低>",
      "reasons": [
        "<根拠1>",
        "<根拠2>",
        "<根拠3>"
      ],
      "suggested_approach": "<この人物へのアプローチ提案>"
    }}
  ]
}}

親和性スコアが50以上のコンタクトのみリストアップし、スコア降順で並べてください。
必ず有効なJSONのみを返し、前後に説明文を入れないでください。"""

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}]
        )

        # レスポンスからテキスト取得
        result_text = ""
        for block in response.content:
            if block.type == "text":
                result_text = block.text
                break

        # JSONパース
        result_json = json.loads(result_text)
        return jsonify(result_json)

    except json.JSONDecodeError:
        # JSONが取り出せない場合はそのまま返す
        return jsonify({"raw": result_text})
    except anthropic.AuthenticationError:
        return jsonify({"error": "APIキーが無効です"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────
# 無料 親和性分析 API（キーワードマッチ）
# ─────────────────────────────────────────────
@app.route("/api/analyze/free", methods=["POST"])
def analyze_free():
    import re
    data = request.json
    transcript = data.get("transcript", "").strip()
    filter_industry = data.get("industry", "")

    if not transcript:
        return jsonify({"error": "文字起こしが空です"}), 400

    conn = get_db()
    query = "SELECT * FROM contacts"
    params = []
    if filter_industry:
        query += " WHERE industry=?"
        params.append(filter_industry)
    contacts = conn.execute(query, params).fetchall()

    contact_list = []
    for c in contacts:
        cd = dict(c)
        cd["categories"] = json.loads(cd["categories"]) if cd["categories"] else []
        appos = conn.execute(
            "SELECT title, date, summary FROM appointments WHERE contact_id=? ORDER BY date DESC LIMIT 3",
            (c["id"],)
        ).fetchall()
        cd["recent_appointments"] = [dict(a) for a in appos]
        contact_list.append(cd)
    conn.close()

    if not contact_list:
        return jsonify({"error": "コンタクトが登録されていません"}), 400

    # キーワード抽出（2文字以上の語）
    stop_words = {
        'です', 'ます', 'ました', 'ません', 'ありがとう', 'よろしく', 'おねがい',
        'する', 'した', 'して', 'いる', 'いた', 'いて', 'ある', 'あった',
        'こと', 'もの', 'ため', 'より', 'から', 'まで', 'など', 'として',
        'ところ', 'それ', 'これ', 'あれ', 'その', 'この', 'ので', 'けど',
        'でも', 'けれど', 'しかし', 'また', 'さらに', 'そして', 'なので',
    }
    raw_words = re.findall(r'[ぁ-んァ-ン一-龥a-zA-Zａ-ｚＡ-Ｚ0-9０-９ー]{2,}', transcript)
    keywords = set(w for w in raw_words if w not in stop_words)

    # 頻度カウント（分析メタ用）
    word_freq = {}
    for w in raw_words:
        if w not in stop_words:
            word_freq[w] = word_freq.get(w, 0) + 1
    top_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)

    ranked = []
    for contact in contact_list:
        score = 0
        reasons = []

        field_weights = [
            ('industry', contact.get('industry') or '', 30, '業種'),
            ('role',     contact.get('role')     or '', 20, '役職'),
            ('company',  contact.get('company')  or '', 15, '会社名'),
            ('notes',    contact.get('notes')    or '', 10, 'メモ'),
        ]
        for _, val, weight, label in field_weights:
            if not val:
                continue
            matched = [kw for kw in keywords if kw in val]
            if matched:
                score += weight * len(matched)
                reasons.append(f"{label}「{val}」がキーワード「{'、'.join(matched[:3])}」と一致")

        for tag in contact.get('categories', []):
            matched = [kw for kw in keywords if kw in tag]
            if matched:
                score += 25
                reasons.append(f"タグ「{tag}」がキーワードと一致")

        for appo in contact.get('recent_appointments', []):
            appo_text = (appo.get('title') or '') + ' ' + (appo.get('summary') or '')
            matched = [kw for kw in keywords if kw in appo_text]
            if matched:
                score += 10
                reasons.append(f"過去アポ「{appo.get('title','')}」との関連性あり")

        if score < 15:
            continue

        affinity_score = min(100, int(score * 1.2))
        affinity_level = '高' if affinity_score >= 70 else '中' if affinity_score >= 45 else '低'
        industry = contact.get('industry', '')
        role = contact.get('role', '')
        approach = f"{role}として{industry}分野の課題へのアプローチを検討" if (role or industry) else "過去の接点をもとにアプローチを検討"

        ranked.append({
            'contact_id': contact['id'],
            'name': contact['name'],
            'company': contact.get('company', ''),
            'affinity_score': affinity_score,
            'affinity_level': affinity_level,
            'reasons': reasons[:3] if reasons else ['共通するキーワードが検出されました'],
            'suggested_approach': approach,
        })

    ranked.sort(key=lambda x: x['affinity_score'], reverse=True)
    for i, r in enumerate(ranked):
        r['rank'] = i + 1

    return jsonify({
        'transcript_analysis': {
            'main_topics':       [w for w, _ in top_words[:5]],
            'needs_and_concerns':[w for w, _ in top_words[5:10]],
            'industry_context':  'キーワードマッチ（無料モード）',
            'keywords':          [w for w, _ in top_words[:15]],
        },
        'ranked_contacts': ranked,
        'mode': 'free',
    })

# ─────────────────────────────────────────────
# 起動
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print("=" * 50)
    print("アポ管理ツール 起動中...")
    print("ブラウザで http://localhost:5000 を開いてください")
    print("終了するには Ctrl+C を押してください")
    print("=" * 50)
    app.run(debug=False, port=5000)
