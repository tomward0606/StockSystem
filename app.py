from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'devkey'
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://servitech_db_user:79U6KaAxlHdUfOeEt1iVDc65KXFLPie2@dpg-d1ckf9ur433s73fti9p0-a.oregon-postgres.render.com/servitech_db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# MODELS
class PartsOrder(db.Model):
    __tablename__ = 'parts_order'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), nullable=True)
    items = db.relationship("PartsOrderItem", backref="order", cascade="all, delete-orphan")

class PartsOrderItem(db.Model):
    __tablename__ = 'parts_order_item'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('parts_order.id'), nullable=False)
    part_number = db.Column(db.String(64))
    description = db.Column(db.String(256))
    quantity = db.Column(db.Integer)
    quantity_sent = db.Column(db.Integer, default=0)

class DispatchNote(db.Model):
    __tablename__ = 'dispatch_note'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('parts_order.id'), nullable=False)
    engineer_email = db.Column(db.String(120), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    items = db.relationship("DispatchItem", backref="dispatch_note", cascade="all, delete-orphan")

class DispatchItem(db.Model):
    __tablename__ = 'dispatch_item'
    id = db.Column(db.Integer, primary_key=True)
    dispatch_note_id = db.Column(db.Integer, db.ForeignKey('dispatch_note.id'), nullable=False)
    part_number = db.Column(db.String(64))
    quantity_sent = db.Column(db.Integer)

# ROUTES
@app.route('/')
def home():
    return render_template('home.html')

@app.route('/admin/parts_orders_list')
def parts_orders_list():
    orders = PartsOrder.query.order_by(PartsOrder.date.desc()).all()
    return render_template('parts_orders_list.html', orders=orders)

@app.route('/admin/parts_order_detail/<int:order_id>', methods=['GET', 'POST'])
def parts_order_detail(order_id):
    order = PartsOrder.query.get_or_404(order_id)

    if request.method == 'POST':
        if request.form.get('dispatch') == 'true':
            # Handle dispatch submission
            dispatch = DispatchNote(order_id=order.id, engineer_email=order.email)
            db.session.add(dispatch)

            for item in order.items:
                send_key = f'send_{item.id}'
                if send_key in request.form:
                    try:
                        to_send = int(request.form[send_key])
                    except ValueError:
                        to_send = 0

                    remaining = item.quantity - item.quantity_sent
                    if 0 < to_send <= remaining:
                        dispatch_item = DispatchItem(
                            dispatch_note=dispatch,
                            part_number=item.part_number,
                            quantity_sent=to_send
                        )
                        db.session.add(dispatch_item)
                        item.quantity_sent += to_send

            db.session.commit()
            flash("Dispatch recorded successfully.", "success")
            return redirect(url_for('parts_order_detail', order_id=order.id))

        else:
            # Handle status update
            new_status = request.form.get('status')
            order.status = new_status
            db.session.commit()
            flash(f"Order #{order.id} status updated.", "success")
            return redirect(url_for('parts_order_detail', order_id=order.id))

    return render_template('parts_order_detail.html', order=order)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
