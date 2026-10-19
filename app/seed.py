"""Demo data: fictional students, suppliers, offers and groups in different states.

The first student (Alex, id 1) and first group (detergent, id 1) match the walkthrough.
Prices are stored pre-tax; all-in totals (8.25% tax, no fees) are noted beside each offer.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import core
from app.core import DEFAULT_START
from app.db import drop_all
from app.models import (
    Group,
    GroupMember,
    GroupStatus,
    Intent,
    IntentStatus,
    MemberStatus,
    Offer,
    Order,
    OrderStatus,
    Product,
    SavingsEntry,
    SimulatedPayment,
    Student,
    Supplier,
    Tier,
)

PICKUP = "Bloomberg Center Lobby"

STUDENTS = [  # key, name, verified
    ("alex", "Alex Chen", True),
    ("jordan", "Jordan Lee", True),
    ("priya", "Priya Nair", True),
    ("maya", "Maya Patel", True),
    ("leo", "Leo Garcia", True),
    ("sam", "Sam Rivera", False),
    ("noah", "Noah Williams", True),
    ("zoe", "Zoe Martin", True),
    ("ethan", "Ethan Brooks", True),
    ("olivia", "Olivia Johnson", True),
    ("lucas", "Lucas Wright", True),
    ("grace", "Grace Kim", True),
    ("daniel", "Daniel Osei", True),
    ("hannah", "Hannah Schultz", True),
    ("mia", "Mia Rossi", True),
    ("ben", "Ben Carter", True),
]

PRODUCTS = {  # key: (product_key, name, variants, keywords)
    "detergent": ("laundry-detergent", "Laundry Detergent", "liquid,pods", "detergent,laundry soap"),
    "towels": ("paper-towels", "Paper Towels (12 rolls)", "", "paper towel,kitchen roll"),
    "ramen": ("instant-ramen", "Instant Ramen (24-pack)", "", "ramen,instant noodle,noodle"),
    "lamp": ("desk-lamp", "LED Desk Lamp", "", "desk lamp,lamp"),
    "coffee": ("coffee-beans", "Coffee Beans (2 lb bag)", "", "coffee"),
    "jacket": ("winter-jacket", "Winter Jacket", "small,medium,large", "jacket,winter coat,coat,parka"),
    "toilet": ("toilet-paper", "Toilet Paper (24 rolls)", "", "toilet paper,toilet roll,loo roll"),
    "dish": ("dish-soap", "Dish Soap (3-pack)", "", "dish soap,dishwashing liquid,washing up liquid"),
    "printer": ("printer-paper", "Printer Paper (5 reams)", "", "printer paper,copy paper,paper ream"),
    "rice": ("rice", "Jasmine Rice (20 lb bag)", "", "rice"),
}

# key, supplier, product, brand, solo pre-tax, group pre-tax, min buyers, variant  (all-in in comments)
OFFERS = [
    ("ecoclean", "EcoClean Bulk Supply", "detergent", "EcoClean", 1663, 1178, 4, "liquid"),  # 18.00 / 12.75
    ("freshbulk", "FreshBulk Co", "detergent", "FreshBulk", 1663, 1285, 3, "liquid"),        # 18.00 / 13.91
    ("cleanpod", "CleanPod Co", "detergent", "CleanPod", 1940, 1478, 4, "pods"),             # 21.00 / 16.00
    ("campusmart_towels", "CampusMart", "towels", "CampusMart", 1290, 840, 3, None),         # 13.96 / 9.09
    ("paperplus_towels", "PaperPlus", "towels", "PaperPlus", 1290, 776, 3, None),            # 13.96 / 8.40
    ("noodlehouse", "NoodleHouse Wholesale", "ramen", "NoodleHouse", 1478, 1016, 4, None),   # 16.00 / 11.00
    ("brightdesk", "BrightDesk Supply", "lamp", "BrightDesk", 2402, 2217, 3, None),          # 26.00 / 24.00
    ("island", "Island Roasters", "coffee", "Island Roasters", 2679, 2125, 4, None),         # 29.00 / 23.00
    ("northpeak", "NorthPeak Outfitters", "jacket", "NorthPeak", 11085, 8222, 5, None),      # 120.00 / 89.00
    ("summit", "Summit Supply", "jacket", "Summit", 11085, 8776, 3, None),                   # 120.00 / 95.00
    ("pantrypal_tp", "PantryPal Wholesale", "toilet", "PantryPal", 2448, 1755, 3, None),     # 26.50 / 19.00
    ("pantrypal_dish", "PantryPal Wholesale", "dish", "PantryPal", 1201, 831, 3, None),      # 13.00 / 9.00
    ("campusmart_paper", "CampusMart", "printer", "CampusMart", 3233, 2587, 4, None),        # 35.00 / 28.00
    ("grainhouse", "GrainHouse", "rice", "GrainHouse", 2771, 2125, 3, None),                 # 30.00 / 23.00
]


# Hours from a group closing to the order being ready for pickup (default 24).
DELIVERY_HOURS = {"campusmart_towels": 12, "campusmart_paper": 12, "northpeak": 48, "summit": 48}


def end_of(d: datetime) -> datetime:
    return d.replace(hour=23, minute=59, second=0, microsecond=0)


def seed(session: Session, start: datetime = DEFAULT_START) -> dict:
    core.set_now(session, start)

    students = {}
    for key, name, verified in STUDENTS:
        first, last = name.lower().split()
        students[key] = Student(name=name, email=f"{first}.{last}@cornell.edu", email_verified=verified,
                                pickup_default=PICKUP, is_simulated=key != "alex")
    students["sam"].verification_code = "482913"
    session.add_all(students.values())

    products = {k: Product(key=pk, name=name, variants_csv=v, keywords_csv=kw)
                for k, (pk, name, v, kw) in PRODUCTS.items()}
    session.add_all(products.values())

    suppliers: dict[str, Supplier] = {}
    for _, sup, *_rest in OFFERS:
        suppliers.setdefault(sup, Supplier(name=sup))
    session.add_all(suppliers.values())
    session.flush()

    offers = {}
    for key, sup, prod, brand, solo, group, min_b, variant in OFFERS:
        offers[key] = Offer(supplier_id=suppliers[sup].id, product_id=products[prod].id, variant=variant,
                            brand=brand, solo_unit_cents=solo, group_unit_cents=group, min_buyers=min_b,
                            delivery_hours=DELIVERY_HOURS.get(key, 24), created_at=start - timedelta(days=30))
    session.add_all(offers.values())
    session.flush()

    friday = end_of(start + timedelta(days=(4 - start.weekday()) % 7))

    def intent(student: str, product: str, cap: int, needed_by: datetime, allow_switch: bool = True,
               variant: str | None = None, text: str = "", status: str = IntentStatus.WATCHING,
               created: datetime | None = None) -> Intent:
        i = Intent(student_id=students[student].id, product_id=products[product].id, variant=variant,
                   max_total_cents=cap, needed_by=needed_by, pickup=PICKUP, allow_switch=allow_switch,
                   permission_tier=Tier.AUTO_WITHIN_LIMITS, status=status, raw_text=text,
                   created_at=created or start - timedelta(hours=6))
        session.add(i)
        return i

    def group(offer_key: str, deadline: datetime, members: list[Intent], created: datetime | None = None,
              status: str = GroupStatus.FORMING) -> Group:
        o = offers[offer_key]
        g = Group(offer_id=o.id, product_id=o.product_id, variant=o.variant, deadline=deadline, pickup=PICKUP,
                  status=status, created_at=created or start - timedelta(hours=6))
        session.add(g)
        session.flush()
        for m in members:
            g.members.append(GroupMember(group_id=g.id, intent_id=m.id, student_id=m.student_id,
                                         status=MemberStatus.ACTIVE, joined_at=g.created_at + timedelta(hours=1)))
            if status == GroupStatus.FORMING:
                m.status, m.group_id = IntentStatus.IN_GROUP, g.id
        return g

    # 1. Walkthrough: Jordan and Priya in an EcoClean detergent group at 2/4 (group id 1).
    jordan = intent("jordan", "detergent", 1500, friday, text="Detergent under $15 by Friday, any brand.")
    priya = intent("priya", "detergent", 1500, friday, text="Any detergent, $15 max, by Friday.")
    session.flush()
    detergent_group = group("ecoclean", start + timedelta(hours=24), [jordan, priya])

    # 2. Ramen at 2/4 (used by the "better deal appears" case).
    maya = intent("maya", "ramen", 1200, friday + timedelta(days=3), text="Ramen under $12.")
    leo = intent("leo", "ramen", 1200, friday + timedelta(days=3), text="Instant ramen, $12 cap.")
    session.flush()
    ramen_group = group("noodlehouse", start + timedelta(hours=30), [maya, leo])

    # 3. Coffee at 3/4: one more buyer completes it immediately.
    coffee_members = [intent(k, "coffee", 2500, friday + timedelta(days=2), text="Coffee beans, $25 max.")
                      for k in ("noah", "zoe", "ethan")]
    session.flush()
    coffee_group = group("island", start + timedelta(hours=20), coffee_members)

    # 4. Winter jackets at 2/5 with a smaller backup offer (Summit, 3+ at $95).
    jackets = [intent("olivia", "jacket", 10000, friday + timedelta(days=4), variant="medium",
                      text="Winter jacket, medium, under $100."),
               intent("lucas", "jacket", 10000, friday + timedelta(days=4), variant="large",
                      text="Large winter coat, $100 max.")]
    session.flush()
    jacket_group = group("northpeak", start + timedelta(hours=48), jackets)

    # 5. Toilet paper at 2/3.
    tp = [intent(k, "toilet", 2100, friday + timedelta(days=1), text="Toilet paper, $21 max.")
          for k in ("grace", "daniel")]
    session.flush()
    tp_group = group("pantrypal_tp", start + timedelta(hours=36), tp)

    # 6. Rice at 1/3.
    rice = [intent("hannah", "rice", 2500, friday + timedelta(days=3), text="Big bag of rice, $25 max.")]
    session.flush()
    rice_group = group("grainhouse", start + timedelta(hours=40), rice)

    # 7. A waiting request with no deal under its cap yet (dish soap at $9.00 vs Mia's $8.50 cap).
    intent("mia", "dish", 850, friday + timedelta(days=3), text="Dish soap, keep it under $8.50.")

    # 8. Alex's order history, so the savings tab has something to show.
    history = [
        ("paperplus_towels", 14, ["alex", "ben", "grace"]),
        ("noodlehouse", 7, ["alex", "maya", "leo", "ben"]),
    ]
    for offer_key, days_ago, keys in history:
        o = offers[offer_key]
        when = start - timedelta(days=days_ago)
        past = [intent(k, _key_of(products, o.product_id), o.group_all_in + 200, when + timedelta(days=3),
                       status=IntentStatus.FULFILLED, created=when - timedelta(days=1), text="(earlier order)")
                for k in keys]
        session.flush()
        g = group(offer_key, when, past, created=when - timedelta(days=1), status=GroupStatus.FULFILLED)
        g.completed_at = when
        order = Order(group_id=g.id, offer_id=o.id, quantity=len(past), status=OrderStatus.COLLECTED,
                      pickup_code="PAST" + str(days_ago).zfill(2), created_at=when)
        session.add(order)
        session.flush()
        for i in past:
            session.add(SimulatedPayment(group_id=g.id, student_id=i.student_id, intent_id=i.id,
                                         amount_cents=o.group_all_in, status="SUCCEEDED", created_at=when,
                                         note="simulated charge (seed history)"))
            session.add(SavingsEntry(student_id=i.student_id, intent_id=i.id, order_id=order.id,
                                     item=f"{o.product.name} · {o.supplier.name}", paid_cents=o.group_all_in,
                                     solo_cents=o.solo_all_in, saved_cents=o.solo_all_in - o.group_all_in,
                                     created_at=when))

    session.flush()
    return {
        "students": {k: s.id for k, s in students.items()},
        "offers": {k: o.id for k, o in offers.items()},
        "products": {k: p.id for k, p in products.items()},
        "groups": {"detergent": detergent_group.id, "ramen": ramen_group.id, "coffee": coffee_group.id,
                   "jacket": jacket_group.id, "toilet": tp_group.id, "rice": rice_group.id},
    }


def _key_of(products: dict[str, Product], product_id: int) -> str:
    return next(k for k, p in products.items() if p.id == product_id)


def reset(session: Session) -> dict:
    """Drop everything and re-seed. Used by the Lab's reset button and by tests."""
    from app.db import ensure_schema

    session.close()
    drop_all()
    ensure_schema()
    return seed(session)
