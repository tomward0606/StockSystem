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
    back_order = db.Column(db.Boolean, default=False)

class DispatchNote(db.Model):
    __tablename__ = 'dispatch_note'
    id = db.Column(db.Integer, primary_key=True)
    engineer_email = db.Column(db.String(120), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    picker_name = db.Column(db.String(100), nullable=True)  # NEW FIELD ADDED
    items = db.relationship("DispatchItem", backref="dispatch_note", cascade="all, delete-orphan")

class DispatchItem(db.Model):
    __tablename__ = 'dispatch_item'
    id = db.Column(db.Integer, primary_key=True)
    dispatch_note_id = db.Column(db.Integer, db.ForeignKey('dispatch_note.id'), nullable=False)
    part_number = db.Column(db.String(64))
    quantity_sent = db.Column(db.Integer)
    description = db.Column(db.String(256))  


# ROUTES
@app.route('/')
def home():
    return render_template('home.html')

@app.route('/admin/parts_orders_list')
def parts_orders_list():
    from sqlalchemy import func

    # Group by email, sum up outstanding quantities (quantity - quantity_sent)
    outstanding_data = db.session.query(
        PartsOrder.email,
        func.sum(PartsOrderItem.quantity - PartsOrderItem.quantity_sent).label("outstanding_total")
    ).join(PartsOrderItem).group_by(PartsOrder.email).having(
        func.sum(PartsOrderItem.quantity - PartsOrderItem.quantity_sent) > 0
    ).order_by(func.sum(PartsOrderItem.quantity - PartsOrderItem.quantity_sent).desc()).all()

    return render_template('parts_orders_list.html', data=outstanding_data)


@app.route('/admin/parts_order_detail/<email>', methods=['GET', 'POST'])
def parts_order_detail(email):
    from sqlalchemy import func

    # Fetch all outstanding items for this engineer
    items = db.session.query(PartsOrderItem).join(PartsOrder).filter(
        PartsOrder.email == email,
        PartsOrderItem.quantity > PartsOrderItem.quantity_sent
    ).all()

    # Fetch dispatch history for this engineer (NEW)
    engineer_dispatches = db.session.query(DispatchNote).filter(
        DispatchNote.engineer_email == email
    ).order_by(DispatchNote.date.desc()).all()

    if request.method == 'POST':
        # Get picker information from form
        picker_name = request.form.get('picker_name', '').strip()
        custom_picker_name = request.form.get('custom_picker_name', '').strip()
        
        # Use custom name if "other" was selected
        if picker_name == 'other' and custom_picker_name:
            final_picker_name = custom_picker_name
        elif picker_name and picker_name != 'other':
            final_picker_name = picker_name
        else:
            flash("Please select or enter a picker name.", "error")
            return render_template('parts_order_detail.html', email=email, items=items, engineer_dispatches=engineer_dispatches)

        # Create new dispatch note for the engineer
        dispatch = DispatchNote(engineer_email=email, picker_name=final_picker_name)
        db.session.add(dispatch)

        dispatch_created = False  # Track if any items were actually dispatched

        for item in items:
            send_key = f'send_{item.id}'
            back_order_key = f'back_order_{item.id}'

            to_send = int(request.form.get(send_key, 0) or 0)
            if 0 < to_send <= (item.quantity - item.quantity_sent):
                dispatch_item = DispatchItem(
                    dispatch_note=dispatch,
                    part_number=item.part_number,
                    description=item.description,
                    quantity_sent=to_send
                )
                db.session.add(dispatch_item)
                item.quantity_sent += to_send
                dispatch_created = True

            # Back order checkbox
            item.back_order = back_order_key in request.form

        if dispatch_created:
            db.session.commit()
            flash(f"Dispatch recorded successfully. Picked by: {final_picker_name}", "success")
        else:
            db.session.rollback()
            flash("No items were dispatched. Please enter quantities to dispatch.", "warning")
            
        return redirect(url_for('parts_order_detail', email=email))

    return render_template('parts_order_detail.html', email=email, items=items, engineer_dispatches=engineer_dispatches)

@app.route('/admin/dispatched_orders')
def dispatched_orders():
    dispatches = db.session.query(DispatchNote).order_by(DispatchNote.date.desc()).all()
    return render_template('dispatched_orders.html', dispatches=dispatches)



if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
