"""Seed the "Smile Dental" demo workspace used by the web app's mock data.

    python -m scripts.seed_demo            # creates it (idempotent: skips if the slug exists)
    python -m scripts.seed_demo --reset    # deletes and recreates it

Login: priya@smiledental.in / demo-password-123 (plus arjun@, neha@, vikram@ with the same password).
"""

import argparse
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from app.core.security import hash_password
from app.db.base import utcnow
from app.db.session import dispose_engine, tenant_session
from app.models import (
    AgentConfig,
    Appointment,
    Channel,
    ConsentRecord,
    Contact,
    Conversation,
    Deal,
    EscalationRule,
    Location,
    Membership,
    Message,
    Notification,
    Organization,
    Payment,
    Playbook,
    Provider,
    Service,
    Task,
    User,
)
from app.models.crm import DEAL_PROBABILITY
from app.services import knowledge

SLUG = "smile-dental"
PASSWORD = "demo-password-123"
TZ = ZoneInfo("Asia/Kolkata")

TEAM = [
    ("u1", "Priya Sharma", "owner", "priya@smiledental.in"),
    ("u2", "Arjun Patil", "agent", "arjun@smiledental.in"),
    ("u3", "Neha Kulkarni", "manager", "neha@smiledental.in"),
    ("u4", "Vikram Joshi", "agent", "vikram@smiledental.in"),
]

# id, name, phone, email, company, city, stage, source, channel, tags, owner, score, value(₹), days_ago, language
CONTACTS = [
    ("c1", "Ananya Rao", "+91 98220 11223", "ananya.rao@gmail.com", None, "Pune", "booked", "Google Ads", "whatsapp", ["whitening", "new patient"], None, 86, 12000, 1, "English"),
    ("c2", "Rohit Mehta", "+91 99700 44556", "rohit.m@outlook.com", None, "Pune", "customer", "Walk-in", "sms", ["root canal", "complaint"], "u2", 64, 18500, 45, "Hindi"),
    ("c3", "Sneha Iyer", "+91 90110 77889", "sneha.iyer@yahoo.com", None, "Pune", "qualified", "Website chat", "webchat", ["braces", "teen"], None, 78, 45000, 0, "English"),
    ("c4", "Karan Desai", "+91 98500 22334", None, None, "Pune", "new", "Instagram ad", "instagram", ["implants"], None, 71, 60000, 0, "English"),
    ("c5", "Meera Nair", "+91 97300 55667", "meera.nair@gmail.com", None, "Pune", "customer", "Referral", "whatsapp", ["cleaning", "vip"], "u3", 92, 8000, 239, "Malayalam"),
    ("c6", "Aditya Kulkarni", "+91 98810 99001", "aditya.k@gmail.com", None, "Mumbai", "lost", "Google Business", "google", ["price shopper"], "u4", 22, 15000, 17, "Marathi"),
    ("c7", "Fatima Shaikh", "+91 90290 33445", None, None, "Pune", "booked", "Missed call", "voice", ["toothache", "urgent"], None, 81, 3500, 0, "Hindi"),
    ("c8", "Rahul Verma", "+91 99230 66778", "rahul@agrigrow.in", "AgriGrow Inputs", "Nashik", "prospect", "Prospecting", "email", ["b2b", "dealer"], "u1", 74, 240000, 7, "English"),
    ("c9", "Pooja Bhatt", "+91 98670 12121", "pooja.bhatt@gmail.com", None, "Pune", "qualified", "Facebook", "facebook", ["aligners"], None, 69, 85000, 2, "Gujarati"),
    ("c10", "Suresh Reddy", "+91 90000 34343", "suresh@kisanmart.co", "Kisan Mart", "Hyderabad", "prospect", "Prospecting", "linkedin", ["b2b"], "u1", 58, 180000, 6, "Telugu"),
    ("c11", "Divya Menon", "+91 97650 88990", "divya.menon@gmail.com", None, "Pune", "customer", "Google Ads", "email", ["whitening"], "u2", 88, 12000, 101, "English"),
    ("c12", "Imran Khan", "+91 98234 56565", None, None, "Pune", "new", "Website chat", "webchat", ["kids dentistry"], None, 55, 4000, 2, "Hindi"),
    ("c13", "Tanvi Gokhale", "+91 98901 45612", "tanvi.gokhale@gmail.com", None, "Pune", "booked", "Google Business", "whatsapp", ["cleaning", "new patient"], None, 77, 2500, 1, "Marathi"),
    ("c14", "Kunal Shah", "+91 99225 78123", "kunal.shah@icloud.com", None, "Pune", "qualified", "Google Ads", "whatsapp", ["implants", "emi"], "u2", 83, 120000, 5, "Gujarati"),
    ("c15", "Riya Kapoor", "+91 97640 33218", "riya.kapoor@gmail.com", None, "Pune", "customer", "Instagram ad", "instagram", ["aligners", "vip"], "u3", 90, 92000, 135, "Hindi"),
    ("c16", "Vivek Bhosale", "+91 98221 90876", None, None, "Pune", "new", "Missed call", "voice", ["toothache"], None, 62, 3500, 0, "Marathi"),
    ("c17", "Zoya Mirza", "+91 90960 11290", "zoya.mirza@outlook.com", None, "Pune", "qualified", "Website chat", "webchat", ["whitening", "wedding"], None, 80, 14000, 3, "Hindi"),
    ("c18", "Lakshmi Narayanan", "+91 94440 21876", "lakshmi.n@gmail.com", None, "Pune", "customer", "Referral", "sms", ["root canal"], "u4", 71, 21000, 85, "Tamil"),
    ("c19", "Aarav Joshi", "+91 98508 66104", None, None, "Pune", "lost", "Facebook", "facebook", ["braces", "price shopper"], None, 31, 45000, 22, "Marathi"),
    ("c20", "Ishita Sen", "+91 98300 45091", "ishita.sen@gmail.com", None, "Pune", "booked", "Google Ads", "whatsapp", ["kids dentistry"], None, 76, 1600, 4, "Bengali"),
    ("c21", "Sanjay Patil", "+91 98230 77412", "sanjay@krishisevakendra.in", "Krishi Seva Kendra", "Ahmednagar", "qualified", "Prospecting", "whatsapp", ["b2b", "dealer", "fertiliser"], "u1", 81, 320000, 13, "Marathi"),
    ("c22", "Manoj Jadhav", "+91 97302 18845", "manoj@shetkariagro.com", "Shetkari Agro", "Kolhapur", "prospect", "Prospecting", "email", ["b2b", "dealer"], None, 66, 150000, 6, "Marathi"),
    ("c23", "Kavita Deshmukh", "+91 94220 63390", "kavita@greenfieldagro.in", "GreenField Agro", "Chh. Sambhajinagar", "booked", "Webinar", "email", ["b2b", "seeds"], "u1", 79, 275000, 19, "Marathi"),
    ("c24", "Harish Gupta", "+91 98260 55107", "harish@bharatkrishi.in", "Bharat Krishi Kendra", "Indore", "customer", "Referral", "whatsapp", ["b2b", "dealer", "annual plan"], "u1", 87, 410000, 90, "Hindi"),
    ("c25", "Nitin Shinde", "+91 90110 42277", "nitin@jaikisaninputs.com", "Jai Kisan Inputs", "Solapur", "prospect", "Prospecting", "linkedin", ["b2b"], None, 49, 120000, 8, "Marathi"),
    ("c26", "Ganesh Pawar", "+91 98812 30419", "ganesh@sahyadriagri.in", "Sahyadri Agri Traders", "Satara", "lost", "Trade show", "voice", ["b2b", "budget"], "u1", 28, 95000, 41, "Marathi"),
]

SERVICES = [
    ("sv1", "Consultation", 20, 500, 0, True),
    ("sv2", "Whitening consultation", 30, 500, 500, True),
    ("sv3", "Laser whitening", 60, 12000, 2000, True),
    ("sv4", "Cleaning & polishing", 45, 2500, 0, True),
    ("sv5", "Root canal (per sitting)", 90, 8500, 1000, False),
    ("sv6", "Implant consultation + CBCT", 45, 1500, 500, True),
    ("sv7", "Ortho assessment", 45, 1000, 500, True),
    ("sv8", "Kids check-up", 30, 800, 0, True),
    ("sv9", "Emergency visit", 30, 1000, 300, True),
]

PROVIDERS = [
    ("Dr. Aditi Kulkarni", "General & cosmetic", ["kp", "baner"], ["sv1", "sv2", "sv3", "sv4", "sv5", "sv6", "sv9"]),
    ("Dr. Sameer Mehra", "Orthodontist", ["baner"], ["sv1", "sv7"]),
    ("Dr. Nisha Shah", "Paediatric dentist", ["kp"], ["sv1", "sv8"]),
    ("Ritu Pawar", "Hygienist", ["kp", "baner"], ["sv4"]),
]

FAQS = [
    ("How much does teeth whitening cost?", "In-clinic laser whitening costs ₹8,000–₹12,000 depending on the shade, and takes about 60 minutes. A whitening consultation is ₹500 (refundable deposit)."),
    ("Do you offer EMI?", "Yes. 0% EMI over 6 months via Bajaj Finserv for treatments above ₹15,000 (implants, aligners, braces)."),
    ("How much do braces and aligners cost?", "Ceramic braces start at ₹45,000 and clear aligners at ₹85,000. Teen braces usually take 12–18 months."),
    ("How much does an implant cost?", "A single implant with crown is ₹45,000–₹60,000 depending on the implant brand. The implant consultation with CBCT scan is ₹1,500."),
    ("What are your opening hours?", "Koregaon Park: Mon–Sat 9 AM–8 PM, Sun 10 AM–2 PM. Baner: Mon–Sat 10 AM–8 PM."),
    ("Do you have a kids dentist?", "Yes. Our paediatric dentist Dr. Nisha Shah sees children at Koregaon Park on Tuesdays and Saturdays."),
    ("What is your cancellation policy?", "Free cancellation or rescheduling up to 4 hours before the appointment. Deposits are refundable or carried over."),
]

PLAYBOOKS = [
    ("Qualify lead", ["new contact", "unknown intent"], "Greet warmly in the customer's language. Ask at most 2 questions per message. Find out which treatment they need, which branch is closer (Koregaon Park or Baner) and how soon. If they mention pain or swelling, treat it as urgent and offer the earliest emergency slot.", ["search_knowledge", "update_contact", "handoff_to_human"]),
    ("Book appointment", ["book", "reschedule", "slot"], "Always call check_availability before offering times. Offer at most 3 slots. Confirm the slot back to the customer before calling book_appointment. For whitening and implant consults, send the ₹500 refundable deposit link after booking.", ["check_availability", "book_appointment", "create_payment_link", "update_contact", "handoff_to_human"]),
    ("Answer FAQ", ["price", "hours", "location", "policy"], "Only answer from search_knowledge results and mention the source. If nothing relevant is found, say you'll check with the team and hand off. Never give medical diagnoses.", ["search_knowledge", "handoff_to_human"]),
    ("Handle complaint", ["complaint", "refund", "negative"], "Apologise sincerely, don't argue or admit liability. Collect the visit date and what went wrong, then hand off to the clinic manager with a 3-line summary. Never promise refunds.", ["update_contact", "create_task", "handoff_to_human"]),
]


def at(days_ago: float = 0, hour: int | None = None, minute: int = 0) -> datetime:
    t = utcnow() - timedelta(days=days_ago)
    if hour is not None:
        t = t.astimezone(TZ).replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(UTC)
    return t


async def seed(reset: bool) -> None:
    async with tenant_session(bypass_rls=True) as db:
        existing = await db.scalar(select(Organization).where(Organization.slug == SLUG))
        if existing and not reset:
            print(f"Workspace '{SLUG}' already exists; use --reset to recreate it.")
            return
        if existing:
            await db.execute(delete(Organization).where(Organization.id == existing.id))
        emails = [t[3] for t in TEAM]
        await db.execute(delete(User).where(User.email.in_(emails)))
        await db.commit()

    org_id = uuid.uuid4()
    async with tenant_session(org_id) as db:
        org = Organization(
            id=org_id, name="Smile Dental", slug=SLUG, timezone="Asia/Kolkata", currency="INR", country="IN",
            industry="dental clinic", plan="growth", settings={},
            business_profile={
                "description": "Two-location dental clinic in Pune: cosmetic, orthodontics, implants and kids dentistry.",
                "website": "https://smiledental.in", "phone": "+91 20 6712 3400",
                "address": "Lane 6, North Main Rd, Koregaon Park, Pune 411001",
                "hours": "KP Mon–Sat 9–8, Sun 10–2 · Baner Mon–Sat 10–8",
                "policies": "Free rescheduling up to 4 hours before. Deposits are refundable.",
            },
        )
        db.add(org)
        await db.flush()
        users: dict[str, uuid.UUID] = {}
        pw = hash_password(PASSWORD)
        for key, name, role, email in TEAM:
            u = User(email=email, name=name, password_hash=pw, last_seen_at=utcnow() if key != "u3" else at(1))
            db.add(u)
            await db.flush()
            users[key] = u.id
            db.add(Membership(org_id=org_id, user_id=u.id, role=role, location_ids=[]))

        kp_hours = [{"day": d, "open": True, "from": "09:00", "to": "20:00"} for d in range(6)] + [
            {"day": 6, "open": True, "from": "10:00", "to": "14:00"}]
        baner_hours = [{"day": d, "open": True, "from": "10:00", "to": "20:00"} for d in range(6)] + [
            {"day": 6, "open": False, "from": "10:00", "to": "14:00"}]
        locs = {
            "kp": Location(org_id=org_id, name="Koregaon Park", address="Lane 6, North Main Rd, Koregaon Park, Pune 411001",
                           phone="+91 20 6712 3400", working_hours=kp_hours),
            "baner": Location(org_id=org_id, name="Baner", address="2nd floor, Pancard Club Rd, Baner, Pune 411045",
                              phone="+91 20 6712 3411", working_hours=baner_hours),
        }
        db.add_all(locs.values())
        svcs = {k: Service(org_id=org_id, name=n, duration_min=d, price_minor=p * 100, deposit_minor=dep * 100, online=o)
                for k, n, d, p, dep, o in SERVICES}
        db.add_all(svcs.values())
        await db.flush()
        provs = []
        for name, title, loc_keys, svc_keys in PROVIDERS:
            p = Provider(org_id=org_id, name=name, title=title, location_ids=[locs[k].id for k in loc_keys],
                         service_ids=[svcs[k].id for k in svc_keys])
            db.add(p)
            provs.append(p)

        db.add(AgentConfig(
            org_id=org_id, name="Anvita", tone="Warm, reassuring and concise, like a senior front-desk coordinator",
            languages=["English", "Hindi", "Marathi"], default_mode="autopilot",
            guardrails={"max_discount_pct": 10, "max_payment_minor": 500000, "blocked_topics": ["medical diagnosis"]},
            instructions="Patients are often anxious about pain and cost: acknowledge it before answering.",
        ))
        for i, (name, triggers, instr, tools) in enumerate(PLAYBOOKS):
            db.add(Playbook(org_id=org_id, name=name, triggers=triggers, instructions=instr, tools=tools, channels=[],
                            priority=(i + 1) * 10))
        db.add_all([
            EscalationRule(org_id=org_id, name="Negative sentiment", kind="sentiment", condition={"sentiment": "negative"},
                           action="assign", assign_to_id=users["u3"]),
            EscalationRule(org_id=org_id, name="Complaint or refund intent", kind="intent",
                           condition={"intents": ["complaint", "refund"]}, action="assign", assign_to_id=users["u3"]),
            EscalationRule(org_id=org_id, name="VIP contact", kind="vip", condition={"tags": ["vip"]}, action="notify",
                           assign_to_id=users["u1"]),
            EscalationRule(org_id=org_id, name="Sensitive keywords", kind="keyword",
                           condition={"keywords": ["lawyer", "consumer court", "police"]}, action="assign",
                           assign_to_id=users["u2"]),
            EscalationRule(org_id=org_id, name="Asks for a human", kind="keyword",
                           condition={"keywords": ["talk to someone", "real person", "insaan se baat", "call me"]},
                           action="assign"),
        ])
        channels = {
            "webchat": Channel(org_id=org_id, type="webchat", name="Website chat", agent_mode="autopilot"),
            "whatsapp": Channel(org_id=org_id, type="whatsapp", name="WhatsApp +91 20 6712 3400",
                                external_id="demo-phone-number-id", agent_mode="autopilot", status="disconnected"),
            "email": Channel(org_id=org_id, type="email", name="hello@smiledental.in", agent_mode="copilot",
                             status="disconnected"),
        }
        db.add_all(channels.values())

        contacts: dict[str, Contact] = {}
        for (cid, name, phone, email, company, city, stage, source, channel, tags, owner, score, value, days,
             lang) in CONTACTS:
            c = Contact(org_id=org_id, name=name, phone=phone.replace(" ", ""), email=email, company=company, city=city,
                        stage=stage, source=source, channel=channel, tags=tags, owner_id=users.get(owner) if owner else None,
                        score=score, value_minor=value * 100, language=lang, utm={}, attributes={},
                        created_at=at(days), last_activity_at=at(min(days, 2) / 3))
            db.add(c)
            contacts[cid] = c
        await db.flush()
        for cid in ("c1", "c5", "c13", "c20", "c24"):
            db.add(ConsentRecord(org_id=org_id, contact_id=contacts[cid].id, channel="whatsapp", purpose="marketing",
                                 status="opted_in", source="Booking form", captured_at=contacts[cid].created_at))

        async def thread(cid: str, channel: str, msgs: list[tuple], *, assignee: str | None = None, status: str = "open",
                         intent: str | None = None, sentiment: str = "neutral", summary: str | None = None,
                         escalation: str | None = None, unread: int = 0, minutes_ago: int = 5) -> Conversation:
            end = utcnow() - timedelta(minutes=minutes_ago)
            conv = Conversation(org_id=org_id, contact_id=contacts[cid].id, channel_type=channel,
                                channel_id=channels[channel].id if channel in channels else None, status=status,
                                assignee_type="human" if assignee else "ai", assignee_id=users.get(assignee or ""),
                                intent=intent, sentiment=sentiment, ai_summary=summary, escalation_reason=escalation,
                                unread_count=unread)
            db.add(conv)
            await db.flush()
            for i, (author, body, *extra) in enumerate(msgs):
                ts = end - timedelta(minutes=2 * (len(msgs) - 1 - i))
                opts = extra[0] if extra else {}
                db.add(Message(org_id=org_id, conversation_id=conv.id, created_at=ts, author_type=author, body=body,
                               direction="inbound" if author == "contact" else "internal" if opts.get("note") else "outbound",
                               author_name=opts.get("name") or ("Anvita" if author == "ai" else None),
                               is_note=opts.get("note", False), card=opts.get("card"), sources=opts.get("sources", []),
                               attachments=[], status="received" if author == "contact" else "read"))
                if author == "contact":
                    conv.last_inbound_at = ts
            conv.last_message_at = end
            conv.last_message_preview = msgs[-1][1][:140]
            return conv

        await thread("c1", "whatsapp", [
            ("contact", "Hi, do you do teeth whitening? How much does it cost?"),
            ("ai", "Hi Ananya! 😊 Yes, we offer in-clinic laser whitening. It usually costs ₹8,000–₹12,000 depending on the shade you want, and takes about 60 minutes. Would you like to book a consultation first?", {"sources": ["How much does teeth whitening cost?"]}),
            ("contact", "Yes please. Saturday morning works"),
            ("ai", "Great! I have 10:00 AM or 11:30 AM this Saturday at our Koregaon Park clinic. Which works better?"),
            ("contact", "10 is good"),
            ("ai", "You're booked ✅ To confirm your slot, please pay a refundable ₹500 deposit.", {"card": {"type": "booking", "title": "Whitening consultation", "subtitle": "Sat · 10:00 AM · Koregaon Park", "status": "Confirmed"}}),
            ("contact", "Done, paid. Perfect, see you Saturday at 10!"),
        ], intent="Book appointment", sentiment="positive", unread=2, minutes_ago=2,
           summary="New patient asking about teeth whitening. Shared price range ₹8k–12k, booked consultation Sat 10:00 AM at Koregaon Park, ₹500 deposit paid via UPI.")
        await thread("c2", "sms", [
            ("contact", "My temporary filling came off again. This is the second time the filling has come off."),
            ("ai", "I'm really sorry about this, Rohit. That shouldn't happen. I'm bringing in Arjun from our front desk right now to get you a priority appointment today."),
            ("system", "Escalated to Arjun Patil: negative sentiment + complaint intent"),
            ("human", "Note: Dr. Kulkarni can see him at 4:30 PM today. Offer it, no charge.", {"note": True, "name": "Arjun Patil"}),
        ], assignee="u2", intent="Complaint", sentiment="negative", escalation="Negative sentiment · complaint", unread=1,
           minutes_ago=6, summary="Existing patient (root canal, Sept). Temporary filling came off for the 2nd time; frustrated. Needs a priority slot today.")
        await thread("c3", "webchat", [
            ("contact", "How long do braces take for a 14 year old? And do you have the clear ones?"),
            ("ai", "Braces for teens usually take 12–18 months. Yes, we offer ceramic (tooth-coloured) braces from ₹45,000 and clear aligners from ₹85,000. Shall I book an orthodontic assessment with Dr. Mehra?", {"sources": ["How much do braces and aligners cost?"]}),
        ], intent="Pricing question", minutes_ago=14,
           summary="Parent asking about braces for 14-year-old. Interested in ceramic. Wants weekday evening slot.")
        await thread("c4", "instagram", [
            ("contact", "Saw your reel. How much for a single implant?"),
            ("ai", "Thanks for reaching out, Karan! A single implant with crown is ₹45,000–₹60,000 depending on the implant brand.", {"sources": ["How much does an implant cost?"]}),
            ("contact", "Is implant EMI available?"),
        ], intent="Financing", unread=3, minutes_ago=32, summary="Asked about implant cost and EMI. Awaiting reply.")
        await thread("c5", "whatsapp", [
            ("ai", "Hi Meera, thanks for visiting today! Would you mind sharing your experience? It really helps us 🙏", {"card": {"type": "review", "title": "Review us on Google", "subtitle": "Takes 30 seconds"}}),
            ("contact", "Thank you! Left a review 🙏"),
        ], assignee="u3", status="closed", intent="Review", sentiment="positive", minutes_ago=60,
           summary="VIP patient, cleaning completed. Left a 5★ Google review.")
        await thread("c8", "email", [
            ("human", "Hi Rahul, noticed AgriGrow added 2 new outlets in Nashik this year. Congrats! We help dealers cut stock-outs with demand forecasting…", {"name": "Priya Sharma (sequence)"}),
            ("contact", "Interesting. Can we talk Thursday afternoon?"),
        ], assignee="u1", intent="Interested (outbound reply)", sentiment="positive", unread=1, minutes_ago=180,
           summary="Reply to sequence 'Agri dealers, Maharashtra' step 2. Interested; asked for a call Thursday.")
        await thread("c12", "webchat", [
            ("contact", "Do you have a kids dentist?"),
            ("ai", "Yes! Our paediatric dentist Dr. Shah is available on Tuesdays and Saturdays. Want me to check a slot?", {"sources": ["Do you have a kids dentist?"]}),
        ], status="closed", intent="Kids dentistry", minutes_ago=2880, summary="Asked about kids' dentist availability. No booking yet.")

        # Appointments over the coming days, placed on valid working hours.
        base = utcnow().astimezone(TZ).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)
        if base.weekday() == 6:
            base += timedelta(days=1)
        plan = [("c1", "sv2", 0, "kp", 0, "ai"), ("c13", "sv4", 3, "kp", 1, "online"), ("c20", "sv8", 2, "kp", 2, "ai"),
                ("c3", "sv7", 1, "baner", 4, "staff"), ("c7", "sv9", 0, "baner", 0, "ai")]
        for i, (cid, sk, prov_i, lk, day, source) in enumerate(plan):
            start = (base + timedelta(days=day, hours=i)).astimezone(UTC)
            db.add(Appointment(org_id=org_id, contact_id=contacts[cid].id, service_id=svcs[sk].id,
                               provider_id=provs[prov_i].id, location_id=locs[lk].id, starts_at=start,
                               ends_at=start + timedelta(minutes=svcs[sk].duration_min), status="confirmed", source=source))

        for cid, title, value, stage, owner, pipeline in [
            ("c1", "Laser whitening package", 12000, "booked", None, "patients"),
            ("c3", "Ceramic braces (teen)", 45000, "qualified", None, "patients"),
            ("c4", "Single implant + crown", 60000, "new", None, "patients"),
            ("c9", "Clear aligners", 85000, "qualified", None, "patients"),
            ("c14", "Full-arch implants", 120000, "proposal", "u2", "patients"),
            ("c15", "Aligners (Invisalign Lite)", 92000, "won", "u3", "patients"),
            ("c19", "Braces", 45000, "lost", None, "patients"),
            ("c8", "AgriGrow: 3-outlet annual plan", 240000, "booked", "u1", "b2b"),
            ("c21", "Krishi Seva Kendra: rabi rollout", 320000, "proposal", "u1", "b2b"),
            ("c24", "Bharat Krishi Kendra: annual plan", 410000, "won", "u1", "b2b"),
        ]:
            db.add(Deal(org_id=org_id, contact_id=contacts[cid].id, title=title, value_minor=value * 100, stage=stage,
                        probability=DEAL_PROBABILITY[stage], owner_id=users.get(owner or ""), pipeline=pipeline,
                        closed_at=utcnow() if stage in ("won", "lost") else None,
                        lost_reason="Chose a cheaper clinic" if stage == "lost" else None))

        for title, ttype, cid, assignee, due_h, prio, by_ai, ctx_text in [
            ("Call Rohit Mehta: confirm 4:30 PM priority slot", "call", "c2", "u2", 1, "high", True, "Rohit: “This is the second time the filling has come off.”"),
            ("Approve Copilot reply to Rahul Verma with Thursday slots", "email", "c8", "u1", -1, "high", True, "Rahul replied “Can we talk Thursday afternoon?”"),
            ("Call Kunal Shah about Bajaj Finserv EMI approval", "call", "c14", "u2", -18, "high", True, "Kunal: “Call me back after 5, I'll have the EMI approval by then.”"),
            ("Send revised 3-outlet pricing to Sanjay Patil", "email", "c21", "u1", 5, "high", False, None),
            ("Follow up with Pooja Bhatt on the aligner brochure", "whatsapp", "c9", None, 48, "normal", True, "Pooja opened the brochure twice."),
            ("Send Zoya Mirza the wedding whitening timeline", "whatsapp", "c17", None, 72, "normal", True, "Zoya: “My wedding is on 12 Dec, is that enough time?”"),
        ]:
            db.add(Task(org_id=org_id, title=title, type=ttype, contact_id=contacts[cid].id, assignee_id=users.get(assignee or ""),
                        created_by_type="ai" if by_ai else "user", created_by_id=None if by_ai else users["u1"],
                        due_at=utcnow() + timedelta(hours=due_h), priority=prio, ai_context=ctx_text,
                        ai_confidence=90 if by_ai else None))

        for i, (cid, amount, purpose, status, days_ago, method) in enumerate([
            ("c1", 500, "Whitening consultation deposit", "paid", 0, "upi"),
            ("c15", 92000, "Invisalign Lite treatment", "paid", 30, "card"),
            ("c11", 12000, "Laser whitening", "paid", 12, "upi"),
            ("c2", 8500, "Root canal sitting 2", "pending", 25, None),
            ("c7", 300, "Emergency visit deposit", "pending", 0, None),
            ("c24", 410000, "Annual plan (3 outlets)", "paid", 20, "netbanking"),
        ]):
            created = at(days_ago)
            db.add(Payment(org_id=org_id, contact_id=contacts[cid].id, number=f"INV-2026-{1040 + i}",
                           amount_minor=amount * 100, currency="INR", purpose=purpose, status=status, method=method,
                           provider="test", provider_order_id=f"test_seed_{i}", public_token=f"demo-{uuid.uuid4().hex}",
                           created_by_type="ai" if amount < 1000 else "user", lines=[], created_at=created,
                           paid_at=created + timedelta(minutes=2) if status == "paid" else None,
                           due_at=created + timedelta(days=7) if status == "pending" else None))

        db.add_all([
            Notification(org_id=org_id, user_id=users["u2"], tab="escalations", icon="escalation", priority=True,
                         title="Rohit Mehta needs a human", body="Negative sentiment · complaint",
                         contact_id=contacts["c2"].id, link="/inbox"),
            Notification(org_id=org_id, tab="ai", icon="booking", title="AI booked Ananya Rao",
                         body="Whitening consultation · Sat 10:00 AM · Koregaon Park", contact_id=contacts["c1"].id),
            Notification(org_id=org_id, tab="system", icon="payment", title="Payment received · ₹500",
                         body="Whitening consultation deposit", contact_id=contacts["c1"].id),
        ])
        await db.flush()
        for q, a in FAQS:
            await knowledge.add_faq(db, org_id, q, a)
        await db.commit()
    print(f"Seeded '{SLUG}'. Sign in as priya@smiledental.in / {PASSWORD}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    try:
        await seed(args.reset)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
