"""
営業レポート Blueprint
- 日報：手動入力でKPI/パターン/課題を記録
- 週報・月報：日報データを集計して自動生成（API不要）
"""
import os
import json
import sqlite3
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify

reports_bp = Blueprint("reports", __name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ═══════════════════════════════════════════════
# ヘルパー
# ═══════════════════════════════════════════════

def _to_int(v):
    try:
        return int(v) if v not in (None, "", "null") else None
    except (ValueError, TypeError):
        return None


def _save_daily(date, transcript, structured):
    """日報をDBに保存（新規 or 上書き）"""
    kpis = structured.get("kpis", {})
    conn = get_db()
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
    return report_id


def _aggregate_free(daily_data, start_date, end_date, period_type):
    """日報データを集計して週報/月報JSONを生成（API不要）"""
    total_appo     = 0
    total_meeting  = 0
    total_contract = 0
    total_referral = 0

    all_win      = []
    all_ng       = []
    all_ref      = []
    all_issues   = []
    all_actions  = []
    all_summaries = []

    for d in daily_data:
        sj   = d["structured_json"]
        kpis = sj.get("kpis", {})
        total_appo     += kpis.get("appointments_set") or 0
        total_meeting  += kpis.get("meetings_held")    or 0
        total_contract += kpis.get("contracts")        or 0
        total_referral += kpis.get("referrals")        or 0

        all_win.extend(sj.get("win_patterns", []))
        all_ng.extend(sj.get("ng_patterns", []))
        all_ref.extend(sj.get("referral_mentions", []))
        all_issues.extend(sj.get("issues", []))
        all_actions.extend(sj.get("priority_actions", []))
        if sj.get("raw_summary"):
            all_summaries.append(f"[{d['date']}] {sj['raw_summary']}")

    conv_appo_meeting      = f"{round(total_meeting  / total_appo     * 100)}%" if total_appo     else None
    conv_meeting_contract  = f"{round(total_contract / total_meeting  * 100)}%" if total_meeting  else None

    # ボトルネック自動検出
    bottlenecks = []
    if total_appo > 0 and total_meeting == 0:
        bottlenecks.append({
            "bottleneck": "アポ→商談の転換率が0%",
            "evidence": f"アポ{total_appo}件に対し商談0件",
            "revenue_impact": "売上機会をすべて取りこぼしている状態",
            "solution": "アポ後の即日フォロー・日程確認を徹底する"
        })
    elif total_meeting > 0 and total_contract == 0:
        bottlenecks.append({
            "bottleneck": "商談→受注の転換率が0%",
            "evidence": f"商談{total_meeting}件に対し受注0件",
            "revenue_impact": "クロージング改善で売上増加の可能性あり",
            "solution": "商談内容・提案方法・価格提示タイミングを見直す"
        })
    for issue in all_issues[:3]:
        bottlenecks.append({
            "bottleneck": issue.get("issue", ""),
            "evidence": issue.get("impact") or "日報より",
            "revenue_impact": issue.get("impact") or "要確認",
            "solution": issue.get("action_needed") or "対策を検討"
        })

    result = {
        "period": {"start": start_date, "end": end_date},
        "kpi_summary": {
            "appointments_set":              total_appo     or None,
            "meetings_held":                 total_meeting  or None,
            "contracts":                     total_contract or None,
            "referrals":                     total_referral or None,
            "conversion_appo_to_meeting":    conv_appo_meeting,
            "conversion_meeting_to_contract":conv_meeting_contract,
        },
        "bottlenecks": bottlenecks[:5],
        "win_patterns": [
            {"pattern": w.get("pattern", ""), "occurrence": None,
             "condition": w.get("condition"), "how_to_replicate": w.get("evidence")}
            for w in all_win[:5]
        ],
        "ng_patterns": [
            {"pattern": n.get("pattern", ""), "occurrence": None,
             "root_cause": n.get("reason"), "how_to_avoid": n.get("improvement")}
            for n in all_ng[:5]
        ],
        "referral_opportunities": [
            {"person": r.get("person"), "opportunity": r.get("context", ""),
             "action": None, "timing": None}
            for r in all_ref[:5]
        ],
        "priority_actions": [
            {"rank": i + 1, "action": a.get("action", ""), "why": None,
             "expected_outcome": a.get("expected_outcome"), "deadline": a.get("deadline")}
            for i, a in enumerate(all_actions[:5])
        ],
    }

    summary_text = "\n".join(all_summaries) if all_summaries else f"{start_date}〜{end_date}の集計結果"

    if period_type == "weekly":
        result["weekly_summary"] = summary_text
    else:
        # 週別トレンド
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        weeks = [[], [], [], []]
        for d in daily_data:
            dt = datetime.strptime(d["date"], "%Y-%m-%d")
            week_num = min((dt - start_dt).days // 7, 3)
            weeks[week_num].append(d)

        def _week_label(week_data):
            if not week_data:
                return "データなし"
            appo = sum((d["structured_json"].get("kpis", {}).get("appointments_set") or 0) for d in week_data)
            mtg  = sum((d["structured_json"].get("kpis", {}).get("meetings_held")    or 0) for d in week_data)
            return f"アポ{appo}件・商談{mtg}件"

        result["kpi_summary"]["best_performing_day"] = None
        result["trend_analysis"] = {
            "week1": _week_label(weeks[0]),
            "week2": _week_label(weeks[1]),
            "week3": _week_label(weeks[2]),
            "week4": _week_label(weeks[3]),
            "overall_trend": f"計{len(daily_data)}日分のデータより集計",
            "improving_areas": [],
            "declining_areas": [],
        }
        result["win_patterns_manual"] = [
            {"pattern_name": w.get("pattern", ""), "applicable_to": w.get("condition"),
             "steps": [], "key_phrases": [], "expected_result": w.get("evidence")}
            for w in all_win[:3]
        ]
        result["referral_strategy"] = {
            "total_referrals": total_referral or None,
            "referral_sources": [r.get("person") for r in all_ref if r.get("person")][:5],
            "next_month_actions": [
                {"action": r.get("context", ""), "target_person": r.get("person"),
                 "expected_referrals": None, "timing": None}
                for r in all_ref[:3]
            ],
        }
        result["monthly_summary"] = summary_text

    return result


# ═══════════════════════════════════════════════
# 日報 API
# ═══════════════════════════════════════════════

@reports_bp.route("/api/reports/daily", methods=["POST"])
def create_daily_report():
    data   = request.json
    date   = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    manual = data.get("manual", {})

    structured = {
        "date": date,
        "kpis": {
            "appointments_set": _to_int(manual.get("appointments_set")),
            "meetings_held":    _to_int(manual.get("meetings_held")),
            "contracts":        _to_int(manual.get("contracts")),
            "referrals":        _to_int(manual.get("referrals")),
        },
        "actions":           [{"action": a, "result": None, "next_step": None}      for a in manual.get("actions", [])           if a],
        "issues":            [{"issue": i, "impact": None, "action_needed": None}   for i in manual.get("issues", [])            if i],
        "insights":          [{"insight": i, "applicable_to": None}                 for i in manual.get("insights", [])          if i],
        "referral_mentions": [{"context": r, "person": None, "status": None}        for r in manual.get("referral_mentions", []) if r],
        "win_patterns":      [{"pattern": w, "condition": None, "evidence": None}   for w in manual.get("win_patterns", [])      if w],
        "ng_patterns":       [{"pattern": n, "reason": None, "improvement": None}   for n in manual.get("ng_patterns", [])       if n],
        "priority_actions":  [{"rank": i+1, "action": a, "expected_outcome": None, "deadline": None} for i, a in enumerate(manual.get("priority_actions", [])) if a],
        "raw_summary":       manual.get("raw_summary", ""),
    }
    report_id = _save_daily(date, "", structured)
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
            d["raw_summary"]      = sj.get("raw_summary", "")
            d["priority_actions"] = sj.get("priority_actions", [])[:2]
            d["referral_count"]   = len(sj.get("referral_mentions", []))
        except Exception:
            pass
        del d["structured_json"]
        result.append(d)
    return jsonify(result)


@reports_bp.route("/api/reports/daily/<int:rid>", methods=["GET"])
def get_daily_report(rid):
    conn = get_db()
    row  = conn.execute("SELECT * FROM daily_reports WHERE id=?", (rid,)).fetchone()
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
# 週報 API（API不要）
# ═══════════════════════════════════════════════

@reports_bp.route("/api/reports/weekly", methods=["POST"])
def generate_weekly():
    data     = request.json
    end_date = data.get("end_date", datetime.now().strftime("%Y-%m-%d"))
    end_dt   = datetime.strptime(end_date, "%Y-%m-%d")
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

    structured = _aggregate_free(daily_data, start_date, end_date, "weekly")

    conn = get_db()
    cur  = conn.execute(
        "INSERT INTO aggregate_reports(type, start_date, end_date, analysis_json) VALUES(?,?,?,?)",
        ("weekly", start_date, end_date, json.dumps(structured, ensure_ascii=False)),
    )
    conn.commit()
    structured["id"] = cur.lastrowid
    conn.close()
    return jsonify(structured)


# ═══════════════════════════════════════════════
# 月報 API（API不要）
# ═══════════════════════════════════════════════

@reports_bp.route("/api/reports/monthly", methods=["POST"])
def generate_monthly():
    data     = request.json
    end_date = data.get("end_date", datetime.now().strftime("%Y-%m-%d"))
    end_dt   = datetime.strptime(end_date, "%Y-%m-%d")
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

    structured = _aggregate_free(daily_data, start_date, end_date, "monthly")

    conn = get_db()
    cur  = conn.execute(
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
    rtype  = request.args.get("type", "")
    conn   = get_db()
    q      = "SELECT id, type, start_date, end_date, analysis_json, created_at FROM aggregate_reports"
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
            d["kpi_summary"]  = aj.get("kpi_summary", {})
            d["summary_text"] = aj.get("weekly_summary") or aj.get("monthly_summary", "")
        except Exception:
            pass
        del d["analysis_json"]
        result.append(d)
    return jsonify(result)


@reports_bp.route("/api/reports/aggregate/<int:rid>", methods=["GET"])
def get_aggregate_report(rid):
    conn = get_db()
    row  = conn.execute("SELECT * FROM aggregate_reports WHERE id=?", (rid,)).fetchone()
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
