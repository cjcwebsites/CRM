from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
import json
import csv
import io
import os

app = Flask(__name__)

# Use DATABASE_URL env var in production (Neon/Supabase/etc.), SQLite locally
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///crm.db')
# Neon and some providers still emit postgres:// — SQLAlchemy requires postgresql://
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'crm-secret-key-change-in-prod')
db = SQLAlchemy(app)

# ─── Models ───────────────────────────────────────────────────────────────────

contact_tags = db.Table('contact_tags',
    db.Column('contact_id', db.Integer, db.ForeignKey('contact.id')),
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id'))
)

class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    color = db.Column(db.String(20), default='#6366f1')

class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    website = db.Column(db.String(200))
    industry = db.Column(db.String(100))
    phone = db.Column(db.String(50))
    email = db.Column(db.String(200))
    address = db.Column(db.Text)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    contacts = db.relationship('Contact', backref='company', lazy=True)
    deals = db.relationship('Deal', backref='company', lazy=True)

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'website': self.website,
            'industry': self.industry, 'phone': self.phone, 'email': self.email,
            'address': self.address, 'notes': self.notes,
            'contact_count': len(self.contacts), 'deal_count': len(self.deals),
            'created_at': self.created_at.strftime('%Y-%m-%d')
        }

class Contact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(200))
    phone = db.Column(db.String(50))
    mobile = db.Column(db.String(50))
    job_title = db.Column(db.String(100))
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    address = db.Column(db.Text)
    linkedin = db.Column(db.String(200))
    twitter = db.Column(db.String(100))
    status = db.Column(db.String(50), default='lead')  # lead, prospect, customer, churned
    source = db.Column(db.String(100))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_contacted = db.Column(db.DateTime)
    tags = db.relationship('Tag', secondary=contact_tags, backref='contacts')
    activities = db.relationship('Activity', backref='contact', lazy=True, cascade='all, delete-orphan')
    deals = db.relationship('Deal', backref='contact', lazy=True)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    def to_dict(self):
        return {
            'id': self.id, 'first_name': self.first_name, 'last_name': self.last_name,
            'full_name': self.full_name, 'email': self.email, 'phone': self.phone,
            'mobile': self.mobile, 'job_title': self.job_title,
            'company_id': self.company_id,
            'company_name': self.company.name if self.company else '',
            'address': self.address, 'linkedin': self.linkedin, 'twitter': self.twitter,
            'status': self.status, 'source': self.source, 'notes': self.notes,
            'created_at': self.created_at.strftime('%Y-%m-%d'),
            'last_contacted': self.last_contacted.strftime('%Y-%m-%d') if self.last_contacted else None,
            'tags': [{'id': t.id, 'name': t.name, 'color': t.color} for t in self.tags]
        }

class Deal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    value = db.Column(db.Float, default=0)
    currency = db.Column(db.String(10), default='USD')
    stage = db.Column(db.String(50), default='lead')  # lead, qualified, proposal, negotiation, won, lost
    probability = db.Column(db.Integer, default=0)  # 0-100
    contact_id = db.Column(db.Integer, db.ForeignKey('contact.id'))
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    owner = db.Column(db.String(100))
    close_date = db.Column(db.Date)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    activities = db.relationship('Activity', backref='deal', lazy=True, cascade='all, delete-orphan')

    STAGE_PROBABILITY = {
        'lead': 10, 'qualified': 25, 'proposal': 50,
        'negotiation': 75, 'won': 100, 'lost': 0
    }

    def to_dict(self):
        return {
            'id': self.id, 'title': self.title, 'value': self.value,
            'currency': self.currency, 'stage': self.stage,
            'probability': self.probability,
            'contact_id': self.contact_id,
            'contact_name': self.contact.full_name if self.contact else '',
            'company_id': self.company_id,
            'company_name': self.company.name if self.company else '',
            'owner': self.owner,
            'close_date': self.close_date.strftime('%Y-%m-%d') if self.close_date else None,
            'description': self.description,
            'created_at': self.created_at.strftime('%Y-%m-%d')
        }

class Activity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50), nullable=False)  # call, email, meeting, note, task
    subject = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    contact_id = db.Column(db.Integer, db.ForeignKey('contact.id'))
    deal_id = db.Column(db.Integer, db.ForeignKey('deal.id'))
    due_date = db.Column(db.DateTime)
    completed = db.Column(db.Boolean, default=False)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'type': self.type, 'subject': self.subject,
            'description': self.description,
            'contact_id': self.contact_id,
            'contact_name': self.contact.full_name if self.contact else '',
            'deal_id': self.deal_id,
            'deal_title': self.deal.title if self.deal else '',
            'due_date': self.due_date.strftime('%Y-%m-%dT%H:%M') if self.due_date else None,
            'completed': self.completed,
            'completed_at': self.completed_at.strftime('%Y-%m-%d %H:%M') if self.completed_at else None,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M')
        }

# ─── Page Routes ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
def dashboard():
    stats = {
        'total_contacts': Contact.query.count(),
        'total_companies': Company.query.count(),
        'open_deals': Deal.query.filter(Deal.stage.notin_(['won', 'lost'])).count(),
        'won_deals': Deal.query.filter_by(stage='won').count(),
        'pipeline_value': db.session.query(db.func.sum(Deal.value)).filter(
            Deal.stage.notin_(['won', 'lost'])).scalar() or 0,
        'won_value': db.session.query(db.func.sum(Deal.value)).filter_by(stage='won').scalar() or 0,
        'overdue_tasks': Activity.query.filter(
            Activity.type == 'task', Activity.completed == False,
            Activity.due_date < datetime.utcnow()).count(),
        'pending_tasks': Activity.query.filter(
            Activity.type == 'task', Activity.completed == False).count(),
    }
    recent_contacts = Contact.query.order_by(Contact.created_at.desc()).limit(5).all()
    recent_deals = Deal.query.order_by(Deal.created_at.desc()).limit(5).all()
    upcoming_activities = Activity.query.filter(
        Activity.completed == False,
        Activity.due_date >= datetime.utcnow()
    ).order_by(Activity.due_date).limit(8).all()

    stage_counts = {}
    for stage in ['lead', 'qualified', 'proposal', 'negotiation', 'won', 'lost']:
        stage_counts[stage] = Deal.query.filter_by(stage=stage).count()

    return render_template('dashboard.html', stats=stats,
                           recent_contacts=recent_contacts,
                           recent_deals=recent_deals,
                           upcoming_activities=upcoming_activities,
                           stage_counts=stage_counts)

@app.route('/contacts')
def contacts():
    q = request.args.get('q', '')
    status_filter = request.args.get('status', '')
    tag_filter = request.args.get('tag', '')
    query = Contact.query
    if q:
        query = query.filter(
            db.or_(
                Contact.first_name.ilike(f'%{q}%'),
                Contact.last_name.ilike(f'%{q}%'),
                Contact.email.ilike(f'%{q}%'),
                Contact.phone.ilike(f'%{q}%')
            )
        )
    if status_filter:
        query = query.filter_by(status=status_filter)
    if tag_filter:
        query = query.filter(Contact.tags.any(Tag.name == tag_filter))
    contacts = query.order_by(Contact.created_at.desc()).all()
    tags = Tag.query.order_by(Tag.name).all()
    companies = Company.query.order_by(Company.name).all()
    return render_template('contacts.html', contacts=contacts, tags=tags,
                           companies=companies, q=q,
                           status_filter=status_filter, tag_filter=tag_filter)

@app.route('/contacts/<int:id>')
def contact_detail(id):
    contact = Contact.query.get_or_404(id)
    companies = Company.query.order_by(Company.name).all()
    tags = Tag.query.order_by(Tag.name).all()
    activities = Activity.query.filter_by(contact_id=id).order_by(Activity.created_at.desc()).all()
    deals = Deal.query.filter_by(contact_id=id).order_by(Deal.created_at.desc()).all()
    return render_template('contact_detail.html', contact=contact,
                           companies=companies, tags=tags,
                           activities=activities, deals=deals)

@app.route('/companies')
def companies():
    q = request.args.get('q', '')
    query = Company.query
    if q:
        query = query.filter(
            db.or_(
                Company.name.ilike(f'%{q}%'),
                Company.industry.ilike(f'%{q}%'),
                Company.email.ilike(f'%{q}%')
            )
        )
    companies = query.order_by(Company.name).all()
    return render_template('companies.html', companies=companies, q=q)

@app.route('/companies/<int:id>')
def company_detail(id):
    company = Company.query.get_or_404(id)
    contacts = Contact.query.filter_by(company_id=id).all()
    deals = Deal.query.filter_by(company_id=id).order_by(Deal.created_at.desc()).all()
    return render_template('company_detail.html', company=company,
                           contacts=contacts, deals=deals)

@app.route('/deals')
def deals():
    stage_filter = request.args.get('stage', '')
    q = request.args.get('q', '')
    view = request.args.get('view', 'kanban')
    query = Deal.query
    if stage_filter:
        query = query.filter_by(stage=stage_filter)
    if q:
        query = query.filter(Deal.title.ilike(f'%{q}%'))
    all_deals = query.order_by(Deal.created_at.desc()).all()
    stages = ['lead', 'qualified', 'proposal', 'negotiation', 'won', 'lost']
    deals_by_stage = {s: [] for s in stages}
    for d in Deal.query.all():
        if d.stage in deals_by_stage:
            deals_by_stage[d.stage].append(d)
    contacts = Contact.query.order_by(Contact.first_name).all()
    companies = Company.query.order_by(Company.name).all()
    return render_template('deals.html', deals=all_deals,
                           deals_by_stage=deals_by_stage,
                           stages=stages, view=view,
                           contacts=contacts, companies=companies,
                           stage_filter=stage_filter, q=q)

@app.route('/activities')
def activities():
    type_filter = request.args.get('type', '')
    done_filter = request.args.get('done', '')
    q = request.args.get('q', '')
    query = Activity.query
    if type_filter:
        query = query.filter_by(type=type_filter)
    if done_filter == '0':
        query = query.filter_by(completed=False)
    elif done_filter == '1':
        query = query.filter_by(completed=True)
    if q:
        query = query.filter(Activity.subject.ilike(f'%{q}%'))
    activities = query.order_by(Activity.created_at.desc()).all()
    contacts = Contact.query.order_by(Contact.first_name).all()
    deals = Deal.query.order_by(Deal.title).all()
    return render_template('activities.html', activities=activities,
                           contacts=contacts, deals=deals,
                           type_filter=type_filter, done_filter=done_filter, q=q)

# ─── API Routes ───────────────────────────────────────────────────────────────

# Contacts API
@app.route('/api/contacts', methods=['GET'])
def api_contacts():
    contacts = Contact.query.order_by(Contact.created_at.desc()).all()
    return jsonify([c.to_dict() for c in contacts])

@app.route('/api/contacts', methods=['POST'])
def api_create_contact():
    data = request.json
    contact = Contact(
        first_name=data['first_name'], last_name=data['last_name'],
        email=data.get('email'), phone=data.get('phone'),
        mobile=data.get('mobile'), job_title=data.get('job_title'),
        company_id=data.get('company_id') or None,
        address=data.get('address'), linkedin=data.get('linkedin'),
        twitter=data.get('twitter'), status=data.get('status', 'lead'),
        source=data.get('source'), notes=data.get('notes')
    )
    if data.get('tag_ids'):
        contact.tags = Tag.query.filter(Tag.id.in_(data['tag_ids'])).all()
    db.session.add(contact)
    db.session.commit()
    return jsonify(contact.to_dict()), 201

@app.route('/api/contacts/<int:id>', methods=['PUT'])
def api_update_contact(id):
    contact = Contact.query.get_or_404(id)
    data = request.json
    for field in ['first_name', 'last_name', 'email', 'phone', 'mobile',
                  'job_title', 'address', 'linkedin', 'twitter', 'status',
                  'source', 'notes']:
        if field in data:
            setattr(contact, field, data[field])
    if 'company_id' in data:
        contact.company_id = data['company_id'] or None
    if 'tag_ids' in data:
        contact.tags = Tag.query.filter(Tag.id.in_(data['tag_ids'])).all()
    if 'last_contacted' in data and data['last_contacted']:
        contact.last_contacted = datetime.strptime(data['last_contacted'], '%Y-%m-%d')
    db.session.commit()
    return jsonify(contact.to_dict())

@app.route('/api/contacts/<int:id>', methods=['DELETE'])
def api_delete_contact(id):
    contact = Contact.query.get_or_404(id)
    db.session.delete(contact)
    db.session.commit()
    return jsonify({'success': True})

# Companies API
@app.route('/api/companies', methods=['POST'])
def api_create_company():
    data = request.json
    company = Company(
        name=data['name'], website=data.get('website'),
        industry=data.get('industry'), phone=data.get('phone'),
        email=data.get('email'), address=data.get('address'),
        notes=data.get('notes')
    )
    db.session.add(company)
    db.session.commit()
    return jsonify(company.to_dict()), 201

@app.route('/api/companies/<int:id>', methods=['PUT'])
def api_update_company(id):
    company = Company.query.get_or_404(id)
    data = request.json
    for field in ['name', 'website', 'industry', 'phone', 'email', 'address', 'notes']:
        if field in data:
            setattr(company, field, data[field])
    db.session.commit()
    return jsonify(company.to_dict())

@app.route('/api/companies/<int:id>', methods=['DELETE'])
def api_delete_company(id):
    company = Company.query.get_or_404(id)
    db.session.delete(company)
    db.session.commit()
    return jsonify({'success': True})

# Deals API
@app.route('/api/deals', methods=['POST'])
def api_create_deal():
    data = request.json
    deal = Deal(
        title=data['title'], value=data.get('value', 0),
        currency=data.get('currency', 'USD'),
        stage=data.get('stage', 'lead'),
        probability=data.get('probability', Deal.STAGE_PROBABILITY.get(data.get('stage', 'lead'), 10)),
        contact_id=data.get('contact_id') or None,
        company_id=data.get('company_id') or None,
        owner=data.get('owner'), description=data.get('description')
    )
    if data.get('close_date'):
        deal.close_date = datetime.strptime(data['close_date'], '%Y-%m-%d').date()
    db.session.add(deal)
    db.session.commit()
    return jsonify(deal.to_dict()), 201

@app.route('/api/deals/<int:id>', methods=['PUT'])
def api_update_deal(id):
    deal = Deal.query.get_or_404(id)
    data = request.json
    for field in ['title', 'value', 'currency', 'stage', 'probability', 'owner', 'description']:
        if field in data:
            setattr(deal, field, data[field])
    if 'contact_id' in data:
        deal.contact_id = data['contact_id'] or None
    if 'company_id' in data:
        deal.company_id = data['company_id'] or None
    if 'close_date' in data and data['close_date']:
        deal.close_date = datetime.strptime(data['close_date'], '%Y-%m-%d').date()
    db.session.commit()
    return jsonify(deal.to_dict())

@app.route('/api/deals/<int:id>', methods=['DELETE'])
def api_delete_deal(id):
    deal = Deal.query.get_or_404(id)
    db.session.delete(deal)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/deals/<int:id>/stage', methods=['PATCH'])
def api_update_deal_stage(id):
    deal = Deal.query.get_or_404(id)
    data = request.json
    deal.stage = data['stage']
    deal.probability = Deal.STAGE_PROBABILITY.get(data['stage'], deal.probability)
    db.session.commit()
    return jsonify(deal.to_dict())

# Activities API
@app.route('/api/activities', methods=['POST'])
def api_create_activity():
    data = request.json
    activity = Activity(
        type=data['type'], subject=data['subject'],
        description=data.get('description'),
        contact_id=data.get('contact_id') or None,
        deal_id=data.get('deal_id') or None,
        completed=data.get('completed', False)
    )
    if data.get('due_date'):
        activity.due_date = datetime.strptime(data['due_date'], '%Y-%m-%dT%H:%M')
    db.session.add(activity)
    db.session.commit()
    return jsonify(activity.to_dict()), 201

@app.route('/api/activities/<int:id>', methods=['PUT'])
def api_update_activity(id):
    activity = Activity.query.get_or_404(id)
    data = request.json
    for field in ['type', 'subject', 'description', 'completed']:
        if field in data:
            setattr(activity, field, data[field])
    if 'contact_id' in data:
        activity.contact_id = data['contact_id'] or None
    if 'deal_id' in data:
        activity.deal_id = data['deal_id'] or None
    if 'due_date' in data and data['due_date']:
        activity.due_date = datetime.strptime(data['due_date'], '%Y-%m-%dT%H:%M')
    if data.get('completed') and not activity.completed_at:
        activity.completed_at = datetime.utcnow()
    db.session.commit()
    return jsonify(activity.to_dict())

@app.route('/api/activities/<int:id>', methods=['DELETE'])
def api_delete_activity(id):
    activity = Activity.query.get_or_404(id)
    db.session.delete(activity)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/activities/<int:id>/complete', methods=['PATCH'])
def api_complete_activity(id):
    activity = Activity.query.get_or_404(id)
    activity.completed = not activity.completed
    if activity.completed:
        activity.completed_at = datetime.utcnow()
        # update contact last_contacted
        if activity.contact_id:
            activity.contact.last_contacted = datetime.utcnow()
    else:
        activity.completed_at = None
    db.session.commit()
    return jsonify(activity.to_dict())

# Tags API
@app.route('/api/tags', methods=['GET'])
def api_tags():
    tags = Tag.query.order_by(Tag.name).all()
    return jsonify([{'id': t.id, 'name': t.name, 'color': t.color} for t in tags])

@app.route('/api/tags', methods=['POST'])
def api_create_tag():
    data = request.json
    tag = Tag(name=data['name'], color=data.get('color', '#6366f1'))
    db.session.add(tag)
    db.session.commit()
    return jsonify({'id': tag.id, 'name': tag.name, 'color': tag.color}), 201

# Search API
@app.route('/api/search')
def api_search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'contacts': [], 'companies': [], 'deals': []})
    contacts = Contact.query.filter(
        db.or_(Contact.first_name.ilike(f'%{q}%'), Contact.last_name.ilike(f'%{q}%'),
               Contact.email.ilike(f'%{q}%'))
    ).limit(5).all()
    companies = Company.query.filter(Company.name.ilike(f'%{q}%')).limit(5).all()
    deals = Deal.query.filter(Deal.title.ilike(f'%{q}%')).limit(5).all()
    return jsonify({
        'contacts': [{'id': c.id, 'name': c.full_name, 'email': c.email} for c in contacts],
        'companies': [{'id': c.id, 'name': c.name, 'industry': c.industry} for c in companies],
        'deals': [{'id': d.id, 'title': d.title, 'stage': d.stage, 'value': d.value} for d in deals]
    })

# Export
@app.route('/api/contacts/export')
def export_contacts():
    contacts = Contact.query.all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['First Name', 'Last Name', 'Email', 'Phone', 'Job Title',
                     'Company', 'Status', 'Source', 'Created'])
    for c in contacts:
        writer.writerow([c.first_name, c.last_name, c.email, c.phone,
                         c.job_title, c.company.name if c.company else '',
                         c.status, c.source, c.created_at.strftime('%Y-%m-%d')])
    output.seek(0)
    from flask import Response
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=contacts.csv'})

# Create tables and seed default tags on startup (local SQLite and Vercel/Postgres)
with app.app_context():
    db.create_all()
    if Tag.query.count() == 0:
        for name, color in [('VIP', '#ef4444'), ('Hot Lead', '#f97316'),
                              ('Partner', '#8b5cf6'), ('Newsletter', '#06b6d4')]:
            db.session.add(Tag(name=name, color=color))
        db.session.commit()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
