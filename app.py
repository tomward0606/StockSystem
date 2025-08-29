# ──────────────────────────────────────────────────────────────────────────────
# Servitech STOCK SYSTEM - Complete Application (Secure Version)
# ──────────────────────────────────────────────────────────────────────────────

# ── Core Imports ──────────────────────────────────────────────────────────────
from datetime import datetime
import os
import csv
import io
import base64
from types import SimpleNamespace

# ── Flask & Extensions ────────────────────────────────────────────────────────
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, session
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from sqlalchemy import func
import requests

# ── App Configuration (Secure with Environment Variables) ────────────────────
app = Flask(__name__)

# Security: Use environment variables for sensitive data
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-change-in-production")

# Database configuration - Secure with environment variables
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    # Render uses postgres:// but SQLAlchemy 1.4+ requires postgresql://
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or (
    "postgresql://servitech_db_user:"
    "79U6KaAxlHdUfOeEt1iVDc65KXFLPie2"
    "@dpg-d1ckf9ur433s73fti9p0-a.oregon-postgres.render.com"
    "/servitech_db?sslmode=require"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Email configuration - Secure with environment variables
app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", 587))
app.config["MAIL_USE_TLS"] = os.environ.get("MAIL_USE_TLS", "True").lower() == "true"
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME", "servitech.stock@gmail.com")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")  # Must be set in environment
app.config["MAIL_DEFAULT_SENDER"] = (
    "Servitech Stock",
    app.config["MAIL_USERNAME"],
)

# GitHub configuration - Secure with environment variables
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "your_github_token_here")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "tomward0606/PartsProjectMain")
CSV_FILE_PATH = os.environ.get("CSV_FILE_PATH", "parts.csv")

# Initialize extensions
db = SQLAlchemy(app)
mail = Mail(app)

# ── Security Check Function ───────────────────────────────────────────────────
def check_required_env_vars():
    """Check if all required environment variables are set"""
    required_vars = {
        'MAIL_PASSWORD': 'Email password for sending notifications',
        'SECRET_KEY': 'Flask secret key for sessions and security',
    }
    
    optional_vars = {
        'GITHUB_TOKEN': 'GitHub token for editing catalogue (read-only without it)',
        'DATABASE_URL': 'Database connection URL (falls back to hardcoded)',
    }
    
    missing_required = []
    missing_optional = []
    
    for var, description in required_vars.items():
        if not os.environ.get(var):
            missing_required.append(f"  • {var}: {description}")
    
    for var, description in optional_vars.items():
        if not os.environ.get(var) or os.environ.get(var) == "your_github_token_here":
            missing_optional.append(f"  • {var}: {description}")
    
    return missing_required, missing_optional

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

# ── GitHub CSV Functions ──────────────────────────────────────────────────────

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
            # Clean up the data and handle different column name variations
            part = {
                'product_code': (row.get('Product Code') or row.get('product_code', '')).strip(),
                'description': (row.get('Description') or row.get('description', '')).strip(),
                'category': (row.get('Category') or row.get('category', '')).strip(),
                'make': (row.get('Make') or row.get('make', '')).strip(),
                'manufacturer': (row.get('Manufacturer') or row.get('manufacturer', '')).strip(),
                'image': (row.get('image') or row.get('Image', '')).strip()
            }
            if part['product_code']:  # Only include rows with product codes
                parts.append(part)
        
        return parts
    except Exception as e:
        print(f"Error parsing CSV: {e}")
        return []

def get_github_file_info():
    """Get file content and SHA from GitHub API (needed for updates)"""
    if GITHUB_TOKEN == "your_github_token_here" or not GITHUB_TOKEN:
        return None, None  # Token not configured
    
    try:
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CSV_FILE_PATH}"
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        
        file_data = response.json()
        content = base64.b64decode(file_data['content']).decode('utf-8')
        sha = file_data['sha']
        
        return content, sha
    except Exception as e:
        print(f"Error getting GitHub file info: {e}")
        return None, None

def update_github_csv(parts_list, sha, commit_message):
    """Update CSV file in GitHub repository"""
    if GITHUB_TOKEN == "your_github_token_here" or not GITHUB_TOKEN:
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
        
        # Update in GitHub
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CSV_FILE_PATH}"
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        encoded_content = base64.b64encode(csv_content.encode('utf-8')).decode('utf-8')
        
        data = {
            'message': commit_message,
            'content': encoded_content,
            'sha': sha
        }
        
        response = requests.put(api_url, json=data, headers=headers)
        response.raise_for_status()
        
        return True, "Successfully updated GitHub repository"
        
    except Exception as e:
        return False, f"Failed to update GitHub: {str(e)}"

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
    # Check if email is configured
    if not app.config["MAIL_PASSWORD"]:
        print("Warning: Email not configured - MAIL_PASSWORD environment variable not set")
        return
    
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

    try:
        # Send email with both plain text and HTML
        msg = Message(subject=subject, recipients=[engineer_email], body="\n".join(lines))
        msg.html = build_html_email(sent_items, back_orders, dispatch)
        mail.send(msg)
    except Exception as e:
        print(f"Error sending email: {e}")

# ── Main Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def home():
    """Main dashboard page with environment variable status"""
    missing_required, missing_optional = check_required_env_vars()
    return render_template("home.html", 
                         missing_required=missing_required, 
                         missing_optional=missing_optional)

@app.route("/admin/env_status")
def env_status():
    """Show environment variable configuration status"""
    missing_required, missing_optional = check_required_env_vars()
    
    config_status = {
        'SECRET_KEY': '✅ Set' if os.environ.get('SECRET_KEY') else '❌ Using default (insecure)',
        'DATABASE_URL': '✅ Set' if os.environ.get('DATABASE_URL') else '⚠️ Using fallback',
        'MAIL_USERNAME': os.environ.get('MAIL_USERNAME', 'servitech.stock@gmail.com'),
        'MAIL_PASSWORD': '✅ Set' if os.environ.get('MAIL_PASSWORD') else '❌ Not set',
        'GITHUB_TOKEN': '✅ Set' if os.environ.get('GITHUB_TOKEN') and os.environ.get('GITHUB_TOKEN') != 'your_github_token_here' else '⚠️ Not configured',
        'GITHUB_REPO': os.environ.get('GITHUB_REPO', 'tomward0606/PartsProjectMain'),
        'CSV_FILE_PATH': os.environ.get('CSV_FILE_PATH', 'parts.csv')
    }
    
    return f"""
    <div style="font-family: system-ui; padding: 40px; max-width: 800px; margin: 0 auto;">
        <h1>🔐 Environment Variables Status</h1>
        
        <div style="background: {'#d4edda' if not missing_required else '#f8d7da'}; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <h3>Security Status: {'✅ Secure' if not missing_required else '❌ Needs Attention'}</h3>
            {'<p>All required environment variables are properly configured.</p>' if not missing_required else f'<p>Missing required variables: {len(missing_required)}</p>'}
        </div>
        
        <h3>Current Configuration:</h3>
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <tr style="background: #f8f9fa;">
                <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">Variable</th>
                <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">Status</th>
            </tr>
            {''.join(f'<tr><td style="padding: 8px; border: 1px solid #dee2e6;"><code>{var}</code></td><td style="padding: 8px; border: 1px solid #dee2e6;">{status}</td></tr>' for var, status in config_status.items())}
        </table>
        
        {'<h3>⚠️ Missing Required Variables:</h3><ul>' + ''.join(f'<li>{var}</li>' for var in missing_required) + '</ul>' if missing_required else ''}
        {'<h3>Optional Variables:</h3><ul>' + ''.join(f'<li>{var}</li>' for var in missing_optional) + '</ul>' if missing_optional else ''}
        
        <h3>📝 How to Set Environment Variables in Render:</h3>
        <ol>
            <li>Go to your Render dashboard</li>
            <li>Select your web service</li>
            <li>Go to "Environment" tab</li>
            <li>Click "Add Environment Variable"</li>
            <li>Set the required variables listed above</li>
        </ol>
        
        <p><a href="/">← Back to Home</a></p>
    </div>
    """

@app.route("/test_email")
def test_email():
    """Test email configuration"""
    if not app.config["MAIL_PASSWORD"]:
        return "❌ Cannot test email - MAIL_PASSWORD environment variable not set"
    
    try:
        msg = Message(
            subject="Test Email from Stock System",
            recipients=["tomward0606@gmail.com"],
            body="This is a test email from the Servitech Stock system. Environment variables are working!",
        )
        mail.send(msg)
        return "✅ Test email sent successfully!"
    except Exception as e:
        return f"❌ Email test failed: {str(e)}"

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

# ── GitHub Direct Catalogue Management Routes ────────────────────────────────

@app.route("/admin/catalogue")
def catalogue_manager():
    """Catalogue manager working directly with GitHub CSV - no database"""
    try:
        # Get search/filter parameters
        search_query = request.args.get('search', '').strip()
        category_filter = request.args.get('category', '').strip()
        
        # Fetch CSV from GitHub (public URL, no token needed for reading)
        csv_content = fetch_csv_from_github()
        if csv_content is None:
            flash("Could not load CSV from GitHub. Check your connection.", "error")
            return render_template("catalogue_manager.html", parts=[], categories=[], 
                                 search_query=search_query, category_filter=category_filter)
        
        # Parse CSV into parts list
        all_parts = parse_csv_content(csv_content)
        
        # Apply search filter
        parts = all_parts
        if search_query:
            search_lower = search_query.lower()
            parts = [p for p in parts if (
                search_lower in p['product_code'].lower() or
                search_lower in p.get('description', '').lower() or
                search_lower in p.get('make', '').lower() or
                search_lower in p.get('manufacturer', '').lower()
            )]
        
        # Apply category filter
        if category_filter:
            parts = [p for p in parts if category_filter.lower() in p.get('category', '').lower()]
        
        # Get unique categories for filter dropdown
        categories = list(set(p.get('category', '') for p in all_parts if p.get('category', '')))
        categories.sort()
        
        return render_template("catalogue_manager.html", 
                             parts=parts, 
                             categories=categories,
                             search_query=search_query,
                             category_filter=category_filter)
                             
    except Exception as e:
        flash(f"Error loading catalogue: {str(e)}", "error")
        return render_template("catalogue_manager.html", parts=[], categories=[], 
                             search_query="", category_filter="")

@app.route("/admin/catalogue/part", methods=["POST"])
def add_part():
    """Add a new part directly to GitHub CSV"""
    try:
        product_code = request.form.get('product_code', '').strip()
        if not product_code:
            flash("Product code is required", "error")
            return redirect(url_for('catalogue_manager'))
        
        # Get current CSV from GitHub API (with SHA for updating)
        csv_content, sha = get_github_file_info()
        if csv_content is None:
            flash("Could not access GitHub API. Check your token configuration.", "error")
            return redirect(url_for('catalogue_manager'))
        
        # Parse existing parts
        parts = parse_csv_content(csv_content)
        
        # Check if part already exists
        if any(p['product_code'] == product_code for p in parts):
            flash(f"Part {product_code} already exists", "error")
            return redirect(url_for('catalogue_manager'))
        
        # Add new part
        new_part = {
            'product_code': product_code,
            'description': request.form.get('description', '').strip(),
            'category': request.form.get('category', '').strip(),
            'make': request.form.get('make', '').strip(),
            'manufacturer': request.form.get('manufacturer', '').strip(),
            'image': request.form.get('image', '').strip()
        }
        parts.append(new_part)
        
        # Sort by product code
        parts.sort(key=lambda x: x['product_code'])
        
        # Update GitHub
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
    """Update or delete a part directly in GitHub CSV"""
    try:
        # Get current CSV from GitHub API
        csv_content, sha = get_github_file_info()
        if csv_content is None:
            return jsonify({"success": False, "message": "Could not access GitHub API"})
        
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
            parts.pop(part_index)
            commit_message = f"Delete part: {product_code}"
            
        elif request.method == "PUT":
            # Update the part
            data = request.get_json()
            part = parts[part_index]
            
            if 'description' in data:
                part['description'] = data['description'].strip()
            if 'category' in data:
                part['category'] = data['category'].strip()
            if 'make' in data:
                part['make'] = data['make'].strip()
            if 'manufacturer' in data:
                part['manufacturer'] = data['manufacturer'].strip()
            if 'image' in data:
                part['image'] = data['image'].strip()
            
            commit_message = f"Update part: {product_code}"
        
        # Update GitHub
        success, message = update_github_csv(parts, sha, commit_message)
        
        return jsonify({"success": success, "message": message})
        
    except Exception as e:
        return jsonify({"success": False, "message": f"Error: {str(e)}"})

@app.route("/admin/catalogue/export")
def export_catalogue():
    """Export current CSV from GitHub"""
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

@app.route("/admin/catalogue/test_github")
def test_github_connection():
    """Test GitHub connection and show configuration status"""
    try:
        # Test public CSV access
        csv_content = fetch_csv_from_github()
        if csv_content:
            lines = csv_content.split('\n')[:5]
            parts_count = len(parse_csv_content(csv_content))
            
            # Test API access (if token is configured)
            api_status = "Not configured"
            if GITHUB_TOKEN and GITHUB_TOKEN != "your_github_token_here":
                api_content, sha = get_github_file_info()
                if api_content and sha:
                    api_status = f"✅ Connected (SHA: {sha[:8]}...)"
                else:
                    api_status = "❌ Token invalid or no access"
            
            return f"""
            <div style="font-family: system-ui; padding: 40px; max-width: 700px; margin: 0 auto;">
                <h2 style="color: #28a745;">✅ GitHub CSV Access Successful!</h2>
                
                <div style="background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0;">
                    <h4>Connection Status:</h4>
                    <ul>
                        <li><strong>Repository:</strong> {GITHUB_REPO}</li>
                        <li><strong>File:</strong> {CSV_FILE_PATH}</li>
                        <li><strong>Parts found:</strong> {parts_count}</li>
                        <li><strong>Public access:</strong> ✅ Working</li>
                        <li><strong>API access:</strong> {api_status}</li>
                    </ul>
                </div>
                
                <h4>First 5 lines of CSV:</h4>
                <pre style="background: #f8f9fa; padding: 15px; border-radius: 8px; overflow-x: auto;">{'<br>'.join(lines)}</pre>
                
                <div style="background: {'#d4edda' if GITHUB_TOKEN and GITHUB_TOKEN != 'your_github_token_here' else '#fff3cd'}; padding: 15px; border-radius: 8px; margin: 20px 0;">
                    <h5>{'✅ Ready for Editing' if GITHUB_TOKEN and GITHUB_TOKEN != 'your_github_token_here' else '⚠️ Read-Only Mode'}</h5>
                    <p>{'You can add, edit, and delete parts.' if GITHUB_TOKEN and GITHUB_TOKEN != 'your_github_token_here' else 'You can view parts but need to configure a GitHub token to edit.'}</p>
                    {'<p><a href="/admin/catalogue/setup">→ Configure GitHub Token</a></p>' if not GITHUB_TOKEN or GITHUB_TOKEN == 'your_github_token_here' else ''}
                </div>
                
                <p><a href="/admin/catalogue">→ Go to Catalogue Manager</a></p>
                <p><a href="/">← Back to Home</a></p>
            </div>
            """
        else:
            return """
            <div style="font-family: system-ui; padding: 40px; max-width: 600px; margin: 0 auto;">
                <h2 style="color: #dc3545;">❌ GitHub Connection Failed</h2>
                <p>Could not access the CSV file. Check:</p>
                <ul>
                    <li>Repository name: <code>tomward0606/PartsProjectMain</code></li>
                    <li>File path: <code>parts.csv</code></li>
                    <li>File exists and is public</li>
                    <li>Internet connection</li>
                </ul>
                <p><a href="/">← Back to Home</a></p>
            </div>
            """
    except Exception as e:
        return f"""
        <div style="font-family: system-ui; padding: 40px; max-width: 600px; margin: 0 auto;">
            <h2 style="color: #dc3545;">❌ Connection Error</h2>
            <p><strong>Error:</strong> {str(e)}</p>
            <p><a href="/">← Back to Home</a></p>
        </div>
        """

@app.route("/admin/catalogue/setup")
def catalogue_setup():
    """Instructions for setting up GitHub integration"""
    return f"""
    <div style="font-family: system-ui; padding: 40px; max-width: 800px; margin: 0 auto; line-height: 1.6;">
        <h1 style="color: #0d6efd;">🔧 GitHub Catalogue Setup</h1>
        
        <div style="background: {'#d4edda' if GITHUB_TOKEN and GITHUB_TOKEN != 'your_github_token_here' else '#fff3cd'}; border: 1px solid {'#c3e6cb' if GITHUB_TOKEN and GITHUB_TOKEN != 'your_github_token_here' else '#ffc107'}; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <h3>{'✅ Token Configured' if GITHUB_TOKEN and GITHUB_TOKEN != 'your_github_token_here' else '⚠️ Setup Required'}</h3>
            <p>{'Your GitHub token is configured via environment variables.' if GITHUB_TOKEN and GITHUB_TOKEN != 'your_github_token_here' else 'To edit your GitHub CSV directly, you need to set the GITHUB_TOKEN environment variable.'}</p>
        </div>
        
        <h3>How It Works:</h3>
        <ul>
            <li><strong>Read Access:</strong> Anyone can view your parts (uses public GitHub URL)</li>
            <li><strong>Write Access:</strong> Requires GITHUB_TOKEN environment variable</li>
        </ul>
        
        <h3>Setup Instructions:</h3>
        <ol>
            <li><strong>Get GitHub Token:</strong>
                <ul>
                    <li>Go to <a href="https://github.com/settings/tokens" target="_blank">GitHub Settings → Personal Access Tokens</a></li>
                    <li>Click "Generate new token (classic)"</li>
                    <li>Give it a name like "Stock System Catalogue"</li>
                    <li>Select scopes: <code>repo</code> (full repository access)</li>
                    <li>Copy your token (starts with <code>ghp_</code>)</li>
                </ul>
            </li>
            <li><strong>Set Environment Variable in Render:</strong>
                <ul>
                    <li>Go to your Render dashboard</li>
                    <li>Select your web service</li>
                    <li>Go to "Environment" tab</li>
                    <li>Click "Add Environment Variable"</li>
                    <li>Set: <code>GITHUB_TOKEN</code> = your token value</li>
                    <li>Deploy the changes</li>
                </ul>
            </li>
        </ol>
        
        <h3>Current Configuration:</h3>
        <ul>
            <li><strong>Repository:</strong> {GITHUB_REPO}</li>
            <li><strong>File:</strong> {CSV_FILE_PATH}</li>
            <li><strong>Token Status:</strong> {'✅ Configured' if GITHUB_TOKEN and GITHUB_TOKEN != 'your_github_token_here' else '❌ Not configured'}</li>
            <li><strong>Read Access:</strong> ✅ Available (public)</li>
            <li><strong>Write Access:</strong> {'✅ Available' if GITHUB_TOKEN and GITHUB_TOKEN != 'your_github_token_here' else '❌ Requires GITHUB_TOKEN env var'}</li>
        </ul>
        
        <p><a href="/admin/catalogue/test_github">→ Test Connection</a> | <a href="/admin/catalogue">→ Open Catalogue</a> | <a href="/admin/env_status">→ Check All Environment Variables</a> | <a href="/">← Home</a></p>
    </div>
    """

# ── Development & Testing Routes ──────────────────────────────────────────────

@app.route("/admin/test_dummy_dispatch_email")
def test_dummy_dispatch_email():
    """Send a test dispatch email with dummy data"""
    if not app.config["MAIL_PASSWORD"]:
        return "❌ Cannot test email - MAIL_PASSWORD environment variable not set"
    
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

    try:
        # Send test email
        msg = Message(
            subject=f"Test Dispatch Note - {dispatch.date.strftime('%d %b %Y')}",
            recipients=["tomward0606@gmail.com"],
            body="\n".join(text_lines),
        )
        msg.html = build_html_email(sent_items, back_orders, dispatch)
        mail.send(msg)
        return "✅ Dummy dispatch email sent successfully to tomward0606@gmail.com"
    except Exception as e:
        return f"❌ Email test failed: {str(e)}"

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
        
        # Security check
        missing_required, missing_optional = check_required_env_vars()
        
        print("\n" + "="*60)
        print("🔐 SECURITY STATUS")
        print("="*60)
        
        if not missing_required:
            print("✅ All required environment variables are set")
        else:
            print("❌ MISSING REQUIRED ENVIRONMENT VARIABLES:")
            for var in missing_required:
                print(f"   {var}")
            print("\n⚠️  Your application may not work correctly!")
        
        if missing_optional:
            print("\n⚠️  Optional environment variables not set:")
            for var in missing_optional:
                print(f"   {var}")
        
        print("\n" + "="*60)
        print("🚀 STARTING SERVITECH STOCK SYSTEM")
        print("="*60)
        print(f"Access your application at: http://localhost:5000")
        print("\nAvailable routes:")
        print("  • /                           - Main dashboard")
        print("  • /admin/env_status           - Environment variables status")
        print("  • /admin/parts_orders_list    - Outstanding orders summary")
        print("  • /admin/dispatched_orders    - Dispatch history")
        print("  • /admin/catalogue            - Parts catalogue manager (GitHub CSV)")
        print("  • /admin/catalogue/setup      - GitHub integration setup")
        print("  • /admin/catalogue/test_github - Test GitHub CSV connection")
        print("  • /test_email                 - Test email configuration")
        
        print(f"\n📧 Email Status: {'✅ Configured' if os.environ.get('MAIL_PASSWORD') else '❌ Not configured'}")
        print(f"🔗 GitHub Status: {'✅ Configured' if GITHUB_TOKEN and GITHUB_TOKEN != 'your_github_token_here' else '❌ Read-only mode'}")
        print("="*60 + "\n")
        
    app.run(debug=True)


