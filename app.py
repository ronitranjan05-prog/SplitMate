from functools import lru_cache
from urllib.parse import urlencode
from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

app = Flask(__name__)
app.secret_key = "splitmate-local-secret"
DB = Path(__file__).with_name("splitmate.db")

CATEGORIES = [
    "Food",
    "Travel",
    "Shopping",
    "Bills",
    "Education",
    "Other"
]

CENT = Decimal("0.01")


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def columns(con, table):
    return {
        row["name"]
        for row in con.execute(f"PRAGMA table_info({table})").fetchall()
    }


def rename_column_if_needed(con, table, old, new):
    existing = columns(con, table)
    if old in existing and new not in existing:
        con.execute(
            f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"
        )


def validate_upi(value):
    upi_id = (value or "").strip()

    if not upi_id:
        return ""

    if len(upi_id) > 100 or not re.fullmatch(
        r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+",
        upi_id
    ):
        raise ValueError(
            "Valid UPI ID enter karo. Example: name@okaxis"
        )

    return upi_id


def setup():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            upi_id TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            paid_by INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS expense_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            amount REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            from_member INTEGER NOT NULL,
            to_member INTEGER NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        rename_column_if_needed(
            con, "expenses", "title", "description"
        )
        rename_column_if_needed(
            con, "expense_splits", "share_amount", "amount"
        )
        rename_column_if_needed(
            con, "settlements", "from_member_id", "from_member"
        )
        rename_column_if_needed(
            con, "settlements", "to_member_id", "to_member"
        )

        if "category" not in columns(con, "expenses"):
            con.execute("""
                ALTER TABLE expenses
                ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'
            """)

        if "upi_id" not in columns(con, "members"):
            con.execute("""
                ALTER TABLE members
                ADD COLUMN upi_id TEXT DEFAULT ''
            """)

        expenses = con.execute("""
            SELECT e.id, e.group_id, e.amount
            FROM expenses e
            WHERE NOT EXISTS (
                SELECT 1 FROM expense_splits s
                WHERE s.expense_id = e.id
            )
        """).fetchall()

        for expense in expenses:
            people = con.execute(
                "SELECT id FROM members WHERE group_id=? ORDER BY id",
                (expense["group_id"],)
            ).fetchall()

            if people:
                total = Decimal(str(expense["amount"]))
                share = (total / len(people)).quantize(
                    CENT, rounding=ROUND_HALF_UP
                )
                allocated = Decimal("0.00")

                for index, person in enumerate(people):
                    if index == len(people) - 1:
                        part = total - allocated
                    else:
                        part = share
                        allocated += part

                    con.execute("""
                        INSERT INTO expense_splits
                        (expense_id, member_id, amount)
                        VALUES (?, ?, ?)
                    """, (
                        expense["id"],
                        person["id"],
                        float(part)
                    ))


def money_decimal(value):
    try:
        amount = Decimal(str(value)).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
        if not amount.is_finite() or amount <= 0:
            raise ValueError
        return amount
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Valid amount enter karo.")


def money(value):
    return float(money_decimal(value))


def share_decimal(value):
    try:
        amount = Decimal(str(value)).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
        if not amount.is_finite() or amount < 0:
            raise ValueError
        return amount
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(
            "Har member ka valid non-negative share enter karo."
        )


def balances_for(con, group_id):
    members = con.execute(
        "SELECT * FROM members WHERE group_id=? ORDER BY id",
        (group_id,)
    ).fetchall()

    result = {}

    for member in members:
        result[member["id"]] = {
            "id": member["id"],
            "name": member["name"],
            "paid": 0.0,
            "share": 0.0,
            "sent": 0.0,
            "received": 0.0,
            "net": 0.0
        }

    expenses = con.execute("""
        SELECT id, paid_by, amount
        FROM expenses
        WHERE group_id=?
    """, (group_id,)).fetchall()

    for expense in expenses:
        if expense["paid_by"] in result:
            result[expense["paid_by"]]["paid"] += float(
                expense["amount"]
            )

        splits = con.execute("""
            SELECT member_id, amount
            FROM expense_splits
            WHERE expense_id=?
        """, (expense["id"],)).fetchall()

        for split in splits:
            if split["member_id"] in result:
                result[split["member_id"]]["share"] += float(
                    split["amount"]
                )

    settlements = con.execute("""
        SELECT from_member, to_member, amount
        FROM settlements
        WHERE group_id=?
    """, (group_id,)).fetchall()

    for settlement in settlements:
        if settlement["from_member"] in result:
            result[settlement["from_member"]]["sent"] += float(
                settlement["amount"]
            )

        if settlement["to_member"] in result:
            result[settlement["to_member"]]["received"] += float(
                settlement["amount"]
            )

    for item in result.values():
        item["net"] = round(
            item["paid"] - item["share"]
            + item["sent"] - item["received"], 2
        )

        for key in ("paid", "share", "sent", "received"):
            item[key] = round(item[key], 2)

    return list(result.values())


def suggested_payments(balances):
    names = [b["name"] for b in balances]
    member_ids = [b["id"] for b in balances]

    amounts = tuple(
        int(round(b["net"] * 100))
        for b in balances
    )

    # A valid expense/settlement ledger should always balance to zero.
    if sum(amounts) != 0:
        return []

    @lru_cache(maxsize=None)
    def solve(state):
        i = next(
            (index for index, value in enumerate(state) if value != 0),
            None
        )

        if i is None:
            return ()

        best = None
        tried = set()

        for j in range(i + 1, len(state)):
            if state[i] * state[j] >= 0:
                continue

            if state[j] in tried:
                continue

            tried.add(state[j])

            updated = list(state)
            transfer = min(abs(state[i]), abs(state[j]))

            if state[i] < 0:
                updated[i] += transfer
                updated[j] -= transfer
                payment = (i, j, transfer)
            else:
                updated[i] -= transfer
                updated[j] += transfer
                payment = (j, i, transfer)

            candidate = (payment,) + solve(tuple(updated))

            if best is None or len(candidate) < len(best):
                best = candidate

            if abs(state[i]) == abs(state[j]):
                break

        return best if best is not None else ()

    transactions = solve(amounts)

    return [
        {
            "from": names[debtor],
            "to": names[creditor],
            "from_id": member_ids[debtor],
            "to_id": member_ids[creditor],
            "amount": amount / 100,
            "upi_link": None
        }
        for debtor, creditor, amount in transactions
    ]


def make_upi_link(upi_id, payee_name, amount, note):
    if not upi_id:
        return None

    params = {
        "pa": upi_id,
        "pn": payee_name,
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": note
    }

    return "upi://pay?" + urlencode(params)


@app.route("/")
def index():
    with db() as con:
        groups = con.execute("""
            SELECT g.*,
                (SELECT COUNT(*) FROM members m
                 WHERE m.group_id=g.id) AS member_count,
                (SELECT COALESCE(SUM(e.amount), 0)
                 FROM expenses e WHERE e.group_id=g.id) AS total
            FROM groups g
            ORDER BY g.id DESC
        """).fetchall()

        dashboard = {
            "total_groups": con.execute(
                "SELECT COUNT(*) FROM groups"
            ).fetchone()[0],

            "total_members": con.execute(
                "SELECT COUNT(*) FROM members"
            ).fetchone()[0],

            "total_expenses": con.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM expenses"
            ).fetchone()[0]
        }

        recent_groups = groups[:4]

    return render_template(
        "index.html",
        groups=groups,
        dashboard=dashboard,
        recent_groups=recent_groups
    )


@app.route("/create_group", methods=["POST"])
def create_group():
    name = request.form.get("group_name", "").strip()

    if not name:
        flash("Group ka naam enter karo.")
        return redirect(url_for("index"))

    if len(name) > 80:
        flash("Group name 80 characters se chhota rakho.")
        return redirect(url_for("index"))

    with db() as con:
        cur = con.execute(
            "INSERT INTO groups (name) VALUES (?)",
            (name,)
        )
        group_id = cur.lastrowid

    flash("Group successfully create ho gaya.")
    return redirect(url_for("group_detail", group_id=group_id))


@app.route("/group/<int:group_id>", methods=["GET", "POST"])
def group_detail(group_id):
    with db() as con:
        group = con.execute(
            "SELECT * FROM groups WHERE id=?",
            (group_id,)
        ).fetchone()

        if not group:
            return "Group nahi mila.", 404

        if request.method == "POST":
            action = request.form.get("action")

            try:
                if action == "add_member":
                    name = request.form.get(
                        "member_name", ""
                    ).strip()

                    upi_id = validate_upi(
                        request.form.get("upi_id", "")
                    )

                    if not name:
                        raise ValueError(
                            "Member ka naam enter karo."
                        )

                    if len(name) > 60:
                        raise ValueError(
                            "Member name 60 characters se chhota rakho."
                        )

                    con.execute("""
                        INSERT INTO members (group_id, name, upi_id)
                        VALUES (?, ?, ?)
                    """, (group_id, name, upi_id))

                    flash("Member successfully add ho gaya.")

                elif action == "update_upi":
                    try:
                        member_id = int(
                            request.form.get("member_id", "0")
                        )
                    except (ValueError, TypeError):
                        raise ValueError("Valid member select karo.")

                    upi_id = validate_upi(
                        request.form.get("upi_id", "")
                    )

                    cur = con.execute("""
                        UPDATE members
                        SET upi_id=?
                        WHERE id=? AND group_id=?
                    """, (upi_id, member_id, group_id))

                    if cur.rowcount == 0:
                        raise ValueError("Member nahi mila.")

                    flash("UPI ID successfully update ho gayi.")

                elif action == "add_expense":
                    description = request.form.get(
                        "description", ""
                    ).strip()

                    total = money_decimal(
                        request.form.get("amount", "")
                    )
                    amount = float(total)

                    try:
                        paid_by = int(
                            request.form.get("paid_by", "0")
                        )
                    except (ValueError, TypeError):
                        raise ValueError("Valid payer select karo.")

                    category = request.form.get(
                        "category", "Other"
                    ).strip()

                    split_type = request.form.get(
                        "split_type", "equal"
                    ).strip().lower()

                    if category not in CATEGORIES:
                        raise ValueError(
                            "Valid expense category select karo."
                        )

                    if split_type not in ("equal", "custom"):
                        raise ValueError("Invalid split type.")

                    members = con.execute("""
                        SELECT id FROM members
                        WHERE group_id=?
                        ORDER BY id
                    """, (group_id,)).fetchall()

                    member_ids = [m["id"] for m in members]

                    if not description:
                        raise ValueError(
                            "Expense ka naam enter karo."
                        )

                    if len(description) > 100:
                        raise ValueError(
                            "Expense name 100 characters se chhota rakho."
                        )

                    if not member_ids:
                        raise ValueError("Pehle members add karo.")

                    if paid_by not in member_ids:
                        raise ValueError("Valid payer select karo.")

                    shares = []

                    if split_type == "custom":
                        for member_id in member_ids:
                            field_name = f"share_{member_id}"
                            raw_share = request.form.get(
                                field_name, ""
                            ).strip()

                            if raw_share == "":
                                raise ValueError(
                                    "Har member ka custom share enter karo."
                                )

                            shares.append(
                                (member_id, share_decimal(raw_share))
                            )

                        custom_total = sum(
                            (share for _, share in shares),
                            Decimal("0.00")
                        )

                        if custom_total != total:
                            raise ValueError(
                                f"Shares ka total ₹{custom_total:.2f} hai. "
                                f"Expense amount ₹{total:.2f} hona chahiye."
                            )

                    else:
                        share = (total / len(member_ids)).quantize(
                            CENT, rounding=ROUND_HALF_UP
                        )

                        allocated = Decimal("0.00")

                        for index, member_id in enumerate(member_ids):
                            if index == len(member_ids) - 1:
                                part = total - allocated
                            else:
                                part = share
                                allocated += part

                            shares.append((member_id, part))

                    cur = con.execute("""
                        INSERT INTO expenses
                        (group_id, description, amount, paid_by, category)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        group_id,
                        description,
                        amount,
                        paid_by,
                        category
                    ))

                    expense_id = cur.lastrowid

                    for member_id, part in shares:
                        con.execute("""
                            INSERT INTO expense_splits
                            (expense_id, member_id, amount)
                            VALUES (?, ?, ?)
                        """, (
                            expense_id,
                            member_id,
                            float(part)
                        ))

                    flash("Expense successfully add ho gaya.")

                elif action == "record_settlement":
                    try:
                        sender = int(
                            request.form.get("from_member", "0")
                        )
                        receiver = int(
                            request.form.get("to_member", "0")
                        )
                    except (ValueError, TypeError):
                        raise ValueError("Valid members select karo.")

                    amount = money(
                        request.form.get("settlement_amount", "")
                    )

                    balances = balances_for(con, group_id)
                    by_id = {b["id"]: b for b in balances}

                    if sender == receiver:
                        raise ValueError(
                            "Same member ko repayment nahi kar sakte."
                        )

                    if sender not in by_id or receiver not in by_id:
                        raise ValueError("Valid members select karo.")

                    if (
                        by_id[sender]["net"] >= 0
                        or by_id[receiver]["net"] <= 0
                    ):
                        raise ValueError(
                            "Payer aur receiver ke balances check karo."
                        )

                    if amount > abs(by_id[sender]["net"]) + 0.001:
                        raise ValueError(
                            "Repayment sender ke due se zyada hai."
                        )

                    if amount > by_id[receiver]["net"] + 0.001:
                        raise ValueError(
                            "Repayment receiver ke due se zyada hai."
                        )

                    con.execute("""
                        INSERT INTO settlements
                        (group_id, from_member, to_member, amount)
                        VALUES (?, ?, ?, ?)
                    """, (
                        group_id, sender, receiver, amount
                    ))

                    flash("Repayment successfully record ho gaya.")

                else:
                    raise ValueError("Invalid action.")

            except (ValueError, TypeError, InvalidOperation) as error:
                flash(str(error))

            return redirect(
                url_for("group_detail", group_id=group_id)
            )

        members = con.execute("""
            SELECT * FROM members
            WHERE group_id=?
            ORDER BY id
        """, (group_id,)).fetchall()

        expenses = con.execute("""
            SELECT e.*, m.name AS payer_name
            FROM expenses e
            LEFT JOIN members m ON e.paid_by=m.id
            WHERE e.group_id=?
            ORDER BY e.id DESC
        """, (group_id,)).fetchall()

        settlements = con.execute("""
            SELECT s.*,
                a.name AS sender_name,
                b.name AS receiver_name
            FROM settlements s
            LEFT JOIN members a ON s.from_member=a.id
            LEFT JOIN members b ON s.to_member=b.id
            WHERE s.group_id=?
            ORDER BY s.id DESC
        """, (group_id,)).fetchall()

        balances = balances_for(con, group_id)
        suggestions = suggested_payments(balances)

        # Add a UPI link only when the receiver has saved a UPI ID.
        member_by_id = {m["id"]: m for m in members}

        for payment in suggestions:
            receiver = member_by_id.get(payment["to_id"])

            if receiver:
                payment["upi_link"] = make_upi_link(
                    receiver["upi_id"],
                    receiver["name"],
                    payment["amount"],
                    f"SplitMate settlement - {group['name']}"
                )

        total_expenses = round(
            sum(float(e["amount"]) for e in expenses), 2
        )

        category_totals = con.execute("""
            SELECT category, SUM(amount) AS total
            FROM expenses
            WHERE group_id=?
            GROUP BY category
            ORDER BY total DESC
        """, (group_id,)).fetchall()

    return render_template(
        "group.html",
        group=group,
        members=members,
        expenses=expenses,
        settlements=settlements,
        balances=balances,
        suggestions=suggestions,
        total_expenses=total_expenses,
        categories=CATEGORIES,
        category_totals=category_totals
    )


@app.route("/delete_group/<int:group_id>", methods=["POST"])
def delete_group(group_id):
    with db() as con:
        expense_ids = con.execute(
            "SELECT id FROM expenses WHERE group_id=?",
            (group_id,)
        ).fetchall()

        for expense in expense_ids:
            con.execute(
                "DELETE FROM expense_splits WHERE expense_id=?",
                (expense["id"],)
            )

        con.execute(
            "DELETE FROM settlements WHERE group_id=?",
            (group_id,)
        )

        con.execute(
            "DELETE FROM expenses WHERE group_id=?",
            (group_id,)
        )

        con.execute(
            "DELETE FROM members WHERE group_id=?",
            (group_id,)
        )

        con.execute(
            "DELETE FROM groups WHERE id=?",
            (group_id,)
        )

    flash("Group delete ho gaya.")
    return redirect(url_for("index"))


setup()


if __name__ == "__main__":
    app.run(debug=True)