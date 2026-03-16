import anthropic
import base64
import json
import re
import os
from dotenv import load_dotenv

load_dotenv()
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


AD_SPEND_PROMPT = """Extract all rows from this Facebook Ads Manager report screenshot.
This is a digital screenshot with clear printed text, not handwritten.
For each row return: name (the campaign/adset name exactly as shown), spend (number only, no currency or commas), results (integer), cost_per_result (number only).
Rules:
- Remove commas from numbers (e.g. 1,240.50 → 1240.50)
- If a value shows "--" or is empty → set it to 0
- Skip any summary/total rows
- Return ONLY a valid JSON array, no extra text
Example: [{"name": "Ahmed - Leads", "spend": 1240.50, "results": 45, "cost_per_result": 27.57}]"""


async def extract_ad_spend_from_image(image_bytes: bytes, media_type: str) -> list:
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": AD_SPEND_PROMPT}
            ]
        }]
    )
    raw = message.content[0].text.strip()
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        raise ValueError("Could not extract JSON from response")
    rows = json.loads(match.group())
    for row in rows:
        row["spend"] = float(row.get("spend") or 0)
        row["results"] = int(row.get("results") or 0)
        row["cost_per_result"] = float(row.get("cost_per_result") or 0)
    return rows


def compute_metrics(agents, leads, rdvs, spend_rows):
    """Pure computation — no DB calls. Returns list of agent metric dicts."""
    results = []
    for agent in agents:
        aid = agent["id"]

        a_leads = [l for l in leads if l.get("original_agent") == aid]
        total   = len(a_leads)
        rdv_c   = sum(1 for l in a_leads if l["status"] == "RDV")
        bv_c    = sum(1 for l in a_leads if l["status"] == "B.V")
        nr_c    = sum(1 for l in a_leads if l["status"] == "N.R")
        pi_c    = sum(1 for l in a_leads if l["status"] == "P.I")
        av_c    = sum(1 for l in a_leads if l["status"] == "Autre ville")

        a_rdvs      = [r for r in rdvs if r.get("agent_id") == aid]
        rdv_booked  = len(a_rdvs)
        showed      = sum(1 for r in a_rdvs if r["status"] == "showed_up")
        registered  = sum(1 for l in leads if l.get("current_agent") == aid
                          and l["status"] in ("registered_logha", "registered_maharat", "registered_takwin"))

        a_spend = [s for s in spend_rows if s.get("agent_id") == aid]
        spend        = sum(s["spend"] for s in a_spend)
        ad_results   = sum(s["ad_results"] for s in a_spend)

        rdv_rate  = round(rdv_c / total * 100, 1)     if total      else 0
        show_rate = round(showed / rdv_booked * 100, 1) if rdv_booked else 0
        reg_rate  = round(registered / showed * 100, 1) if showed    else 0

        cpl       = round(spend / total, 0)       if total and spend       else 0
        cpr       = round(spend / ad_results, 0)  if ad_results and spend  else 0
        cp_rdv    = round(spend / rdv_c, 0)        if rdv_c and spend       else 0
        cp_show   = round(spend / showed, 0)       if showed and spend      else 0
        cp_reg    = round(spend / registered, 0)   if registered and spend  else 0

        results.append({
            "agent_id": aid, "agent_name": agent["name"],
            "total_leads": total, "rdv_count": rdv_c,
            "bv_count": bv_c, "nr_count": nr_c, "pi_count": pi_c, "av_count": av_c,
            "rdv_booked": rdv_booked, "showed_up": showed, "registered": registered,
            "spend": spend, "ad_results": ad_results,
            "rdv_rate": rdv_rate, "show_rate": show_rate, "reg_rate": reg_rate,
            "cpl": cpl, "cost_per_result": cpr,
            "cost_per_rdv": cp_rdv, "cost_per_show": cp_show, "cost_per_registration": cp_reg,
        })
    return results


def compute_warnings(agents_metrics):
    """Rule-based warnings. Returns list of {agent_name, warnings:[]}."""
    active = [a for a in agents_metrics if a["total_leads"] > 0]
    cpls   = [a["cpl"] for a in active if a["cpl"] > 0]
    avg_cpl = sum(cpls) / len(cpls) if cpls else 0

    output = []
    for a in active:
        ws = []
        total = a["total_leads"]

        # Critical
        if a["rdv_booked"] >= 5 and a["showed_up"] == 0:
            ws.append({"severity": "critical", "text": f"لديه {a['rdv_booked']} موعد RDV بدون أي حضور — مواعيد وهمية محتملة"})
        if total >= 15 and a["rdv_count"] == 0:
            ws.append({"severity": "critical", "text": f"{total} عميل بدون أي موعد RDV — مشكلة في الإقناع"})
        if avg_cpl > 0 and a["cpl"] > avg_cpl * 3 and a["cpl"] > 0:
            ws.append({"severity": "critical", "text": f"تكلفة عميل {a['cpl']:.0f}دم — أكثر من 3× متوسط الفريق ({avg_cpl:.0f}دم)"})

        # Warning
        if total > 0:
            av_pct = a["av_count"] / total * 100
            nr_pct = a["nr_count"] / total * 100
            pi_pct = a["pi_count"] / total * 100
            if av_pct > 35:
                ws.append({"severity": "warning", "text": f"{av_pct:.0f}% من عملائه من مدن أخرى ({a['av_count']} عميل)"})
            if nr_pct > 45:
                ws.append({"severity": "warning", "text": f"معدل رفض مرتفع: {nr_pct:.0f}% لم يردوا ({a['nr_count']} عميل)"})
            if pi_pct > 30:
                ws.append({"severity": "warning", "text": f"{pi_pct:.0f}% غير مهتمين — جودة الاستهداف ضعيفة"})
        if a["rdv_booked"] >= 3 and a["show_rate"] < 30:
            ws.append({"severity": "warning", "text": f"معدل حضور منخفض جداً: {a['show_rate']:.0f}% فقط"})
        if a["showed_up"] >= 3 and a["registered"] == 0:
            ws.append({"severity": "warning", "text": f"لا تسجيلات رغم {a['showed_up']} زيارة — مشكلة في الإغلاق"})
        if avg_cpl > 0 and a["cpl"] > avg_cpl * 2 and a["cpl"] > 0:
            ws.append({"severity": "info", "text": f"تكلفة عميل أعلى من الفريق: {a['cpl']:.0f} مقابل {avg_cpl:.0f}دم"})

        if ws:
            output.append({
                "agent_id": a["agent_id"], "agent_name": a["agent_name"],
                "warnings": ws,
                "critical": sum(1 for w in ws if w["severity"] == "critical"),
                "warning":  sum(1 for w in ws if w["severity"] == "warning"),
            })

    output.sort(key=lambda x: (x["critical"], x["warning"]), reverse=True)
    return output


async def ai_analyze_team(agents_metrics: list) -> str:
    active = [a for a in agents_metrics if a["total_leads"] > 0]
    if not active:
        return "لا توجد بيانات كافية للتحليل."

    rows = "\n".join([
        f"• {a['agent_name']}: {a['total_leads']} عميل | "
        f"RDV {a['rdv_rate']}% | حضور {a['show_rate']}% | تسجيل {a['reg_rate']}% | "
        f"تكلفة عميل {a['cpl']:.0f}دم | تكلفة تسجيل {a['cost_per_registration']:.0f}دم"
        for a in active
    ])

    prompt = f"""أنت خبير تسويق ومبيعات تحلل أداء فريق وكلاء مدرسة خاصة.

بيانات الفريق:
{rows}

قدم تحليلاً مختصراً يشمل:
1. أفضل وكيل في كل مرحلة (جلب عملاء / حجز RDV / إحضار الحضور / التسجيل)
2. الوكلاء الذين يحتاجون تحسيناً عاجلاً وفي أي مرحلة
3. مقترحات تعليمية: من يتعلم من من وفي أي مهارة
4. أهم 3 أولويات للمدير هذا الأسبوع

الرد بالعربية، منظم بنقاط واضحة، مختصر ومباشر."""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=700,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()


async def ai_analyze_agent(agent: dict, team_avg: dict) -> str:
    prompt = f"""أنت خبير مبيعات. حلل أداء هذا الوكيل بدقة.

الوكيل: {agent['agent_name']}
— {agent['total_leads']} عميل | RDV {agent['rdv_rate']}% | حضور {agent['show_rate']}% | تسجيل {agent['reg_rate']}%
— تكلفة عميل: {agent['cpl']:.0f}دم | تكلفة تسجيل: {agent['cost_per_registration']:.0f}دم
— B.V: {agent['bv_count']} | N.R: {agent['nr_count']} | P.I: {agent['pi_count']} | مدن أخرى: {agent['av_count']}

متوسط الفريق: RDV {team_avg.get('rdv_rate',0):.1f}% | حضور {team_avg.get('show_rate',0):.1f}% | تسجيل {team_avg.get('reg_rate',0):.1f}%

1. ما هي أكبر نقطة ضعف في قمعه؟
2. ماذا يجب أن يفعل بشكل مختلف هذا الأسبوع؟ (3 إجراءات محددة)
3. تقييم: ممتاز / جيد / يحتاج تحسين / حرج

الرد بالعربية، مختصر ومباشر، بدون مقدمات."""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()
