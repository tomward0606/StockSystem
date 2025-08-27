# ──────────────────────────────────────────────────────────────────────────────
# Servitech STOCK SYSTEM
# ──────────────────────────────────────────────────────────────────────────────

from datetime import datetime
import os
from types import SimpleNamespace

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from sqlalchemy import func

# ── App & Config (LOCAL HARD-CODED) ────────────────────────────────────────────
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail

app = Flask(__name__)

# Keep a simple dev secret key locally
app.config["SECRET_KEY"] = "devkey"

# Database: hard-coded Render external URL (needs SSL)
# NOTE: Render requires SSL for external connections → add '?sslmode=require'
app.config["SQLALCHEMY_DATABASE_URI"] = (
    "postgresql://servitech_db_user:"
    "79U6KaAxlHdUfOeEt1iVDc65KXFLPie2"
    "@dpg-d1ckf9ur433s73fti9p0-a.oregon-postgres.render.com"
    "/servitech_db?sslmode=require"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Mail.    -- Switch back when done
app.config["MAIL_SERVER"] = "smtp.gmail.com"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USERNAME"] = "servitech.stock@gmail.com"          # <— change me!!!!
app.config["MAIL_PASSWORD"] = "qmorqthzpbxqnkrp"  # <— change !!!!
app.config["MAIL_DEFAULT_SENDER"] = (
    "Servitech Stock",                             # display name
    app.config["MAIL_USERNAME"],                   # email address
)

db = SQLAlchemy(app)
mail = Mail(app)

# ── Models ────────────────────────────────────────────────────────────────────
class PartsOrder(db.Model):
    __tablename__ = "parts_order"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), nullable=True)
    items = db.relationship("PartsOrderItem", backref="order", cascade="all, delete-orphan")

class PartsOrderItem(db.Model):
    __tablename__ = "parts_order_item"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("parts_order.id"), nullable=False)
    part_number = db.Column(db.String(64))
    description = db.Column(db.String(256))
    quantity = db.Column(db.Integer)
    quantity_sent = db.Column(db.Integer, default=0)
    back_order = db.Column(db.Boolean, default=False)
    back_order = db.Column(db.Boolean, nullable=False, default=False)

    @property
    def qty_remaining(self) -> int:
        """Remaining = requested - sent (never below 0)."""
        try:
            return max(0, int(self.quantity or 0) - int(self.quantity_sent or 0))
        except Exception:
            return 0

class HiddenPart(db.Model):
    __tablename__ = "hidden_part"

    part_number = db.Column(db.String, primary_key=True)   # matches CSV "Part Number"
    reason      = db.Column(db.String, nullable=True)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_by  = db.Column(db.String, nullable=True)

    def __repr__(self):
        return f"<HiddenPart {self.part_number}>"

class DispatchNote(db.Model):
    __tablename__ = "dispatch_note"
    id = db.Column(db.Integer, primary_key=True)
    engineer_email = db.Column(db.String(120), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    picker_name = db.Column(db.String(100), nullable=True)
    items = db.relationship("DispatchItem", backref="dispatch_note", cascade="all, delete-orphan")

class DispatchItem(db.Model):
    __tablename__ = "dispatch_item"
    id = db.Column(db.Integer, primary_key=True)
    dispatch_note_id = db.Column(db.Integer, db.ForeignKey("dispatch_note.id"), nullable=False)
    part_number = db.Column(db.String(64))
    quantity_sent = db.Column(db.Integer)
    description = db.Column(db.String(256))

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_outstanding_items(engineer_email: str):
    """All lines with remaining > 0, regardless of back_order flag."""
    return (
        db.session.query(PartsOrderItem)
        .join(PartsOrder, PartsOrder.id == PartsOrderItem.order_id)
        .filter(
            PartsOrder.email == engineer_email,
            (PartsOrderItem.quantity - func.coalesce(PartsOrderItem.quantity_sent, 0)) > 0
        )
        .order_by(PartsOrderItem.id.asc())
        .all()
    )


def get_back_orders(engineer_email: str):
    """Only items explicitly marked as back_order AND still remaining > 0."""
    return (
        db.session.query(PartsOrderItem)
        .join(PartsOrder, PartsOrder.id == PartsOrderItem.order_id)
        .filter(
            PartsOrder.email == engineer_email,
            PartsOrderItem.back_order.is_(True),
            (PartsOrderItem.quantity - func.coalesce(PartsOrderItem.quantity_sent, 0)) > 0
        )
        .order_by(PartsOrderItem.id.asc())
        .all()
    )


def build_html_email(sent_items, back_orders, dispatch, generated_at=None) -> str:
    """Build an HTML email body listing sent items and current back orders."""
    generated_at = generated_at or datetime.utcnow()

    def esc(s):  # small escape helper
        return s or ""

    sent_rows = (
        "".join(
            f"<tr><td>{esc(s.part_number)}</td>"
            f"<td>{esc(getattr(s, 'description', ''))}</td>"
            f"<td style='text-align:right'>{int(s.quantity_sent or 0)}</td></tr>"
            for s in sent_items
        )
        or "<tr><td colspan='3' style='text-align:center; color:#666'>(No items on this dispatch)</td></tr>"
    )

    bo_rows = (
        "".join(
            f"<tr><td>{esc(bo.part_number)}</td>"
            f"<td>{esc(bo.description)}</td>"
            f"<td style='text-align:right'>{int((bo.quantity or 0) - (bo.quantity_sent or 0))}</td>"
            f"<td>{bo.order.date.strftime('%d %b %Y')}</td></tr>"
            for bo in back_orders
            if (bo.quantity or 0) - (bo.quantity_sent or 0) > 0
        )
        or "<tr><td colspan='4' style='text-align:center; color:#666'>No items on back order.</td></tr>"
    )

    html = f"""
    <div style="font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; color:#111; line-height:1.4">
      <h2 style="margin:0 0 8px 0">Dispatch Note — {dispatch.date.strftime('%d %b %Y')}</h2>
      <p style="margin:0 0 12px 0"><strong>Engineer:</strong> {esc(dispatch.engineer_email)}<br>
      <strong>Picked by:</strong> {esc(dispatch.picker_name or 'Unknown')}<br>
      <span style="color:#666">Generated at {generated_at.strftime('%d %b %Y %H:%M UTC')}</span></p>

      <h3 style="margin:16px 0 8px 0; font-size:18px">Items Sent</h3>
      <table width="100%" cellpadding="8" cellspacing="0" border="1" style="border-collapse:collapse; border-color:#ddd">
        <thead style="background:#f8f9fa">
          <tr>
            <th align="left">Part Number</th>
            <th align="left">Description</th>
            <th align="right">Quantity Sent</th>
          </tr>
        </thead>
        <tbody>{sent_rows}</tbody>
      </table>

      <h3 style="margin:16px 0 8px 0; font-size:18px">Still on Back Order</h3>
      <table width="100%" cellpadding="8" cellspacing="0" border="1" style="border-collapse:collapse; border-color:#ddd">
        <thead style="background:#f8f9fa">
          <tr>
            <th align="left">Part Number</th>
            <th align="left">Description</th>
            <th align="right">Qty Remaining</th>
            <th align="left">Order Date</th>
          </tr>
        </thead>
        <tbody>{bo_rows}</tbody>
      </table>

      <p style="margin-top:16px">Thank you,<br>Servitech Stock System</p>
    </div>
    """
    return html

def send_dispatch_email(engineer_email: str, dispatch_id: int) -> None:
    """
    Send an email listing items sent in this dispatch + current back orders.
    Plain text body + HTML body for nicer clients.
    """
    dispatch = DispatchNote.query.get(dispatch_id)
    if not dispatch:
        return

    sent_items = DispatchItem.query.filter_by(dispatch_note_id=dispatch_id).all()
    back_orders = get_back_orders(engineer_email)

    subject = f"Dispatch Note - {dispatch.date.strftime('%d %b %Y')}"
    lines = []
    lines.append("Hello,\n")
    lines.append("Your dispatch has been processed.")
    if dispatch.picker_name:
        lines.append(f"Picker: {dispatch.picker_name}")
    lines.append("")  # blank line

    # Sent items
    lines.append("Items Sent:")
    if sent_items:
        for s in sent_items:
            lines.append(f"- {s.part_number} ({s.description or ''}): {s.quantity_sent}")
    else:
        lines.append("- (No items recorded on this dispatch)")

    # Back orders
    if back_orders:
        lines.append("\nItems Still on Back Order:")
        for bo in back_orders:
            remaining = (bo.quantity or 0) - (bo.quantity_sent or 0)
            if remaining > 0:
                lines.append(f"- {bo.part_number} ({bo.description or ''}): {remaining}")

    lines.append("\nThank you,\nServitech Stock System")

    msg = Message(subject=subject, recipients=[engineer_email], body="\n".join(lines))
    msg.html = build_html_email(sent_items, back_orders, dispatch)
    mail.send(msg)

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return render_template("home.html")

@app.route("/test_email")
def test_email():
    """Quick smoke-test for email config."""
    msg = Message(
        subject="Test Email from Stock System",
        recipients=["tomward0606@gmail.com"],
        body="This is a test email from the Servitech Stock system.",
    )
    mail.send(msg)
    return "Test email sent!"

@app.route("/admin/test_dummy_dispatch_email")
def test_dummy_dispatch_email():
    """
    Send a DUMMY dispatch email (HTML + plain text) to tomward0606@gmail.com.
    Does not touch the database.
    """
    # Dummy dispatch & items
    dispatch = SimpleNamespace(
        engineer_email="engineer@example.com",
        picker_name="Test Picker",
        date=datetime.utcnow(),
    )

    class DummySent(SimpleNamespace): ...
    class DummyBO(SimpleNamespace): ...

    sent_items = [
        DummySent(part_number="PN123", description="Widget A", quantity_sent=3),
        DummySent(part_number="PN456", description="Widget B", quantity_sent=1),
    ]
    # Simulate back orders with an .order having .date like the real model
    order1 = SimpleNamespace(date=datetime(2025, 8, 10))
    order2 = SimpleNamespace(date=datetime(2025, 8, 12))
    back_orders = [
        DummyBO(part_number="PN123", description="Widget A", quantity=5, quantity_sent=3, order=order2),
        DummyBO(part_number="PN789", description="Widget C", quantity=5, quantity_sent=0, order=order1),
    ]

    # Plain-text body
    text_lines = [
        "Your dispatch has been processed.",
        f"Picker: {dispatch.picker_name}",
        "",
        "Items Sent:",
    ]
    for s in sent_items:
        text_lines.append(f"- {s.part_number} ({s.description}): {s.quantity_sent}")

    text_lines.append("\nItems Still on Back Order:")
    for bo in back_orders:
        remaining = (bo.quantity or 0) - (bo.quantity_sent or 0)
        if remaining > 0:
            text_lines.append(
                f"- {bo.part_number} ({bo.description}): {remaining} (Ordered {bo.order.date.strftime('%d %b %Y')})"
            )

    msg = Message(
        subject=f"Dispatch Note - {dispatch.date.strftime('%d %b %Y')}",
        recipients=["tomward0606@gmail.com"],
        body="\n".join(text_lines),
    )
    msg.html = build_html_email(sent_items, back_orders, dispatch)
    mail.send(msg)
    return "Dummy dispatch email sent to tomward0606@gmail.com"

@app.route('/admin/parts_orders_list')
def parts_orders_list():
    from sqlalchemy import func

    outstanding_data = (
        db.session.query(
            PartsOrder.email,
            func.coalesce(func.sum(PartsOrderItem.quantity - PartsOrderItem.quantity_sent), 0).label("outstanding_total")
        )
        .join(PartsOrderItem)
        .group_by(PartsOrder.email)
        .order_by(func.coalesce(func.sum(PartsOrderItem.quantity - PartsOrderItem.quantity_sent), 0).desc())
        .all()
    )

    return render_template('parts_orders_list.html', data=outstanding_data)


# helper: ALL outstanding items (remaining > 0), regardless of back_order
def get_outstanding_items(email: str):
    return (
        db.session.query(PartsOrderItem)
        .join(PartsOrder, PartsOrder.id == PartsOrderItem.order_id)
        .filter(
            PartsOrder.email == email,
            (PartsOrderItem.quantity - db.func.coalesce(PartsOrderItem.quantity_sent, 0)) > 0
        )
        .order_by(PartsOrderItem.id.asc())
        .all()
    )

@app.route("/admin/parts_order_detail/<email>", methods=["GET", "POST"])
def parts_order_detail(email):
    """
    Admin page to dispatch items for an engineer:
    - Shows outstanding lines (remaining > 0)
    - Records a DispatchNote with one or more DispatchItems
    - Emails the engineer (sent + current back orders = explicit flags only)
    """
    outstanding_items = get_outstanding_items(email)   # all remaining > 0
    back_orders = get_back_orders(email)               # strict: flag==True & remaining>0

    engineer_dispatches = (
        db.session.query(DispatchNote)
        .filter(DispatchNote.engineer_email == email)
        .order_by(DispatchNote.date.desc())
        .all()
    )

    if request.method == "POST":
        picker_name = request.form.get("picker_name", "").strip()
        custom_picker_name = request.form.get("custom_picker_name", "").strip()
        if picker_name == "other" and custom_picker_name:
            final_picker_name = custom_picker_name
        elif picker_name and picker_name != "other":
            final_picker_name = picker_name
        else:
            flash("Please select or enter a picker name.", "error")
            return render_template(
                "parts_order_detail.html",
                email=email,
                outstanding_items=outstanding_items,
                back_orders=back_orders,
                engineer_dispatches=engineer_dispatches,
            )

        dispatch = None
        dispatch_created = False
        flags_changed = False

        for item in outstanding_items:
            to_send = int(request.form.get(f"send_{item.id}", 0) or 0)
            max_send = getattr(item, "qty_remaining", item.quantity - (item.quantity_sent or 0))

            if 0 < to_send <= max_send:
                if dispatch is None:
                    dispatch = DispatchNote(engineer_email=email, picker_name=final_picker_name)
                    db.session.add(dispatch)

                db.session.add(DispatchItem(
                    dispatch_note=dispatch,
                    part_number=item.part_number,
                    description=item.description,
                    quantity_sent=to_send,
                ))
                item.quantity_sent = (item.quantity_sent or 0) + to_send
                dispatch_created = True

            desired_flag = (request.form.get(f"back_order_{item.id}") == "on")
            if getattr(item, "back_order", False) != desired_flag:
                item.back_order = desired_flag
                flags_changed = True

            db.session.add(item)

        if dispatch_created:
            db.session.commit()
            flash(f"Dispatch recorded successfully. Picked by: {final_picker_name}", "success")
            send_dispatch_email(email, dispatch.id)
        elif flags_changed:
            db.session.commit()
            flash("Back order flags updated.", "info")
        else:
            db.session.rollback()
            flash("No items were dispatched and no changes were made.", "warning")

        return redirect(url_for("parts_order_detail", email=email))

    return render_template(
        "parts_order_detail.html",
        email=email,
        outstanding_items=outstanding_items,
        back_orders=back_orders,
        engineer_dispatches=engineer_dispatches,
    )



@app.route("/admin/dispatched_orders")
def dispatched_orders():
    """Chronological list of all dispatch notes."""
    dispatches = db.session.query(DispatchNote).order_by(DispatchNote.date.desc()).all()
    return render_template("dispatched_orders.html", dispatches=dispatches)

@app.route("/admin/dispatch_note/<int:dispatch_id>")
def view_dispatch_note(dispatch_id: int):
    """Dedicated view for a single dispatch note (if you want to render/print)."""
    dispatch = DispatchNote.query.get_or_404(dispatch_id)
    sent_items = DispatchItem.query.filter_by(dispatch_note_id=dispatch_id).all()
    back_orders = get_back_orders(dispatch.engineer_email)
    return render_template(
        "dispatch_note.html",
        dispatch=dispatch,
        sent_items=sent_items,
        back_orders=back_orders,
    )

@app.route('/admin/cancel_order_item/<int:item_id>', methods=['POST'])
def cancel_order_item(item_id: int):
    """
    Instantly remove a PartsOrderItem from the order (regardless of qty_sent).
    Does NOT affect past dispatch history.
    If the parent order has no more items after removal, delete the order too.
    """
    item = PartsOrderItem.query.get_or_404(item_id)

    # keep details before delete
    engineer_email = item.order.email if item.order else request.form.get('email', '')
    part_num = item.part_number
    parent_order = item.order

    db.session.delete(item)
    db.session.flush()  # reflect relationship counts before commit

    # If this was the last item, remove the empty parent order
    if parent_order and len(parent_order.items) == 0:
        db.session.delete(parent_order)

    db.session.commit()
    flash(f"Removed item {part_num} from the order.", "success")
    return redirect(url_for('parts_order_detail', email=engineer_email))

# ── Catalogue Manager (Hide / Unhide parts) ───────────────────────────────────
@app.route("/admin/hidden-parts", methods=["GET", "POST"])
def hidden_parts():
    """
    List hidden parts and allow admins to hide new part numbers.
    Uses the HiddenPart table (case-insensitive by uppercasing keys).
    """
    if request.method == "POST":
        pn = (request.form.get("part_number") or "").strip().upper()
        reason = (request.form.get("reason") or "").strip()
        created_by = (request.form.get("created_by") or "").strip()

        if not pn:
            flash("Please enter a part number.", "warning")
            return redirect(url_for("hidden_parts"))

        # Avoid duplicates
        existing = HiddenPart.query.get(pn)
        if existing:
            flash(f"{pn} is already hidden.", "info")
        else:
            db.session.add(HiddenPart(part_number=pn, reason=reason, created_by=created_by))
            db.session.commit()
            flash(f"Hidden: {pn}", "success")

        return redirect(url_for("hidden_parts"))

    rows = HiddenPart.query.order_by(HiddenPart.created_at.desc()).all()
    return render_template("hidden_parts.html", rows=rows)



@app.route("/admin/hidden-parts/unhide", methods=["POST"])
@app.route("/admin/hidden-parts/unhide/<path:part_number>", methods=["POST"])
def unhide_part(part_number=None):
    pn = (part_number or request.form.get("part_number") or "").strip().upper()
    if not pn:
        flash("No part number provided.", "warning")
        return redirect(url_for("hidden_parts"))

    row = HiddenPart.query.get(pn)
    if row:
        db.session.delete(row)
        db.session.commit()
        flash(f"Unhidden: {pn}", "success")
    else:
        flash(f"{pn} wasn’t hidden.", "info")
    return redirect(url_for("hidden_parts"))


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)



