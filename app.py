# ──────────────────────────────────────────────────────────────────────────────
# Servitech STOCK SYSTEM - Production Version
# ──────────────────────────────────────────────────────────────────────────────

# ── Core Imports ──────────────────────────────────────────────────────────────
from datetime import datetime
import os
import csv
import io
import base64
from types import SimpleNamespace

# ── Flask & Extensions ────────────────────────────────────────────────────────
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from sqlalchemy import func
import requests

# ── App Configuration ─────────────────────────────────────────────────────────
app = Flask(__name__)

# Security: Use environment variables for sensitive data
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-change-in-production")

# Database configuration - Secure with environment variables
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or (
    "postgresql://servitech_db_user:"
    "79U6KaAxlHdUfOeEt1iVDc65KXFLPie2"
    "@dpg-d1ckf9ur433s73fti9p0-a.oregon-postgres.render.com"
    "/servitech_db?sslmode=require"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Email configuration
app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", 587))
app.config["MAIL_USE_TLS"] = os.environ.get("MAIL_USE_TLS", "True").lower() == "true"
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME", "servitech.stock@gmail.com")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")
app.config["MAIL_DEFAULT_SENDER"] = ("Servitech Stock", app.config["MAIL_USERNAME"])

# GitHub configuration
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "tomward0606/PartsProjectMain")
CSV_FILE_PATH = os.environ.get("CSV_FILE_PATH", "parts.csv")

# Initialize extensions
db = SQLAlchemy(app)
mail = Mail(app)

# ── Database Models ───────────────────────────────────────────────────────────

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
    back_order = db.Column(db.Boolean, nullable=False, default=False)

    @property
    def qty_remaining(self) -> int:
        try:
            return max(0, int(self.quantity or 0) - int(self.quantity_sent or 0))
        except Exception:
            return 0

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

class HiddenPart(db.Model):
    __tablename__ = "hidden_part"
    part_number = db.Column(db.String, primary_key=True)
    reason = db.Column(db.String, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_by = db.Column(db.String, nullable=True)

# ── Helper Functions ──────────────────────────────────────────────────────────

def get_outstanding_items(engineer_email: str):
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

# ── GitHub CSV Functions (Fixed) ──────────────────────────────────────────────

def fetch_csv_from_github():
    """Fetch CSV content directly from GitHub repository"""
    github_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{CSV_FILE_PATH}"
    try:
        response = requests.get(github_url, timeout=10)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        print(f"Error fetching CSV from GitHub: {e}")
        return None

def parse_csv_content(csv_content):
    """Parse CSV content into list of part dictionaries"""
    if not csv_content:
        return []
    
    try:
        reader = csv.DictReader(io.StringIO(csv_content))
        parts = []
        for row in reader:
            part = {
                'product_code': (row.get('Product Code') or row.get('product_code', '')).strip(),
                'description': (row.get('Description') or row.get('description', '')).strip(),
                'category': (row.get('Category') or row.get('category', '')).strip(),
                'make': (row.get('Make') or row.get('make', '')).strip(),
                'manufacturer': (row.get('Manufacturer') or row.get('manufacturer', '')).strip(),
                'image': (row.get('image') or row.get('Image', '')).strip()
            }
            if part['product_code']:
                parts.append(part)
        return parts
    except Exception as e:
        print(f"Error parsing CSV: {e}")
        return []

def get_github_file_info():
    """Get file content and SHA from GitHub API - FIXED VERSION"""
    if not GITHUB_TOKEN:
        return None, None
    
    try:
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CSV_FILE_PATH}"
        headers = {
            'Authorization': f'Bearer {GITHUB_TOKEN}',  # Fixed: Use Bearer instead of token
            'Accept': 'application/vnd.github+json',    # Fixed: Updated Accept header
            'X-GitHub-Api-Version': '2022-11-28'        # Fixed: Added API version
        }
        
        response = requests.get(api_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        file_data = response.json()
        content = base64.b64decode(file_data['content']).decode('utf-8')
        sha = file_data['sha']
        
        return content, sha
    except Exception as e:
        print(f"Error getting GitHub file info: {e}")
        return None, None

def update_github_csv(parts_list, sha, commit_message):
    """Update CSV file in GitHub repository - FIXED VERSION"""
    if not GITHUB_TOKEN:
        return False, "GitHub token not configured"
    
    try:
        # Convert parts list to CSV format
        output = io.StringIO()
        fieldnames = ['Product Code', 'Description', 'Category', 'Make', 'Manufacturer', 'image']
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        
        writer.writeheader()
        for part in parts_list:
            writer.writerow({
                'Product Code': part.get('product_code', ''),
                'Description': part.get('description', ''),
                'Category': part.get('category', ''),
                'Make': part.get('make', ''),
                'Manufacturer': part.get('manufacturer', ''),
                'image': part.get('image', '')
            })
        
        csv_content = output.getvalue()
        
        # Update in GitHub with fixed headers
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CSV_FILE_PATH}"
        headers = {
            'Authorization': f'Bearer {GITHUB_TOKEN}',  # Fixed: Use Bearer
            'Accept': 'application/vnd.github+json',    # Fixed: Updated Accept header
            'X-GitHub-Api-Version': '2022-11-28',       # Fixed: Added API version
            'Content-Type': 'application/json'          # Fixed: Added Content-Type
        }
        
        encoded_content = base64.b64encode(csv_content.encode('utf-8')).decode('utf-8')
        
        data = {
            'message': commit_message,
            'content': encoded_content,
            'sha': sha
        }
        
        response = requests.put(api_url, json=data, headers=headers, timeout=30)
        response.raise_for_status()
        
        return True, "Successfully updated GitHub repository"
        
    except Exception as e:
        print(f"GitHub update error: {e}")
        return False, f"Failed to update GitHub: {str(e)}"

# ── Email Functions ───────────────────────────────────────────────────────────

def build_html_email(sent_items, back_orders, dispatch, generated_at=None) -> str:
    generated_at = generated_at or datetime.utcnow()

    def esc(s):
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
    if not app.config["MAIL_PASSWORD"]:
        print("Warning: Email not configured")
        return
    
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
    lines.append("")

    lines.append("Items Sent:")
    if sent_items:
        for s in sent_items:
            lines.append(f"- {s.part_number} ({s.description or ''}): {s.quantity_sent}")
    else:
        lines.append("- (No items recorded on this dispatch)")

    if back_orders:
        lines.append("\nItems Still on Back Order:")
        for bo in back_orders:
            remaining = (bo.quantity or 0) - (bo.quantity_sent or 0)
            if remaining > 0:
                lines.append(f"- {bo.part_number} ({bo.description or ''}): {remaining}")

    lines.append("\nThank you,\nServitech Stock System")

    try:
        msg = Message(subject=subject, recipients=[engineer_email], body="\n".join(lines))
        msg.html = build_html_email(sent_items, back_orders, dispatch)
        mail.send(msg)
    except Exception as e:
        print(f"Error sending email: {e}")

# ── Main Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("home.html")

# ── Parts Order Management Routes ─────────────────────────────────────────────

@app.route('/admin/parts_orders_list')
def parts_orders_list():
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
    outstanding_items = get_outstanding_items(email)
    back_orders = get_back_orders(email)
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
            return render_template("parts_order_detail.html", email=email, outstanding_items=outstanding_items, back_orders=back_orders, engineer_dispatches=engineer_dispatches)

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

    return render_template("parts_order_detail.html", email=email, outstanding_items=outstanding_items, back_orders=back_orders, engineer_dispatches=engineer_dispatches)

@app.route('/admin/cancel_order_item/<int:item_id>', methods=['POST'])
def cancel_order_item(item_id: int):
    item = PartsOrderItem.query.get_or_404(item_id)
    engineer_email = item.order.email if item.order else request.form.get('email', '')
    part_num = item.part_number
    parent_order = item.order

    db.session.delete(item)
    db.session.flush()

    if parent_order and len(parent_order.items) == 0:
        db.session.delete(parent_order)

    db.session.commit()
    flash(f"Removed item {part_num} from the order.", "success")
    return redirect(url_for('parts_order_detail', email=engineer_email))

# ── Dispatch History Routes ───────────────────────────────────────────────────

@app.route("/admin/dispatched_orders")
def dispatched_orders():
    dispatches = db.session.query(DispatchNote).order_by(DispatchNote.date.desc()).all()
    return render_template("dispatched_orders.html", dispatches=dispatches)

@app.route("/admin/dispatch_note/<int:dispatch_id>")
def view_dispatch_note(dispatch_id: int):
    dispatch = DispatchNote.query.get_or_404(dispatch_id)
    sent_items = DispatchItem.query.filter_by(dispatch_note_id=dispatch_id).all()
    back_orders = get_back_orders(dispatch.engineer_email)
    return render_template("dispatch_note.html", dispatch=dispatch, sent_items=sent_items, back_orders=back_orders)

# ── Catalogue Management Routes (Fixed) ──────────────────────────────────────

@app.route("/admin/catalogue")
def catalogue_manager():
    try:
        search_query = request.args.get('search', '').strip()
        category_filter = request.args.get('category', '').strip()
        
        csv_content = fetch_csv_from_github()
        if csv_content is None:
            flash("Could not load CSV from GitHub. Check your connection.", "error")
            return render_template("catalogue_manager.html", parts=[], categories=[], search_query=search_query, category_filter=category_filter)
        
        all_parts = parse_csv_content(csv_content)
        
        parts = all_parts
        if search_query:
            search_lower = search_query.lower()
            parts = [p for p in parts if (
                search_lower in p['product_code'].lower() or
                search_lower in p.get('description', '').lower() or
                search_lower in p.get('make', '').lower() or
                search_lower in p.get('manufacturer', '').lower()
            )]
        
        if category_filter:
            parts = [p for p in parts if category_filter.lower() in p.get('category', '').lower()]
        
        categories = list(set(p.get('category', '') for p in all_parts if p.get('category', '')))
        categories.sort()
        
        return render_template("catalogue_manager.html", parts=parts, categories=categories, search_query=search_query, category_filter=category_filter)
                             
    except Exception as e:
        flash(f"Error loading catalogue: {str(e)}", "error")
        return render_template("catalogue_manager.html", parts=[], categories=[], search_query="", category_filter="")

@app.route("/admin/catalogue/part", methods=["POST"])
def add_part():
    try:
        product_code = request.form.get('product_code', '').strip()
        if not product_code:
            flash("Product code is required", "error")
            return redirect(url_for('catalogue_manager'))
        
        csv_content, sha = get_github_file_info()
        if csv_content is None:
            flash("Could not access GitHub API. Check your token configuration.", "error")
            return redirect(url_for('catalogue_manager'))
        
        parts = parse_csv_content(csv_content)
        
        if any(p['product_code'] == product_code for p in parts):
            flash(f"Part {product_code} already exists", "error")
            return redirect(url_for('catalogue_manager'))
        
        new_part = {
            'product_code': product_code,
            'description': request.form.get('description', '').strip(),
            'category': request.form.get('category', '').strip(),
            'make': request.form.get('make', '').strip(),
            'manufacturer': request.form.get('manufacturer', '').strip(),
            'image': request.form.get('image', '').strip()
        }
        parts.append(new_part)
        parts.sort(key=lambda x: x['product_code'])
        
        success, message = update_github_csv(parts, sha, f"Add new part: {product_code}")
        
        if success:
            flash(f"Successfully added part: {product_code}", "success")
        else:
            flash(f"Failed to add part: {message}", "error")
            
    except Exception as e:
        flash(f"Error adding part: {str(e)}", "error")
    
    return redirect(url_for('catalogue_manager'))

@app.route("/admin/catalogue/part/<product_code>", methods=["PUT", "DELETE"])
def update_or_delete_part(product_code):
    """FIXED: Update or delete a part directly in GitHub CSV"""
    try:
        # Get current CSV from GitHub API
        csv_content, sha = get_github_file_info()
        if csv_content is None:
            return jsonify({"success": False, "message": "Could not access GitHub API. Check your GITHUB_TOKEN."})
        
        # Parse existing parts
        parts = parse_csv_content(csv_content)
        
        # Find the part to update/delete
        part_index = None
        for i, part in enumerate(parts):
            if part['product_code'] == product_code:
                part_index = i
                break
        
        if part_index is None:
            return jsonify({"success": False, "message": "Part not found"})
        
        if request.method == "DELETE":
            # Remove the part
            deleted_part = parts.pop(part_index)
            commit_message = f"Delete part: {product_code}"
            
        elif request.method == "PUT":
            # Update the part
            data = request.get_json()
            if not data:
                return jsonify({"success": False, "message": "No data provided"})
                
            part = parts[part_index]
            
            # Update only the provided fields
            if 'description' in data:
                part['description'] = str(data['description']).strip()
            if 'category' in data:
                part['category'] = str(data['category']).strip()
            if 'make' in data:
                part['make'] = str(data['make']).strip()
            if 'manufacturer' in data:
                part['manufacturer'] = str(data['manufacturer']).strip()
            if 'image' in data:
                part['image'] = str(data['image']).strip()
            
            commit_message = f"Update part: {product_code}"
        
        # Update GitHub with better error handling
        success, message = update_github_csv(parts, sha, commit_message)
        
        return jsonify({"success": success, "message": message})
        
    except Exception as e:
        error_msg = f"Error processing request: {str(e)}"
        print(error_msg)  # Log the error
        return jsonify({"success": False, "message": error_msg})

@app.route("/admin/catalogue/export")
def export_catalogue():
    try:
        csv_content = fetch_csv_from_github()
        if csv_content is None:
            flash("Could not access GitHub CSV", "error")
            return redirect(url_for('catalogue_manager'))
        
        return Response(
            csv_content,
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=parts_catalogue.csv'}
        )
    except Exception as e:
        flash(f"Export failed: {str(e)}", "error")
        return redirect(url_for('catalogue_manager'))

# ── Legacy Routes ─────────────────────────────────────────────────────────────

@app.route("/admin/hidden-parts")
def hidden_parts_redirect():
    flash("The hidden parts feature has been upgraded to a full catalogue manager!", "info")
    return redirect(url_for('catalogue_manager'))

# ── Application Initialization ────────────────────────────────────────────────

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)

