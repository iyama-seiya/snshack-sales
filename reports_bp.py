"""
営業レポート自動生成 Blueprint
- 日報：文字起こし → KPI/行動/課題/気づき/勝ちパターン/NGパターン抽出
- 週報：直近7日の日報を集計・分析
- 月報：直近30日の日報を集計・分析
"""
import os
import json
import sqlite3
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
import anthropic

reports_bp = Blueprint("reports", __name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ═══════════════════════════════════════════════
# Claude プロンプト定義
# ═══════════════════════════════════════════════

DAILY_PROMPT = """あなたは売上向上・リファラル最大化を唯一の目的とした営業分析AIです。
以下の文字起こしから営業活動データを抽出してください。

## 絶対ルール
- 数値は文字起こしに明示されているもののみ抽出（推測・計算・補完禁止 → 不明はnull）
- 紹介・リファラル・紹介獲得・繋いでもらう・紹介してもらうに関する全ての発言を必ず検出
- 抽象論禁止 → 「○○する」ではなく「誰に・何を・いつまでに・どうする」レベルで記述
- KPI定義：アポ=日程確定した件数 / 商談=実施した件数 / 受注=契約した件数 / 紹介獲得=実際に繋がった件数

## 文字起こし
---
{transcript}
---

## 出力（有効なJSONのみ・前後に説明文不要）
{{
  "date": "{date}",
  "kpis": {{
    "appointments_set": <整数|null>,
    "meetings_held": <整数|null>,
    "contracts": <整数|null>,
    "referrals": <整数|null>
  }},
  "actions": [
    {{"action": "実施した具体的行動", "result": "相手の反応・結果", "next_step": "次にやること（誰に・何を・いつ）"}}
  ],
  "issues": [
    {{"issue": "具体的な課題", "impact": "放置した場合の損失・影響", "action_needed": "今週中にやるべき具体的対策"}}
  ],
  "insights": [
    {{"insight": "今日得た気づき・学び", "applicable_to": "この知見を使える相手・場面"}}
  ],
  "referral_mentions": [
    {{"context": "紹介に関する発言・状況の詳細", "person": "関係者名（不明はnull）", "status": "打診済|検討中|獲得|見込あり|null"}}
  ],
  "win_patterns": [
    {{"pattern": "うまくいったアプローチ・言い回し", "condition": "どんな相手・状況で通じたか", "evidence": "根拠となる具体的発言・事実"}}
  ],
  "ng_patterns": [
    {{"pattern": "うまくいかなかったこと", "reason": "なぜ失敗したか（具体的）", "improvement": "次回から変えること（具体的）"}}
  ],
  "priority_actions": [
    {{"rank": 1, "action": "最優先でやるべき具体的アクション", "expected_outcome": "実施した場合の期待成果", "deadline": "期限（例：明日中/今週金曜/null）"}}
  ],
  "raw_summary": "今日の活動を3行で要約（事実のみ）"
}}"""


WEEKLY_PROMPT = """あなたは売上向上・リファラル最大化を専門とする営業戦略アドバイザーです。
過去7日間の営業活動データを分析し、来週の勝ち筋を提示してください。

## 絶対ルール
- 全ての提言は具体的アクション形式（誰に・何を・いつ・どうする）
- 勝ちパターン・NGパターンは必ずパターン化して再現・回避可能にする
- ボトルネックは数値的根拠（例：商談→受注転換率XX%）とセットで提示
- リファラル機会は全て特定してアクション化する
- 推測・抽象論・精神論禁止

## 7日間の日次データ
{daily_data}

## 出力（有効なJSONのみ）
{{
  "period": {{"start": "{start_date}", "end": "{end_date}"}},
  "kpi_summary": {{
    "appointments_set": <合計整数|null>,
    "meetings_held": <合計整数|null>,
    "contracts": <合計整数|null>,
    "referrals": <合計整数|null>,
    "conversion_appo_to_meeting": "<XX%|null>",
    "conversion_meeting_to_contract": "<XX%|null>"
  }},
  "bottlenecks": [
    {{"bottleneck": "ボトルネック名", "evidence": "データに基づく根拠", "revenue_impact": "売上への影響", "solution": "来週中の具体的解決策"}}
  ],
  "win_patterns": [
    {{"pattern": "勝ちパターン名", "occurrence": <回数|null>, "condition": "通じる相手・状況・条件", "how_to_replicate": "再現するための具体的手順"}}
  ],
  "ng_patterns": [
    {{"pattern": "NGパターン名", "occurrence": <回数|null>, "root_cause": "根本原因", "how_to_avoid": "具体的回避方法"}}
  ],
  "referral_opportunities": [
    {{"person": "対象者名（不明はnull）", "opportunity": "紹介機会の詳細", "action": "具体的アクション", "timing": "いつ動くか"}}
  ],
  "priority_actions": [
    {{"rank": 1, "action": "来週最優先アクション", "why": "なぜこれが最優先か（数値的理由）", "expected_outcome": "期待成果", "deadline": "期限"}}
  ],
  "weekly_summary": "今週の総括と来週の戦略（5行以内・具体的）"
}}"""


MONTHLY_PROMPT = """あなたは売上向上・リファラル最大化を専門とする営業戦略アドバイザーです。
過去30日間の営業活動データを分析し、翌月の勝ち戦略を提示してください。

## 絶対ルール
- 勝ちパターンは再現マニュアル形式で出力（誰でも実行できる手順）
- トレンド分析は週次推移を必ず含める
- リファラル戦略は来月の具体的アクションプランとして出力
- 抽象論・精神論・推測禁止

## 30日間の日次データ
{daily_data}

## 出力（有効なJSONのみ）
{{
  "period": {{"start": "{start_date}", "end": "{end_date}"}},
  "kpi_summary": {{
    "appointments_set": <合計|null>,
    "meetings_held": <合計|null>,
    "contracts": <合計|null>,
    "referrals": <合計|null>,
    "conversion_appo_to_meeting": "<XX%|null>",
    "conversion_meeting_to_contract": "<XX%|null>",
    "best_performing_day": "最も成果が出た曜日・時間帯（不明はnull）"
  }},
  "trend_analysis": {{
    "week1": "第1週の特徴",
    "week2": "第2週の特徴",
    "week3": "第3週の特徴",
    "week4": "第4週の特徴",
    "overall_trend": "全体トレンドの評価",
    "improving_areas": ["改善している項目"],
    "declining_areas": ["悪化している項目"]
  }},
  "bottlenecks": [
    {{"bottleneck": "ボトルネック名", "evidence": "根拠", "revenue_impact": "売上影響", "solution": "翌月の解決策"}}
  ],
  "win_patterns_manual": [
    {{
      "pattern_name": "パターン名",
      "applicable_to": "どんな相手・場面に使えるか",
      "steps": ["ステップ1（具体的）", "ステップ2", "ステップ3"],
      "key_phrases": ["効果的な言い回し1", "言い回し2"],
      "expected_result": "期待成果"
    }}
  ],
  "ng_patterns": [
    {{"pattern": "NGパターン", "root_cause": "根本原因", "how_to_avoid": "具体的回避方法"}}
  ],
  "referral_strategy": {{
    "total_referrals": <数値|null>,
    "referral_sources": ["紹介元1", "紹介元2"],
    "next_month_actions": [
      {{"action": "具体的アクション", "target_person": "対象者", "expected_referrals": <期待件数|null>, "timing": "時期"}}
    ]
  }},
  "priority_actions": [
    {{"rank": 1, "action": "翌月最優先アクション", "why": "根拠", "expected_outcome": "期待成果", "deadline": "期限"}}
  ],
  "monthly_summary": "今月の総括と翌月戦略（7行以内・来月から即実行できる内容）"
}}"""


# ═══════════════════════════════════════════════
# ヘルパー
# ═══════════════════════════════════════════════

def call_claude(api_key, prompt, max_tokens=4000):
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text for b in response.content if b.type == "text"), "")


def safe_json(text):
    """JSON文字列をパース。コードブロックが含まれていても対応"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(text)


# ═══════════════════════════════════════════════
# 日報 API
# ═══════════════════════════════════════════════

@reports_bp.route("/api/reports/daily", methods=["POST"])
def create_daily_report():
    data = request.json
    transcript = data.get("transcript", "").strip()
    api_key = data.get("api_key", "").strip()
    date = data.get("date", datetime.now().strftime("%Y-%m-%d"))

    if not transcript:
        return jsonify({"error": "文字起こしが空です"}), 400
    if not api_key:
        return jsonify({"error": "API Keyを入力してください"}), 400

    prompt = DAILY_PROMPT.format(transcript=transcript, date=date)
    try:
        result_text = call_claude(api_key, prompt, max_tokens=4000)
        structured = safe_json(result_text)
    except json.JSONDecodeError:
        return jsonify({"error": "AI出力のパースに失敗しました", "raw": result_text}), 500
    except anthropic.AuthenticationError:
        return jsonify({"error": "APIキーが無効です"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    kpis = structured.get("kpis", {})
    conn = get_db()
    # 同日レポートは上書き
    existing = conn.execute("SELECT id FROM daily_reports WHERE date=?", (date,)).fetchone()
    if existing:
        conn.execute(
            """UPDATE daily_reports SET raw_transcript=?, structured_json=?,
               appointments_set=?, meetings_held=?, contracts=?, referrals=?,
               created_at=datetime('now','localtime') WHERE date=?""",
            (transcript, json.dumps(structured, ensure_ascii=False),
             kpis.get("appointments_set"), kpis.get("meetings_held"),
             kpis.get("contracts"), kpis.get("referrals"), date),
        )
        report_id = existing["id"]
    else:
        cur = conn.execute(
            """INSERT INTO daily_reports
               (date, raw_transcript, structured_json, appointments_set, meetings_held, contracts, referrals)
               VALUES (?,?,?,?,?,?,?)""",
            (date, transcript, json.dumps(structured, ensure_ascii=False),
             kpis.get("appointments_set"), kpis.get("meetings_held"),
             kpis.get("contracts"), kpis.get("referrals")),
        )
        report_id = cur.lastrowid
    conn.commit()
    conn.close()

    structured["id"] = report_id
    return jsonify(structured)


@reports_bp.route("/api/reports/daily", methods=["GET"])
def list_daily_reports():
    conn = get_db()
    rows = conn.execute(
        """SELECT id, date, appointments_set, meetings_held, contracts, referrals,
                  structured_json, created_at
           FROM daily_reports ORDER BY date DESC"""
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            sj = json.loads(d["structured_json"])
            d["raw_summary"] = sj.get("raw_summary", "")
            d["priority_actions"] = sj.get("priority_actions", [])[:2]
            d["referral_count"] = len(sj.get("referral_mentions", []))
        except Exception:
            pass
        del d["structured_json"]
        result.append(d)
    return jsonify(result)


@reports_bp.route("/api/reports/daily/<int:rid>", methods=["GET"])
def get_daily_report(rid):
    conn = get_db()
    row = conn.execute("SELECT * FROM daily_reports WHERE id=?", (rid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    d = dict(row)
    d["structured_json"] = json.loads(d["structured_json"])
    return jsonify(d)


@reports_bp.route("/api/reports/daily/<int:rid>", methods=["DELETE"])
def delete_daily_report(rid):
    conn = get_db()
    conn.execute("DELETE FROM daily_reports WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════
# 週報 API
# ═══════════════════════════════════════════════

@reports_bp.route("/api/reports/weekly", methods=["POST"])
def generate_weekly():
    data = request.json
    api_key = data.get("api_key", "").strip()
    end_date = data.get("end_date", datetime.now().strftime("%Y-%m-%d"))
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=6)
    start_date = start_dt.strftime("%Y-%m-%d")

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM daily_reports WHERE date BETWEEN ? AND ? ORDER BY date",
        (start_date, end_date),
    ).fetchall()
    conn.close()

    if not rows:
        return jsonify({"error": f"{start_date} 〜 {end_date} の日報がありません"}), 400

    daily_data = [
        {**dict(r), "structured_json": json.loads(dict(r)["structured_json"])}
        for r in rows
    ]

    prompt = WEEKLY_PROMPT.format(
        daily_data=json.dumps(daily_data, ensure_ascii=False, indent=2),
        start_date=start_date,
        end_date=end_date,
    )
    try:
        result_text = call_claude(api_key, prompt, max_tokens=5000)
        structured = safe_json(result_text)
    except json.JSONDecodeError:
        return jsonify({"error": "AI出力のパースに失敗しました", "raw": result_text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO aggregate_reports(type, start_date, end_date, analysis_json) VALUES(?,?,?,?)",
        ("weekly", start_date, end_date, json.dumps(structured, ensure_ascii=False)),
    )
    conn.commit()
    structured["id"] = cur.lastrowid
    conn.close()
    return jsonify(structured)


# ═══════════════════════════════════════════════
# 月報 API
# ═══════════════════════════════════════════════

@reports_bp.route("/api/reports/monthly", methods=["POST"])
def generate_monthly():
    data = request.json
    api_key = data.get("api_key", "").strip()
    end_date = data.get("end_date", datetime.now().strftime("%Y-%m-%d"))
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=29)
    start_date = start_dt.strftime("%Y-%m-%d")

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM daily_reports WHERE date BETWEEN ? AND ? ORDER BY date",
        (start_date, end_date),
    ).fetchall()
    conn.close()

    if not rows:
        return jsonify({"error": f"{start_date} 〜 {end_date} の日報がありません"}), 400

    daily_data = [
        {**dict(r), "structured_json": json.loads(dict(r)["structured_json"])}
        for r in rows
    ]

    prompt = MONTHLY_PROMPT.format(
        daily_data=json.dumps(daily_data, ensure_ascii=False, indent=2),
        start_date=start_date,
        end_date=end_date,
    )
    try:
        result_text = call_claude(api_key, prompt, max_tokens=6000)
        structured = safe_json(result_text)
    except json.JSONDecodeError:
        return jsonify({"error": "AI出力のパースに失敗しました", "raw": result_text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO aggregate_reports(type, start_date, end_date, analysis_json) VALUES(?,?,?,?)",
        ("monthly", start_date, end_date, json.dumps(structured, ensure_ascii=False)),
    )
    conn.commit()
    structured["id"] = cur.lastrowid
    conn.close()
    return jsonify(structured)


# ═══════════════════════════════════════════════
# 集計レポート一覧 API
# ═══════════════════════════════════════════════

@reports_bp.route("/api/reports/aggregate", methods=["GET"])
def list_aggregate_reports():
    rtype = request.args.get("type", "")
    conn = get_db()
    q = "SELECT id, type, start_date, end_date, analysis_json, created_at FROM aggregate_reports"
    params = []
    if rtype:
        q += " WHERE type=?"
        params.append(rtype)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            aj = json.loads(d["analysis_json"])
            d["kpi_summary"] = aj.get("kpi_summary", {})
            d["summary_text"] = aj.get("weekly_summary") or aj.get("monthly_summary", "")
        except Exception:
            pass
        del d["analysis_json"]
        result.append(d)
    return jsonify(result)


@reports_bp.route("/api/reports/aggregate/<int:rid>", methods=["GET"])
def get_aggregate_report(rid):
    conn = get_db()
    row = conn.execute("SELECT * FROM aggregate_reports WHERE id=?", (rid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    d = dict(row)
    d["analysis_json"] = json.loads(d["analysis_json"])
    return jsonify(d)


@reports_bp.route("/api/reports/aggregate/<int:rid>", methods=["DELETE"])
def delete_aggregate_report(rid):
    conn = get_db()
    conn.execute("DELETE FROM aggregate_reports WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})
