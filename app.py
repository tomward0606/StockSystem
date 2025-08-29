# ──────────────────────────────────────────────────────────────────────────────
# Servitech STOCK SYSTEM - Complete Application
# ──────────────────────────────────────────────────────────────────────────────

# ── Core Imports ──────────────────────────────────────────────────────────────
from datetime import datetime
import os
import csv
import io
from types import SimpleNamespace

# ── Flask & Extensions ────────────────────────────────────────────────────────
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from sqlalchemy import func
import requests

# ── App Configuration ─────────────────────────────────────────────────────────
app = Flask(__name__)

# Basic configuration
app.config["SECRET_KEY"] = "devkey"

# Database configuration - Render PostgreSQL with SSL
app.config["SQLALCHEMY_DATABASE_URI"] = (
    "postgresql://servitech_db_user:"
    "79U6KaAxlHdUfOeEt1iVDc65KXFLPie2"
    "@dpg-d1ckf9ur433s73fti9p0-a.oregon-postgres.render.com"
    "/servitech_db?sslmode=require"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Email configuration
app.config["MAIL_SERVER"] = "smtp.gmail.com"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USERNAME"] = "servitech.stock@gmail.com"
app.config["MAIL_PASSWORD"] = "qmorqthzpbxqnkrp"
app.config["MAIL_DEFAULT_SENDER"] = (
    "Servitech Stock",
    app.config["MAIL_USERNAME"],
)

# Initialize extensions
db = SQLAlchemy(app)
mail = Mail(app)

# ── Database Models ───────────────────────────────────────────────────────────

class PartsOrder(db.Model):
    """Main parts order - groups items by engineer email"""
    __tablename__ = "parts_order"
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), nullable=True)
    items = db.relationship("PartsOrderItem", backref="order", cascade="all, delete-orphan")


class PartsOrderItem(db.Model):
    """Individual line items in a parts order"""
    __tablename__ = "parts_order_item"
    
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("parts_order.id"), nullable=False)
    part_number = db.Column(db.String(64))
    description = db.Column(db.String(256))
    quantity = db.Column(db.Integer)
    quantity_sent = db.Column(db.Integer, default=0)
    back_order = db.Column(db.Boolean, nullable=False, default=False)

    @property
    def qty_remaining(self) -> int:
        """Calculate remaining quantity (requested - sent, never below 0)"""
        try:
            return max(0, int(self.quantity or 0) - int(self.quantity_sent or 0))
        except Exception:
            return 0


class DispatchNote(db.Model):
    """Record of dispatched parts to engineers"""
    __tablename__ = "dispatch_note"
    
    id = db.Column(db.Integer, primary_key=True)
    engineer_email = db.Column(db.String(120), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    picker_name = db.Column(db.String(100), nullable=True)
    items = db.relationship("DispatchItem", backref="dispatch_note", cascade="all, delete-orphan")


class DispatchItem(db.Model):
    """Individual items sent in a dispatch"""
    __tablename__ = "dispatch_item"
    
    id = db.Column(db.Integer, primary_key=True)
    dispatch_note_id = db.Column(db.Integer, db.ForeignKey("dispatch_note.id"), nullable=False)
    part_number = db.Column(db.String(64))
    quantity_sent = db.Column(db.Integer)
    description = db.Column(db.String(256))


class Part(db.Model):
    """Parts catalogue - synced with GitHub CSV"""
    __tablename__ = "part"
    
    product_code = db.Column(db.String(64), primary_key=True)
    description = db.Column(db.String(256), nullable=True)
    category = db.Column(db.String(128), nullable=True)
    make = db.Column(db.String(128), nullable=True)
    manufacturer = db.Column(db.String(128), nullable=True)
    image = db.Column(db.String(128), nullable=True)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        """Convert part to dictionary for JSON responses"""
        return {
            'product_code': self.product_code,
            'description': self.description or '',
            'category': self.category or '',
            'make': self.make or '',
            'manufacturer': self.manufacturer or '',
            'image': self.image or ''
        }


class HiddenPart(db.Model):
    """Legacy table for hidden parts - kept for backward compatibility"""
    __tablename__ = "hidden_part"

    part_number = db.Column(db.String, primary_key=True)
    reason = db.Column(db.String, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_by = db.Column(db.String, nullable=True)

    def __repr__(self):
        return f"<HiddenPart {self.part_number}>"

# ── Helper Functions ──────────────────────────────────────────────────────────

def get_outstanding_items(engineer_email: str):
    """Get all order items with remaining quantity > 0"""
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
    """Get items explicitly marked as back_order AND still have remaining quantity"""
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

# ── CSV Integration Functions ─────────────────────────────────────────────────

def fetch_csv_from_github():
    """Fetch the latest CSV from GitHub repository"""
    github_url = "https://raw.githubusercontent.com/tomward0606/PartsProjectMain/main/parts.csv"
    try:
        response = requests.get(github_url, timeout=10)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        print(f"Error fetching CSV from GitHub: {e}")
        return None


def sync_parts_from_csv():
    """Load parts from GitHub CSV into database"""
    csv_content = fetch_csv_from_github()
    if not csv_content:
        return False, "Could not fetch CSV from GitHub"
    
    try:
        # Clear existing parts
        db.session.query(Part).delete()
        
        # Parse CSV - handle different possible headers
        csv_reader = csv.DictReader(io.StringIO(csv_content))
        parts_added = 0
        
        for row in csv_reader:
            # Handle both 'Product Code' and 'product_code' variations
            product_code = (row.get('Product Code') or row.get('product_code', '')).strip()
            if not product_code:
                continue
                
            part = Part(
                product_code=product_code,
                description=(row.get('Description') or row.get('description', '')).strip() or None,
                category=(row.get('Category') or row.get('category', '')).strip() or None,
                make=(row.get('Make') or row.get('make', '')).strip() or None,
                manufacturer=(row.get('Manufacturer') or row.get('manufacturer', '')).strip() or None,
                image=(row.get('image') or row.get('Image', '')).strip() or None
            )
            
            db.session.add(part)
            parts_added += 1
        
        db.session.commit()
        return True, f"Successfully synced {parts_added} parts from GitHub CSV"
        
    except Exception as e:
        db.session.rollback()
        return False, f"Error syncing CSV: {str(e)}"


def export_parts_to_csv_format():
    """Export current parts to CSV format matching GitHub structure"""
    parts = Part.query.order_by(Part.product_code).all()
    
    output = io.StringIO()
    fieldnames = ['Product Code', 'Description', 'Category', 'Make', 'Manufacturer', 'image']
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    
    writer.writeheader()
    for part in parts:
        writer.writerow({
            'Product Code': part.product_code,
            'Description': part.description or '',
            'Category': part.category or '',
            'Make': part.make or '',
            'Manufacturer': part.manufacturer or '',
            'image': part.image or ''
        })
    
    return output.getvalue()

# ── Email Functions ───────────────────────────────────────────────────────────

def build_html_email(sent_items, back_orders, dispatch, generated_at=None) -> str:
    """Build HTML email body listing sent items and current back orders"""
    generated_at = generated_at or datetime.utcnow()

    def esc(s):  # Helper to escape HTML
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
    """Send dispatch notification email with HTML and plain text versions"""
    dispatch = DispatchNote.query.get(dispatch_id)
    if not dispatch:
        return

    sent_items = DispatchItem.query.filter_by(dispatch_note_id=dispatch_id).all()
    back_orders = get_back_orders(engineer_email)

    subject = f"Dispatch Note - {dispatch.date.strftime('%d %b %Y')}"
    
    # Build plain text version
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

    # Send email with both plain text and HTML
    msg = Message(subject=subject, recipients=[engineer_email], body="\n".join(lines))
    msg.html = build_html_email(sent_items, back_orders, dispatch)
    mail.send(msg)

# ── Main Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def home():
    """Main dashboard page"""
    return render_template("home.html")


@app.route("/test_email")
def test_email():
    """Quick email configuration test"""
    msg = Message(
        subject="Test Email from Stock System",
        recipients=["tomward0606@gmail.com"],
        body="This is a test email from the Servitech Stock system.",
    )
    mail.send(msg)
    return "Test email sent!"

# ── Parts Order Management Routes ─────────────────────────────────────────────

@app.route('/admin/parts_orders_list')
def parts_orders_list():
    """Summary of outstanding parts by engineer"""
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


@app.route("/admin/parts_order_detail/<email>", methods=["GET", "POST"])
def parts_order_detail(email):
    """Detailed view for managing one engineer's parts orders"""
    outstanding_items = get_outstanding_items(email)
    back_orders = get_back_orders(email)

    engineer_dispatches = (
        db.session.query(DispatchNote)
        .filter(DispatchNote.engineer_email == email)
        .order_by(DispatchNote.date.desc())
        .all()
    )

    if request.method == "POST":
        # Handle picker name selection
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

        # Process each outstanding item
        for item in outstanding_items:
            to_send = int(request.form.get(f"send_{item.id}", 0) or 0)
            max_send = getattr(item, "qty_remaining", item.quantity - (item.quantity_sent or 0))

            # Handle dispatch quantities
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

            # Handle back order flag changes
            desired_flag = (request.form.get(f"back_order_{item.id}") == "on")
            if getattr(item, "back_order", False) != desired_flag:
                item.back_order = desired_flag
                flags_changed = True

            db.session.add(item)

        # Commit changes and send email
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


@app.route('/admin/cancel_order_item/<int:item_id>', methods=['POST'])
def cancel_order_item(item_id: int):
    """Remove a parts order item completely"""
    item = PartsOrderItem.query.get_or_404(item_id)

    engineer_email = item.order.email if item.order else request.form.get('email', '')
    part_num = item.part_number
    parent_order = item.order

    db.session.delete(item)
    db.session.flush()

    # Remove empty parent orders
    if parent_order and len(parent_order.items) == 0:
        db.session.delete(parent_order)

    db.session.commit()
    flash(f"Removed item {part_num} from the order.", "success")
    return redirect(url_for('parts_order_detail', email=engineer_email))

# ── Dispatch History Routes ───────────────────────────────────────────────────

@app.route("/admin/dispatched_orders")
def dispatched_orders():
    """Chronological list of all dispatch notes"""
    dispatches = db.session.query(DispatchNote).order_by(DispatchNote.date.desc()).all()
    return render_template("dispatched_orders.html", dispatches=dispatches)


@app.route("/admin/dispatch_note/<int:dispatch_id>")
def view_dispatch_note(dispatch_id: int):
    """Detailed view of a single dispatch note"""
    dispatch = DispatchNote.query.get_or_404(dispatch_id)
    sent_items = DispatchItem.query.filter_by(dispatch_note_id=dispatch_id).all()
    back_orders = get_back_orders(dispatch.engineer_email)
    return render_template(
        "dispatch_note.html",
        dispatch=dispatch,
        sent_items=sent_items,
        back_orders=back_orders,
    )

# ── Catalogue Management Routes ───────────────────────────────────────────────

@app.route("/admin/catalogue")
def catalogue_manager():
    """Main catalogue management page with search and filtering"""
    search_query = request.args.get('search', '').strip()
    category_filter = request.args.get('category', '').strip()
    
    # Build query with filters
    query = Part.query
    
    if search_query:
        search_pattern = f"%{search_query}%"
        query = query.filter(
            db.or_(
                Part.product_code.ilike(search_pattern),
                Part.description.ilike(search_pattern),
                Part.make.ilike(search_pattern),
                Part.manufacturer.ilike(search_pattern)
            )
        )
    
    if category_filter:
        query = query.filter(Part.category.ilike(f"%{category_filter}%"))
    
    parts = query.order_by(Part.product_code).all()
    
    # Get categories for filter dropdown
    categories = db.session.query(Part.category).filter(Part.category.isnot(None)).distinct().order_by(Part.category).all()
    categories = [cat[0] for cat in categories if cat[0]]
    
    return render_template("catalogue_manager.html", 
                         parts=parts, 
                         categories=categories,
                         search_query=search_query,
                         category_filter=category_filter)


@app.route("/admin/catalogue/sync", methods=["POST"])
def sync_catalogue():
    """Sync parts from GitHub CSV"""
    success, message = sync_parts_from_csv()
    if success:
        flash(message, "success")
    else:
        flash(message, "error")
    return redirect(url_for('catalogue_manager'))


@app.route("/admin/catalogue/export")
def export_catalogue():
    """Export current catalogue as downloadable CSV"""
    csv_data = export_parts_to_csv_format()
    
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=parts_catalogue_export.csv'}
    )


@app.route("/admin/catalogue/part", methods=["POST"])
def add_part():
    """Add a new part to the catalogue"""
    product_code = request.form.get('product_code', '').strip()
    
    if not product_code:
        flash("Product code is required", "error")
        return redirect(url_for('catalogue_manager'))
    
    # Check for duplicates
    existing = Part.query.get(product_code)
    if existing:
        flash(f"Part {product_code} already exists", "error")
        return redirect(url_for('catalogue_manager'))
    
    part = Part(
        product_code=product_code,
        description=request.form.get('description', '').strip() or None,
        category=request.form.get('category', '').strip() or None,
        make=request.form.get('make', '').strip() or None,
        manufacturer=request.form.get('manufacturer', '').strip() or None,
        image=request.form.get('image', '').strip() or None
    )
    
    try:
        db.session.add(part)
        db.session.commit()
        flash(f"Successfully added part: {product_code}", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error adding part: {str(e)}", "error")
    
    return redirect(url_for('catalogue_manager'))


@app.route("/admin/catalogue/part/<product_code>", methods=["PUT", "DELETE"])
def update_or_delete_part(product_code):
    """AJAX endpoint for updating or deleting parts"""
    part = Part.query.get_or_404(product_code)
    
    if request.method == "DELETE":
        try:
            db.session.delete(part)
            db.session.commit()
            return jsonify({"success": True, "message": f"Successfully deleted part {product_code}"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"success": False, "message": f"Error deleting part: {str(e)}"})
    
    elif request.method == "PUT":
        try:
            data = request.get_json()
            
            # Update fields
            if 'description' in data:
                part.description = data['description'].strip() or None
            if 'category' in data:
                part.category = data['category'].strip() or None
            if 'make' in data:
                part.make = data['make'].strip() or None
            if 'manufacturer' in data:
                part.manufacturer = data['manufacturer'].strip() or None
            if 'image' in data:
                part.image = data['image'].strip() or None
            
            db.session.commit()
            return jsonify({"success": True, "message": f"Successfully updated part {product_code}"})
            
        except Exception as e:
            db.session.rollback()
            return jsonify({"success": False, "message": f"Error updating part: {str(e)}"})

# ── Development & Testing Routes ──────────────────────────────────────────────

@app.route("/admin/test_dummy_dispatch_email")
def test_dummy_dispatch_email():
    """Send a test dispatch email with dummy data"""
    # Create dummy objects for testing
    dispatch = SimpleNamespace(
        engineer_email="engineer@example.com",
        picker_name="Test Picker",
        date=datetime.utcnow(),
    )

    class DummySent(SimpleNamespace): pass
    class DummyBO(SimpleNamespace): pass

    sent_items = [
        DummySent(part_number="PN123", description="Widget A", quantity_sent=3),
        DummySent(part_number="PN456", description="Widget B", quantity_sent=1),
    ]
    
    # Dummy back orders with order dates
    order1 = SimpleNamespace(date=datetime(2025, 8, 10))
    order2 = SimpleNamespace(date=datetime(2025, 8, 12))
    back_orders = [
        DummyBO(part_number="PN123", description="Widget A", quantity=5, quantity_sent=3, order=order2),
        DummyBO(part_number="PN789", description="Widget C", quantity=5, quantity_sent=0, order=order1),
    ]

    # Build plain text email
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

    # Send test email
    msg = Message(
        subject=f"Dispatch Note - {dispatch.date.strftime('%d %b %Y')}",
        recipients=["tomward0606@gmail.com"],
        body="\n".join(text_lines),
    )
    msg.html = build_html_email(sent_items, back_orders, dispatch)
    mail.send(msg)
    return "Dummy dispatch email sent to tomward0606@gmail.com"


@app.route("/admin/catalogue/test_github")
def test_github_connection():
    """Test GitHub CSV connection"""
    csv_content = fetch_csv_from_github()
    if csv_content:
        lines = csv_content.split('\n')[:5]  # Show first 5 lines
        return f"<pre>GitHub CSV accessible!\n\nFirst 5 lines:\n{chr(10).join(lines)}</pre>"
    else:
        return "<pre>Error: Could not access GitHub CSV</pre>"

# ── Legacy Routes (Backward Compatibility) ────────────────────────────────────

@app.route("/admin/hidden-parts")
def hidden_parts_redirect():
    """Redirect old hidden parts URL to new catalogue manager"""
    flash("The hidden parts feature has been upgraded to a full catalogue manager!", "info")
    return redirect(url_for('catalogue_manager'))


@app.route("/admin/hidden-parts/unhide", methods=["POST"])
@app.route("/admin/hidden-parts/unhide/<path:part_number>", methods=["POST"])
def unhide_part_redirect(part_number=None):
    """Redirect old unhide functionality to catalogue manager"""
    flash("Part hiding has been replaced with the catalogue manager. Use the delete function instead.", "info")
    return redirect(url_for('catalogue_manager'))

# ── Application Initialization ────────────────────────────────────────────────

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print("Database tables created successfully!")
        print("Starting Servitech Stock System...")
        print(f"Access your application at: http://localhost:5000")
        print("\nAvailable admin routes:")
        print("  • /                           - Main dashboard")
        print("  • /admin/parts_orders_list    - Outstanding orders summary")
        print("  • /admin/dispatched_orders    - Dispatch history")
        print("  • /admin/catalogue            - Parts catalogue manager")
        print("  • /admin/catalogue/test_github - Test GitHub CSV connection")
        print("  • /test_email                 - Test email configuration")
        
    app.run(debug=True)


