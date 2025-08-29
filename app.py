# ──────────────────────────────────────────────────────────────────────────────
# Servitech STOCK SYSTEM - Production Version with Debug
# ──────────────────────────────────────────────────────────────────────────────

# ── Core Imports ──────────────────────────────────────────────────────────────
from datetime import datetime
import os
import csv
import io
import base64
import traceback
from types import SimpleNamespace

# ── Flask & Extensions ────────────────────────────────────────────────────────
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from sqlalchemy import func
import requests
from urllib.parse import unquote


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

# ── GitHub CSV Functions (Debug Version) ─────────────────────────────────────

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
    """DEBUG VERSION: Get file content and SHA from GitHub API"""
    print("=== DEBUG: get_github_file_info called ===")
    
    if not GITHUB_TOKEN:
        print("ERROR: No GITHUB_TOKEN provided")
        return None, None
    
    print(f"Using GITHUB_TOKEN: {GITHUB_TOKEN[:10]}...")
    print(f"Repository: {GITHUB_REPO}")
    print(f"File path: {CSV_FILE_PATH}")
    
    try:
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CSV_FILE_PATH}"
        print(f"API URL: {api_url}")
        
        headers = {
            'Authorization': f'Bearer {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'
        }
        print(f"Request headers: {headers}")
        
        print("Making request to GitHub API...")
        response = requests.get(api_url, headers=headers, timeout=10)
        print(f"Response status: {response.status_code}")
        print(f"Response headers: {dict(response.headers)}")
        
        if response.status_code != 200:
            print(f"ERROR: GitHub API returned {response.status_code}")
            print(f"Response text: {response.text}")
            return None, None
        
        response.raise_for_status()
        
        file_data = response.json()
        print(f"File data keys: {file_data.keys()}")
        print(f"File SHA: {file_data.get('sha', 'NOT FOUND')}")
        
        content = base64.b64decode(file_data['content']).decode('utf-8')
        sha = file_data['sha']
        
        print(f"Decoded content length: {len(content)} characters")
        print(f"First 100 chars: {content[:100]}")
        
        return content, sha
    except Exception as e:
        print(f"Exception in get_github_file_info: {str(e)}")
        traceback.print_exc()
        return None, None

def update_github_csv(parts_list, sha, commit_message):
    """DEBUG VERSION: Update CSV file in GitHub repository"""
    print("=== DEBUG: update_github_csv called ===")
    print(f"Parts to write: {len(parts_list)}")
    print(f"SHA: {sha}")
    print(f"Commit message: {commit_message}")
    
    if not GITHUB_TOKEN:
        error_msg = "GitHub token not configured"
        print(f"ERROR: {error_msg}")
        return False, error_msg
    
    try:
        # Convert parts list to CSV format
        print("Converting parts to CSV format...")
        output = io.StringIO()
        fieldnames = ['Product Code', 'Description', 'Category', 'Make', 'Manufacturer', 'image']
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        
        writer.writeheader()
        for i, part in enumerate(parts_list):
            print(f"Writing part {i}: {part['product_code']}")
            writer.writerow({
                'Product Code': part.get('product_code', ''),
                'Description': part.get('description', ''),
                'Category': part.get('category', ''),
                'Make': part.get('make', ''),
                'Manufacturer': part.get('manufacturer', ''),
                'image': part.get('image', '')
            })
        
        csv_content = output.getvalue()
        print(f"Generated CSV length: {len(csv_content)} characters")
        print(f"First 200 chars: {csv_content[:200]}")
        
        # Update in GitHub with fixed headers
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CSV_FILE_PATH}"
        print(f"Update API URL: {api_url}")
        
        headers = {
            'Authorization': f'Bearer {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'Content-Type': 'application/json'
        }
        print(f"Update headers: {headers}")
        
        encoded_content = base64.b64encode(csv_content.encode('utf-8')).decode('utf-8')
        print(f"Encoded content length: {len(encoded_content)}")
        
        data = {
            'message': commit_message,
            'content': encoded_content,
            'sha': sha
        }
        print(f"Request data keys: {data.keys()}")
        
        print("Making PUT request to GitHub...")
        response = requests.put(api_url, json=data, headers=headers, timeout=30)
        print(f"PUT response status: {response.status_code}")
        print(f"PUT response headers: {dict(response.headers)}")
        
        if response.status_code not in [200, 201]:
            print(f"ERROR: GitHub PUT returned {response.status_code}")
            print(f"Response text: {response.text}")
            return False, f"GitHub API error: {response.status_code} - {response.text}"
        
        response.raise_for_status()
        
        print("GitHub update successful!")
        return True, "Successfully updated GitHub repository"
        
    except Exception as e:
        error_msg = f"Exception in update_github_csv: {str(e)}"
        print(f"ERROR: {error_msg}")
        traceback.print_exc()
        return False, error_msg

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

# ── Catalogue Management Routes (Debug Version) ──────────────────────────────

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

@app.route("/admin/catalogue/part/<path:product_code>", methods=["PUT", "DELETE"])
def update_or_delete_part(product_code):
    """FIXED VERSION: Handle special characters in product codes"""
    # Decode URL-encoded product code
    product_code = unquote(product_code)
    print(f"=== DEBUG: Received {request.method} request for product_code: '{product_code}' ===")
    
    try:
        # Log request details
        print(f"Request method: {request.method}")
        print(f"Decoded product code: '{product_code}'")
        
        if request.method == "PUT":
            print(f"Request JSON: {request.get_json()}")
        
        # Check GitHub token
        print(f"GITHUB_TOKEN set: {bool(GITHUB_TOKEN)}")
        
        # Get current CSV from GitHub API
        print("Attempting to fetch CSV from GitHub...")
        csv_content, sha = get_github_file_info()
        print(f"CSV content received: {bool(csv_content)}")
        print(f"SHA received: {sha[:8] if sha else None}...")
        
        if csv_content is None:
            error_msg = "Could not access GitHub API. Check your GITHUB_TOKEN."
            print(f"ERROR: {error_msg}")
            return jsonify({"success": False, "message": error_msg})
        
        # Parse existing parts
        print("Parsing CSV content...")
        parts = parse_csv_content(csv_content)
        print(f"Parts parsed: {len(parts)} parts found")
        
        # Find the part to update/delete
        print(f"Looking for part with product_code: '{product_code}'")
        part_index = None
        for i, part in enumerate(parts):
            if part['product_code'] == product_code:
                part_index = i
                print(f"Found matching part at index {i}")
                break
        
        if part_index is None:
            error_msg = f"Part '{product_code}' not found in {len(parts)} parts"
            print(f"ERROR: {error_msg}")
            return jsonify({"success": False, "message": error_msg})
        
        print(f"Found part at index {part_index}: {parts[part_index]}")
        
        if request.method == "DELETE":
            print("Processing DELETE request...")
            deleted_part = parts.pop(part_index)
            commit_message = f"Delete part: {product_code}"
            print(f"Deleted part: {deleted_part}")
            
        elif request.method == "PUT":
            print("Processing PUT request...")
            data = request.get_json()
            if not data:
                error_msg = "No JSON data provided in PUT request"
                print(f"ERROR: {error_msg}")
                return jsonify({"success": False, "message": error_msg})
                
            print(f"Update data received: {data}")
            part = parts[part_index]
            print(f"Original part: {part}")
            
            # Update only the provided fields
            if 'description' in data:
                old_val = part['description']
                part['description'] = str(data['description']).strip()
                print(f"Updated description: '{old_val}' -> '{part['description']}'")
            if 'category' in data:
                old_val = part['category']
                part['category'] = str(data['category']).strip()
                print(f"Updated category: '{old_val}' -> '{part['category']}'")
            if 'make' in data:
                old_val = part['make']
                part['make'] = str(data['make']).strip()
                print(f"Updated make: '{old_val}' -> '{part['make']}'")
            if 'manufacturer' in data:
                old_val = part['manufacturer']
                part['manufacturer'] = str(data['manufacturer']).strip()
                print(f"Updated manufacturer: '{old_val}' -> '{part['manufacturer']}'")
            if 'image' in data:
                old_val = part['image']
                part['image'] = str(data['image']).strip()
                print(f"Updated image: '{old_val}' -> '{part['image']}'")
            
            commit_message = f"Update part: {product_code}"
            print(f"Updated part: {part}")
        
        # Update GitHub
        print(f"Attempting to update GitHub with commit message: '{commit_message}'")
        print(f"Total parts to write: {len(parts)}")
        
        success, message = update_github_csv(parts, sha, commit_message)
        print(f"GitHub update result - Success: {success}, Message: '{message}'")
        
        return jsonify({"success": success, "message": message})
        
    except Exception as e:
        error_msg = f"Exception in update_or_delete_part: {str(e)}"
        print(f"ERROR: {error_msg}")
        print("Full traceback:")
        traceback.print_exc()
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

@app.route("/admin/catalogue/debug_test")
def debug_test():
    """Test route to check GitHub connectivity"""
    print("=== DEBUG TEST ROUTE ===")
    
    # Test 1: Check environment variables
    print(f"GITHUB_TOKEN set: {bool(GITHUB_TOKEN)}")
    print(f"GITHUB_REPO: {GITHUB_REPO}")
    print(f"CSV_FILE_PATH: {CSV_FILE_PATH}")
    
    # Test 2: Try to fetch CSV
    print("Testing CSV fetch...")
    csv_content = fetch_csv_from_github()
    print(f"Public CSV fetch success: {bool(csv_content)}")
    
    # Test 3: Try GitHub API
    print("Testing GitHub API...")
    api_content, sha = get_github_file_info()
    print(f"API fetch success: {bool(api_content and sha)}")
    
    # Test 4: Parse parts
    parts = []
    if csv_content:
        parts = parse_csv_content(csv_content)
        print(f"Parsed {len(parts)} parts")
        if parts:
            print(f"First part: {parts[0]}")
    
    return f"""
    <div style="font-family: system-ui; padding: 40px; max-width: 700px; margin: 0 auto;">
        <h2>Debug Test Results</h2>
        <div style="background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <h4>Configuration:</h4>
            <ul>
                <li><strong>GITHUB_TOKEN:</strong> {'✅ Set (' + str(len(GITHUB_TOKEN)) + ' chars)' if GITHUB_TOKEN else '❌ Not set'}</li>
                <li><strong>GITHUB_REPO:</strong> {GITHUB_REPO}</li>
                <li><strong>CSV_FILE_PATH:</strong> {CSV_FILE_PATH}</li>
            </ul>
        </div>
        
        <div style="background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <h4>Connectivity Tests:</h4>
            <ul>
                <li><strong>Public CSV fetch:</strong> {'✅ Success' if csv_content else '❌ Failed'}</li>
                <li><strong>GitHub API fetch:</strong> {'✅ Success' if api_content and sha else '❌ Failed'}</li>
                <li><strong>Parts parsed:</strong> {len(parts) if parts else 0}</li>
            </ul>
        </div>
        
        {f'<div style="background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0;"><h4>Sample Data:</h4><pre>{str(parts[0]) if parts else "No parts found"}</pre></div>' if parts else ''}
        
        <p><a href="/admin/catalogue">← Back to Catalogue</a></p>
    </div>
    """


# Add this test route to your app.py to debug edit/delete

@app.route("/admin/catalogue/test_edit", methods=["GET", "POST"])
def test_edit():
    """Simple test route to debug edit functionality"""
    if request.method == "POST":
        product_code = request.form.get('product_code')
        field_name = request.form.get('field_name') 
        new_value = request.form.get('new_value')
        
        print(f"=== TEST EDIT FORM DATA ===")
        print(f"Product Code: '{product_code}'")
        print(f"Field Name: '{field_name}'")
        print(f"New Value: '{new_value}'")
        
        # Try the same logic as the real edit
        csv_content, sha = get_github_file_info()
        if csv_content:
            parts = parse_csv_content(csv_content)
            print(f"Found {len(parts)} parts")
            
            # Find the part
            part_index = None
            for i, part in enumerate(parts):
                if part['product_code'] == product_code:
                    part_index = i
                    break
            
            if part_index is not None:
                print(f"Found part at index {part_index}")
                parts[part_index][field_name] = new_value
                success, message = update_github_csv(parts, sha, f"Test update: {product_code}")
                return f"<h2>Update Result</h2><p>Success: {success}</p><p>Message: {message}</p><p><a href='/admin/catalogue/test_edit'>Try Again</a></p>"
            else:
                return f"<h2>Error</h2><p>Part '{product_code}' not found</p><p><a href='/admin/catalogue/test_edit'>Try Again</a></p>"
        else:
            return "<h2>Error</h2><p>Could not fetch CSV</p><p><a href='/admin/catalogue/test_edit'>Try Again</a></p>"
    
    # GET request - show test form
    return """
    <div style="font-family: system-ui; padding: 40px; max-width: 600px; margin: 0 auto;">
        <h2>Test Edit Functionality</h2>
        <form method="post">
            <div style="margin-bottom: 15px;">
                <label>Product Code:</label><br>
                <input name="product_code" value="01-11-300" style="width: 100%; padding: 8px;" placeholder="e.g. 01-11-300">
            </div>
            <div style="margin-bottom: 15px;">
                <label>Field to Update:</label><br>
                <select name="field_name" style="width: 100%; padding: 8px;">
                    <option value="description">Description</option>
                    <option value="category">Category</option>
                    <option value="make">Make</option>
                    <option value="manufacturer">Manufacturer</option>
                    <option value="image">Image</option>
                </select>
            </div>
            <div style="margin-bottom: 15px;">
                <label>New Value:</label><br>
                <input name="new_value" style="width: 100%; padding: 8px;" placeholder="Enter new value">
            </div>
            <button type="submit" style="background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px;">Test Update</button>
        </form>
        <p><a href="/admin/catalogue">← Back to Catalogue</a></p>
    </div>
    """

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



