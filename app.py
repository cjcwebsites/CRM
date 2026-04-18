from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date, timedelta
import json
import csv
import io
import os

TRADES = ['Plumber','Electrician','HVAC/AC','Roofer','Painter',
          'Landscaper','General Contractor','Carpenter','Handyman','Other']

LEAD_STAGES = ['new','voicemail','interested','proposal','won','lost','dnc']
LEAD_STAGE_LABELS = {
    'new':'New Lead','voicemail':'Voicemail','interested':'Interested',
    'proposal':'Proposal Sent','won':'Client (Won)','lost':'Not Interested','dnc':'Do Not Call'
}
# Days until suggested follow-up after each stage
FOLLOW_UP_DAYS = {'voicemail':3,'interested':1,'proposal':3,'new':1}

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

class SyncConfig(db.Model):
    """Stores the Google Sheet URL and saved credentials filename for live sync."""
    id = db.Column(db.Integer, primary_key=True)
    sheet_url = db.Column(db.String(500))
    credentials_filename = db.Column(db.String(300))  # filename inside uploads/
    is_active = db.Column(db.Boolean, default=False)
    last_synced = db.Column(db.DateTime)
    last_result = db.Column(db.Text)  # JSON: {added, removed, errors}

class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    website = db.Column(db.String(200))
    industry = db.Column(db.String(100))
    phone = db.Column(db.String(50))
    email = db.Column(db.String(200))
    address = db.Column(db.Text)
    city = db.Column(db.String(100))
    state = db.Column(db.String(100))
    zipcode = db.Column(db.String(20))
    categories = db.Column(db.String(300))
    review_count = db.Column(db.Integer)
    rating = db.Column(db.Float)
    place_id = db.Column(db.String(200))
    search_query = db.Column(db.String(300))
    sheet_synced = db.Column(db.Boolean, default=False)  # True = came from Google Sheet
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    contacts = db.relationship('Contact', backref='company', lazy=True)
    deals = db.relationship('Deal', backref='company', lazy=True)

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'website': self.website,
            'industry': self.industry, 'phone': self.phone, 'email': self.email,
            'address': self.address, 'city': self.city, 'state': self.state,
            'zipcode': self.zipcode, 'categories': self.categories,
            'review_count': self.review_count, 'rating': self.rating,
            'place_id': self.place_id, 'search_query': self.search_query,
            'sheet_synced': self.sheet_synced, 'notes': self.notes,
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
    status = db.Column(db.String(50), default='lead')
    source = db.Column(db.String(100))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_contacted = db.Column(db.DateTime)
    # ── Cold-call / trades fields ──────────────────────────────────────────
    trade = db.Column(db.String(100))           # Plumber, Electrician, etc.
    business_name = db.Column(db.String(200))   # Their business name
    monthly_fee = db.Column(db.Float, default=0)# What we charge them/month
    website_url = db.Column(db.String(200))     # Their live website
    website_status = db.Column(db.String(50), default='not_started')  # not_started / building / live
    client_since = db.Column(db.Date)           # Date they signed up
    lead_stage = db.Column(db.String(50), default='new')  # new/voicemail/interested/proposal/won/lost/dnc
    next_follow_up = db.Column(db.DateTime)     # When to call them next
    call_count = db.Column(db.Integer, default=0)
    last_call_date = db.Column(db.DateTime)
    tags = db.relationship('Tag', secondary=contact_tags, backref='contacts')
    activities = db.relationship('Activity', backref='contact', lazy=True, cascade='all, delete-orphan')
    deals = db.relationship('Deal', backref='contact', lazy=True)
    invoices = db.relationship('Invoice', backref='contact', lazy=True, cascade='all, delete-orphan')

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def display_name(self):
        return self.business_name or self.full_name

    def to_dict(self):
        return {
            'id': self.id, 'first_name': self.first_name, 'last_name': self.last_name,
            'full_name': self.full_name, 'display_name': self.display_name,
            'email': self.email, 'phone': self.phone, 'mobile': self.mobile,
            'job_title': self.job_title,
            'company_id': self.company_id,
            'company_name': self.company.name if self.company else '',
            'address': self.address, 'linkedin': self.linkedin, 'twitter': self.twitter,
            'status': self.status, 'source': self.source, 'notes': self.notes,
            'trade': self.trade, 'business_name': self.business_name,
            'monthly_fee': self.monthly_fee or 0,
            'website_url': self.website_url, 'website_status': self.website_status,
            'client_since': self.client_since.strftime('%Y-%m-%d') if self.client_since else None,
            'lead_stage': self.lead_stage or 'new',
            'next_follow_up': self.next_follow_up.strftime('%Y-%m-%dT%H:%M') if self.next_follow_up else None,
            'call_count': self.call_count or 0,
            'last_call_date': self.last_call_date.strftime('%Y-%m-%d %H:%M') if self.last_call_date else None,
            'created_at': self.created_at.strftime('%Y-%m-%d'),
            'last_contacted': self.last_contacted.strftime('%Y-%m-%d') if self.last_contacted else None,
            'tags': [{'id': t.id, 'name': t.name, 'color': t.color} for t in self.tags]
        }

class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey('contact.id'), nullable=False)
    invoice_number = db.Column(db.String(50), unique=True)
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(300))
    due_date = db.Column(db.Date)
    paid_date = db.Column(db.Date)
    status = db.Column(db.String(20), default='pending')  # pending / paid / overdue / cancelled
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'contact_id': self.contact_id,
            'client_name': self.contact.display_name,
            'invoice_number': self.invoice_number, 'amount': self.amount,
            'description': self.description,
            'due_date': self.due_date.strftime('%Y-%m-%d') if self.due_date else None,
            'paid_date': self.paid_date.strftime('%Y-%m-%d') if self.paid_date else None,
            'status': self.status, 'notes': self.notes,
            'created_at': self.created_at.strftime('%Y-%m-%d')
        }

def generate_invoice_number():
    ym = date.today().strftime('%Y%m')
    last = Invoice.query.filter(Invoice.invoice_number.like(f'INV-{ym}-%'))\
                        .order_by(Invoice.id.desc()).first()
    seq = (int(last.invoice_number.split('-')[-1]) + 1) if last else 1
    return f'INV-{ym}-{seq:03d}'

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
    now = datetime.utcnow()
    today_end = now.replace(hour=23, minute=59, second=59)
    clients = Contact.query.filter_by(lead_stage='won').all()
    mrr = sum(c.monthly_fee or 0 for c in clients)
    # Auto-mark overdue invoices
    Invoice.query.filter(Invoice.status=='pending', Invoice.due_date < date.today())\
                 .update({'status':'overdue'})
    db.session.commit()
    stats = {
        'mrr': mrr,
        'client_count': len(clients),
        'total_leads': Contact.query.filter(Contact.lead_stage.notin_(['won','lost','dnc'])).count(),
        'follow_ups_today': Contact.query.filter(
            Contact.lead_stage.notin_(['won','lost','dnc']),
            Contact.next_follow_up <= today_end).count(),
        'overdue_invoices': Invoice.query.filter_by(status='overdue').count(),
        'paid_this_month': db.session.query(db.func.sum(Invoice.amount)).filter(
            Invoice.status=='paid',
            Invoice.paid_date >= date.today().replace(day=1)).scalar() or 0,
        'interested_leads': Contact.query.filter_by(lead_stage='interested').count(),
        'proposal_leads': Contact.query.filter_by(lead_stage='proposal').count(),
    }
    follow_ups_due = Contact.query.filter(
        Contact.lead_stage.notin_(['won','lost','dnc']),
        Contact.next_follow_up <= today_end
    ).order_by(Contact.next_follow_up).limit(8).all()
    recent_clients = Contact.query.filter_by(lead_stage='won')\
                                  .order_by(Contact.client_since.desc()).limit(5).all()
    recent_invoices = Invoice.query.order_by(Invoice.created_at.desc()).limit(5).all()
    stage_counts = {s: Contact.query.filter_by(lead_stage=s).count() for s in LEAD_STAGES}
    return render_template('dashboard.html', stats=stats,
                           follow_ups_due=follow_ups_due,
                           recent_clients=recent_clients,
                           recent_invoices=recent_invoices,
                           stage_counts=stage_counts,
                           stage_labels=LEAD_STAGE_LABELS,
                           trades=TRADES, stages=LEAD_STAGES,
                           follow_up_days=FOLLOW_UP_DAYS,
                           now=now)

@app.route('/contacts')
def contacts():
    q             = request.args.get('q', '')
    stage_filter  = request.args.get('stage', '')
    trade_filter  = request.args.get('trade', '')
    website_filter = request.args.get('has_website', '')
    query = Contact.query.filter(Contact.lead_stage.notin_(['won']))
    if q:
        query = query.filter(
            db.or_(
                Contact.first_name.ilike(f'%{q}%'),
                Contact.last_name.ilike(f'%{q}%'),
                Contact.business_name.ilike(f'%{q}%'),
                Contact.email.ilike(f'%{q}%'),
                Contact.phone.ilike(f'%{q}%')
            )
        )
    if stage_filter:
        query = query.filter_by(lead_stage=stage_filter)
    if trade_filter:
        query = query.filter_by(trade=trade_filter)
    if website_filter == '1':
        query = query.filter(Contact.website_url != None, Contact.website_url != '')
    elif website_filter == '0':
        query = query.filter(db.or_(Contact.website_url == None, Contact.website_url == ''))
    contacts = query.order_by(Contact.created_at.desc()).all()
    return render_template('contacts.html', contacts=contacts, q=q,
                           stage_filter=stage_filter, trade_filter=trade_filter,
                           website_filter=website_filter,
                           trades=TRADES, stage_labels=LEAD_STAGE_LABELS,
                           follow_up_days=FOLLOW_UP_DAYS,
                           now=datetime.utcnow())

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

@app.route('/follow-ups')
def follow_ups():
    today = datetime.utcnow()
    overdue = Contact.query.filter(
        Contact.lead_stage.notin_(['won','lost','dnc']),
        Contact.next_follow_up < today
    ).order_by(Contact.next_follow_up).all()
    due_today = Contact.query.filter(
        Contact.lead_stage.notin_(['won','lost','dnc']),
        Contact.next_follow_up >= today,
        Contact.next_follow_up < today.replace(hour=23, minute=59)
    ).order_by(Contact.next_follow_up).all()
    upcoming = Contact.query.filter(
        Contact.lead_stage.notin_(['won','lost','dnc']),
        Contact.next_follow_up >= today.replace(hour=23, minute=59)
    ).order_by(Contact.next_follow_up).limit(20).all()
    no_followup = Contact.query.filter(
        Contact.lead_stage.notin_(['won','lost','dnc']),
        Contact.next_follow_up == None
    ).order_by(Contact.created_at.desc()).limit(20).all()
    return render_template('follow_ups.html', overdue=overdue,
                           due_today=due_today, upcoming=upcoming,
                           no_followup=no_followup, trades=TRADES,
                           stages=LEAD_STAGES, stage_labels=LEAD_STAGE_LABELS,
                           follow_up_days=FOLLOW_UP_DAYS, now=today)

@app.route('/clients')
def clients():
    clients = Contact.query.filter_by(lead_stage='won').order_by(Contact.client_since.desc()).all()
    mrr = sum(c.monthly_fee or 0 for c in clients)
    overdue_invoices = Invoice.query.filter_by(status='overdue').count()
    return render_template('clients.html', clients=clients, mrr=mrr,
                           overdue_invoices=overdue_invoices, trades=TRADES)

@app.route('/invoices')
def invoices():
    status_filter = request.args.get('status', '')
    client_filter = request.args.get('client_id', '')
    query = Invoice.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    if client_filter:
        query = query.filter_by(contact_id=client_filter)
    # Auto-mark overdue
    Invoice.query.filter(
        Invoice.status == 'pending',
        Invoice.due_date < date.today()
    ).update({'status': 'overdue'})
    db.session.commit()
    invoices = query.order_by(Invoice.created_at.desc()).all()
    clients = Contact.query.filter_by(lead_stage='won').order_by(Contact.first_name).all()
    total_outstanding = db.session.query(db.func.sum(Invoice.amount))\
        .filter(Invoice.status.in_(['pending','overdue'])).scalar() or 0
    total_paid_month = db.session.query(db.func.sum(Invoice.amount))\
        .filter(Invoice.status == 'paid',
                Invoice.paid_date >= date.today().replace(day=1)).scalar() or 0
    return render_template('invoices.html', invoices=invoices, clients=clients,
                           status_filter=status_filter, client_filter=client_filter,
                           total_outstanding=total_outstanding, total_paid_month=total_paid_month)

@app.route('/analytics')
def analytics():
    clients = Contact.query.filter_by(lead_stage='won').all()
    mrr = sum(c.monthly_fee or 0 for c in clients)
    all_leads = Contact.query.all()
    stage_counts = {s: Contact.query.filter_by(lead_stage=s).count() for s in LEAD_STAGES}
    trade_counts = {}
    for t in TRADES:
        trade_counts[t] = Contact.query.filter_by(trade=t).count()
    trade_counts = {k:v for k,v in trade_counts.items() if v > 0}
    # Revenue last 6 months
    revenue_months = []
    for i in range(5, -1, -1):
        d = date.today().replace(day=1)
        month = (d.month - i - 1) % 12 + 1
        year  = d.year - ((d.month - i - 1) // 12)
        rev = db.session.query(db.func.sum(Invoice.amount)).filter(
            Invoice.status == 'paid',
            db.extract('month', Invoice.paid_date) == month,
            db.extract('year',  Invoice.paid_date) == year
        ).scalar() or 0
        revenue_months.append({'label': date(year, month, 1).strftime('%b'), 'value': rev})
    # Calls this month
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)
    calls_month = Activity.query.filter(
        Activity.type == 'call', Activity.created_at >= month_start).count()
    won_count  = stage_counts.get('won', 0)
    lost_count = stage_counts.get('lost', 0)
    conversion = round(won_count / (won_count + lost_count) * 100) if (won_count + lost_count) > 0 else 0
    max_rev = max((m['value'] for m in revenue_months), default=1) or 1
    return render_template('analytics.html',
                           mrr=mrr, clients_count=len(clients),
                           stage_counts=stage_counts, trade_counts=trade_counts,
                           revenue_months=revenue_months, max_rev=max_rev,
                           calls_month=calls_month, conversion=conversion,
                           stage_labels=LEAD_STAGE_LABELS, all_leads_count=len(all_leads))

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
        first_name=data['first_name'], last_name=data.get('last_name', '—'),
        email=data.get('email'), phone=data.get('phone'),
        mobile=data.get('mobile'), job_title=data.get('job_title'),
        company_id=data.get('company_id') or None,
        address=data.get('address'), linkedin=data.get('linkedin'),
        twitter=data.get('twitter'), status=data.get('status', 'lead'),
        source=data.get('source', 'Cold Call'), notes=data.get('notes'),
        trade=data.get('trade'), business_name=data.get('business_name'),
        monthly_fee=data.get('monthly_fee', 0),
        lead_stage=data.get('lead_stage', 'new'),
    )
    if data.get('next_follow_up'):
        contact.next_follow_up = datetime.strptime(data['next_follow_up'], '%Y-%m-%dT%H:%M')
    elif data.get('lead_stage') in FOLLOW_UP_DAYS:
        days = FOLLOW_UP_DAYS[data['lead_stage']]
        contact.next_follow_up = datetime.utcnow().replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=days)
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
                  'source', 'notes', 'trade', 'business_name', 'monthly_fee',
                  'website_url', 'website_status', 'lead_stage']:
        if field in data:
            setattr(contact, field, data[field])
    if 'company_id' in data:
        contact.company_id = data['company_id'] or None
    if 'tag_ids' in data:
        contact.tags = Tag.query.filter(Tag.id.in_(data['tag_ids'])).all()
    if 'last_contacted' in data and data['last_contacted']:
        contact.last_contacted = datetime.strptime(data['last_contacted'], '%Y-%m-%d')
    if 'next_follow_up' in data and data['next_follow_up']:
        contact.next_follow_up = datetime.strptime(data['next_follow_up'], '%Y-%m-%dT%H:%M')
    elif 'next_follow_up' in data and not data['next_follow_up']:
        contact.next_follow_up = None
    if 'client_since' in data and data['client_since']:
        from datetime import datetime as _dt
        contact.client_since = _dt.strptime(data['client_since'], '%Y-%m-%d').date()
    if data.get('lead_stage') == 'won' and not contact.client_since:
        contact.client_since = date.today()
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
        city=data.get('city'), state=data.get('state'), zipcode=data.get('zipcode'),
        categories=data.get('categories'), review_count=data.get('review_count'),
        rating=data.get('rating'), place_id=data.get('place_id'),
        search_query=data.get('search_query'), notes=data.get('notes')
    )
    db.session.add(company)
    db.session.commit()
    return jsonify(company.to_dict()), 201

@app.route('/api/companies/<int:id>', methods=['PUT'])
def api_update_company(id):
    company = Company.query.get_or_404(id)
    data = request.json
    for field in ['name', 'website', 'industry', 'phone', 'email', 'address',
                  'city', 'state', 'zipcode', 'categories', 'review_count',
                  'rating', 'place_id', 'search_query', 'notes']:
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

@app.route('/api/companies/<int:id>/notes', methods=['PATCH'])
def api_company_notes(id):
    company = Company.query.get_or_404(id)
    company.notes = request.json.get('notes', '')
    db.session.commit()
    return jsonify({'success': True, 'notes': company.notes})

# ── Lead/contact quick log call ───────────────────────────────────────────────
@app.route('/api/contacts/<int:id>/log-call', methods=['POST'])
def api_log_call(id):
    contact = Contact.query.get_or_404(id)
    data = request.json
    contact.call_count = (contact.call_count or 0) + 1
    contact.last_call_date = datetime.utcnow()
    contact.last_contacted = datetime.utcnow()
    if data.get('lead_stage'):
        contact.lead_stage = data['lead_stage']
        # If won, set client_since
        if data['lead_stage'] == 'won' and not contact.client_since:
            contact.client_since = date.today()
    if data.get('next_follow_up'):
        contact.next_follow_up = datetime.strptime(data['next_follow_up'], '%Y-%m-%dT%H:%M')
    elif data.get('lead_stage') in FOLLOW_UP_DAYS:
        days = FOLLOW_UP_DAYS[data['lead_stage']]
        fu = datetime.utcnow().replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=days)
        contact.next_follow_up = fu
    if data.get('notes'):
        activity = Activity(
            type='call', subject=f"Called {contact.display_name}",
            description=data['notes'], contact_id=id,
            completed=True, completed_at=datetime.utcnow()
        )
        db.session.add(activity)
    db.session.commit()
    return jsonify(contact.to_dict())

# ── Invoice API ───────────────────────────────────────────────────────────────
@app.route('/api/invoices', methods=['POST'])
def api_create_invoice():
    data = request.json
    inv = Invoice(
        contact_id=data['contact_id'],
        invoice_number=generate_invoice_number(),
        amount=data['amount'],
        description=data.get('description', 'Monthly Website Fee'),
        due_date=datetime.strptime(data['due_date'], '%Y-%m-%d').date() if data.get('due_date') else (date.today() + timedelta(days=14)),
        status='pending',
        notes=data.get('notes')
    )
    db.session.add(inv)
    db.session.commit()
    return jsonify(inv.to_dict()), 201

@app.route('/api/invoices/<int:id>/mark-paid', methods=['PATCH'])
def api_mark_paid(id):
    inv = Invoice.query.get_or_404(id)
    inv.status = 'paid'
    inv.paid_date = date.today()
    db.session.commit()
    return jsonify(inv.to_dict())

@app.route('/api/invoices/<int:id>/cancel', methods=['PATCH'])
def api_cancel_invoice(id):
    inv = Invoice.query.get_or_404(id)
    inv.status = 'cancelled'
    db.session.commit()
    return jsonify(inv.to_dict())

@app.route('/api/invoices/<int:id>', methods=['DELETE'])
def api_delete_invoice(id):
    inv = Invoice.query.get_or_404(id)
    db.session.delete(inv)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/invoices/generate-monthly', methods=['POST'])
def api_generate_monthly():
    """Auto-create invoices for all active clients who don't have one this month."""
    clients = Contact.query.filter_by(lead_stage='won').all()
    month_start = date.today().replace(day=1)
    created = 0
    for c in clients:
        if not c.monthly_fee:
            continue
        existing = Invoice.query.filter(
            Invoice.contact_id == c.id,
            Invoice.created_at >= datetime.combine(month_start, datetime.min.time())
        ).first()
        if not existing:
            inv = Invoice(
                contact_id=c.id,
                invoice_number=generate_invoice_number(),
                amount=c.monthly_fee,
                description=f"Monthly Website Fee — {date.today().strftime('%B %Y')}",
                due_date=date.today() + timedelta(days=14),
                status='pending'
            )
            db.session.add(inv)
            created += 1
    db.session.commit()
    return jsonify({'created': created})

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

# ─── Google Sheets Live Sync ──────────────────────────────────────────────────

import threading, time as _time_mod

UPLOADS_DIR = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOADS_DIR, exist_ok=True)

COL_MAP = {
    'name': 'name', 'website': 'website', 'phone': 'phone',
    'address': 'address', 'city': 'city', 'state': 'state',
    'categories': 'categories', 'review_count': 'review_count',
    'rating': 'rating', 'status': 'company_status',
    'place_id': 'place_id', 'query': 'search_query',
}

def lookup_zipcode(address, city, state):
    """Call Nominatim (OpenStreetMap) to find a zip code from an address — free, no API key."""
    import requests as req_lib
    query = ', '.join(p for p in [address, city, state] if p)
    if not query:
        return ''
    try:
        resp = req_lib.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': query, 'format': 'json', 'addressdetails': 1, 'limit': 1},
            headers={'User-Agent': 'MyCRM/1.0'},
            timeout=6
        )
        if resp.ok and resp.json():
            return resp.json()[0].get('address', {}).get('postcode', '')
    except Exception:
        pass
    return ''

def is_green(bg):
    """Return True if a Google Sheets background color dict looks green."""
    r = bg.get('red', 1.0)
    g = bg.get('green', 1.0)
    b = bg.get('blue', 1.0)
    # Skip white / default (all channels near 1)
    if r > 0.9 and g > 0.9 and b > 0.9:
        return False
    # Green channel must be dominant and meaningful
    return g > r and g > b and g > 0.35

@app.route('/import-sheet')
def import_sheet_page():
    config = SyncConfig.query.first()
    return render_template('import_sheet.html', config=config)

# ── Setup: save credentials file + sheet URL ──────────────────────────────────
@app.route('/api/sync/setup', methods=['POST'])
def api_sync_setup():
    sheet_url  = request.form.get('sheet_url', '').strip()
    creds_file = request.files.get('credentials')

    if not sheet_url:
        return jsonify({'error': 'Please enter your Google Sheet URL.'}), 400

    import re, json as json_lib
    if not re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', sheet_url):
        return jsonify({'error': 'That does not look like a valid Google Sheets URL.'}), 400

    config = SyncConfig.query.first() or SyncConfig()

    # Save credentials file if a new one was uploaded
    if creds_file and creds_file.filename:
        try:
            json_lib.load(creds_file)          # validate it's valid JSON
            creds_file.seek(0)
        except Exception:
            return jsonify({'error': 'Credentials file is not valid JSON.'}), 400
        filename = 'google_credentials.json'
        creds_file.save(os.path.join(UPLOADS_DIR, filename))
        config.credentials_filename = filename
    elif not config.credentials_filename:
        return jsonify({'error': 'Please upload your Google credentials JSON file.'}), 400

    config.sheet_url = sheet_url
    db.session.add(config)
    db.session.commit()
    return jsonify({'success': True})

# ── Toggle live sync on/off ───────────────────────────────────────────────────
@app.route('/api/sync/toggle', methods=['POST'])
def api_sync_toggle():
    config = SyncConfig.query.first()
    if not config or not config.sheet_url or not config.credentials_filename:
        return jsonify({'error': 'Set up your sheet first.'}), 400
    config.is_active = not config.is_active
    db.session.commit()
    return jsonify({'is_active': config.is_active})

# ── Status ────────────────────────────────────────────────────────────────────
@app.route('/api/sync/status')
def api_sync_status():
    config = SyncConfig.query.first()
    if not config:
        return jsonify({'configured': False})
    import json as json_lib
    result = json_lib.loads(config.last_result) if config.last_result else {}
    return jsonify({
        'configured': bool(config.sheet_url and config.credentials_filename),
        'is_active': config.is_active,
        'sheet_url': config.sheet_url,
        'last_synced': config.last_synced.strftime('%b %d %H:%M:%S') if config.last_synced else None,
        'last_result': result,
    })

# ── Manual sync now ───────────────────────────────────────────────────────────
@app.route('/api/sync/now', methods=['POST'])
def api_sync_now():
    result = do_sync()
    return jsonify(result)

# ── Core sync function (called by background thread and manual trigger) ────────
def do_sync():
    import re, json as json_lib
    from googleapiclient.discovery import build
    from google.oauth2.service_account import Credentials as GCredentials

    with app.app_context():
        config = SyncConfig.query.first()
        if not config or not config.sheet_url or not config.credentials_filename:
            return {'error': 'Not configured'}

        creds_path = os.path.join(UPLOADS_DIR, config.credentials_filename)
        if not os.path.exists(creds_path):
            return {'error': 'Credentials file missing'}

        try:
            with open(creds_path) as f:
                creds_info = json_lib.load(f)
        except Exception as e:
            return {'error': f'Could not read credentials: {e}'}

        match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', config.sheet_url)
        if not match:
            return {'error': 'Invalid sheet URL'}
        spreadsheet_id = match.group(1)

        try:
            creds = GCredentials.from_service_account_info(
                creds_info, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
            service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
            result = service.spreadsheets().get(
                spreadsheetId=spreadsheet_id, includeGridData=True).execute()
        except Exception as e:
            return {'error': f'Google Sheets error: {e}'}

        rows = result['sheets'][0]['data'][0].get('rowData', [])
        if len(rows) < 2:
            return {'error': 'Sheet looks empty'}

        headers = [cell.get('formattedValue', '').strip().lower()
                   for cell in rows[0].get('values', [])]

        green_place_ids = set()
        green_names     = set()
        added = removed = 0
        errors = []

        for row in rows[1:]:
            values = row.get('values', [])
            if not values:
                continue

            row_is_green = any(
                is_green(cell.get('userEnteredFormat', {}).get('backgroundColor', {}))
                for cell in values)

            # Parse row into a dict using COL_MAP
            row_data = {}
            for i, header in enumerate(headers):
                if header in COL_MAP:
                    row_data[COL_MAP[header]] = (
                        values[i].get('formattedValue', '').strip() if i < len(values) else '')

            company_name = row_data.get('name', '').strip()
            place_id     = row_data.get('place_id', '').strip()
            if not company_name:
                continue

            # Track what is currently green so we can remove un-highlighted rows
            if row_is_green:
                if place_id: green_place_ids.add(place_id)
                green_names.add(company_name)

                # Find existing record
                existing = (Company.query.filter_by(place_id=place_id).first() if place_id
                            else Company.query.filter_by(name=company_name).first())
                if existing:
                    continue  # already in CRM, nothing to do

                # Look up zip code then create the company
                address = row_data.get('address', '')
                city    = row_data.get('city', '')
                state   = row_data.get('state', '')
                zipcode = lookup_zipcode(address, city, state)
                _time_mod.sleep(1.1)   # Nominatim: 1 req/sec limit
                full_address = ', '.join(p for p in [address, city, state, zipcode] if p)

                try:
                    rc = row_data.get('review_count', '')
                    rt = row_data.get('rating', '')
                    company = Company(
                        name=company_name,
                        website=row_data.get('website') or None,
                        phone=row_data.get('phone') or None,
                        address=full_address or None,
                        city=city or None, state=state or None, zipcode=zipcode or None,
                        categories=row_data.get('categories') or None,
                        review_count=int(rc) if rc and rc.isdigit() else None,
                        rating=float(rt) if rt else None,
                        place_id=place_id or None,
                        search_query=row_data.get('search_query') or None,
                        sheet_synced=True,
                    )
                    db.session.add(company)
                    db.session.commit()
                    added += 1
                except Exception as e:
                    db.session.rollback()
                    errors.append(f"{company_name}: {e}")

        # Remove sheet-synced companies whose rows are no longer highlighted green
        for company in Company.query.filter_by(sheet_synced=True).all():
            still_green = (
                (company.place_id and company.place_id in green_place_ids) or
                (not company.place_id and company.name in green_names)
            )
            if not still_green:
                try:
                    db.session.delete(company)
                    db.session.commit()
                    removed += 1
                except Exception as e:
                    db.session.rollback()
                    errors.append(f"Remove {company.name}: {e}")

        # Persist result
        import json as json_lib2
        config.last_synced = datetime.utcnow()
        config.last_result = json_lib2.dumps({'added': added, 'removed': removed, 'errors': errors})
        db.session.commit()

        return {'added': added, 'removed': removed, 'errors': errors}

# ── Background polling thread (runs every 30 s when sync is active) ───────────
def _sync_loop():
    _time_mod.sleep(10)   # give Flask a moment to fully start
    while True:
        try:
            with app.app_context():
                config = SyncConfig.query.first()
                if config and config.is_active:
                    do_sync()
        except Exception:
            pass
        _time_mod.sleep(30)

threading.Thread(target=_sync_loop, daemon=True).start()

# Create tables and seed default tags on startup (local SQLite and Vercel/Postgres)
with app.app_context():
    db.create_all()
    # Add any new Company columns that don't exist yet (safe to run on every startup)
    company_columns = [
        ('city',         'VARCHAR(100)'),
        ('state',        'VARCHAR(100)'),
        ('zipcode',      'VARCHAR(20)'),
        ('categories',   'VARCHAR(300)'),
        ('review_count', 'INTEGER'),
        ('rating',       'FLOAT'),
        ('place_id',     'VARCHAR(200)'),
        ('search_query', 'VARCHAR(300)'),
        ('sheet_synced', 'BOOLEAN DEFAULT 0'),
        ('notes',        'TEXT'),
    ]
    # Add cold-call fields to Contact table
    contact_columns = [
        ('trade',           'VARCHAR(100)'),
        ('business_name',   'VARCHAR(200)'),
        ('monthly_fee',     'FLOAT DEFAULT 0'),
        ('website_url',     'VARCHAR(200)'),
        ('website_status',  'VARCHAR(50) DEFAULT "not_started"'),
        ('client_since',    'DATE'),
        ('lead_stage',      'VARCHAR(50) DEFAULT "new"'),
        ('next_follow_up',  'DATETIME'),
        ('call_count',      'INTEGER DEFAULT 0'),
        ('last_call_date',  'DATETIME'),
    ]
    with db.engine.connect() as conn:
        for col_name, col_type in company_columns:
            try:
                conn.execute(db.text(f'ALTER TABLE company ADD COLUMN {col_name} {col_type}'))
                conn.commit()
            except Exception:
                pass  # column already exists — fine
        for col_name, col_type in contact_columns:
            try:
                conn.execute(db.text(f'ALTER TABLE contact ADD COLUMN {col_name} {col_type}'))
                conn.commit()
            except Exception:
                pass  # column already exists — fine
    if Tag.query.count() == 0:
        for name, color in [('VIP', '#ef4444'), ('Hot Lead', '#f97316'),
                              ('Partner', '#8b5cf6'), ('Newsletter', '#06b6d4')]:
            db.session.add(Tag(name=name, color=color))
        db.session.commit()

# ─── Phone Lookup from Public Records ────────────────────────────────────────

import re as _re
from bs4 import BeautifulSoup as _BS

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/124.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

def _normalize_phone(raw):
    digits = _re.sub(r'\D', '', raw)
    if len(digits) == 10:
        return f'({digits[:3]}) {digits[3:6]}-{digits[6:]}'
    if len(digits) == 11 and digits[0] == '1':
        return f'({digits[1:4]}) {digits[4:7]}-{digits[7:]}'
    return None

def _extract_phones(text):
    found = _re.findall(
        r'(\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})', text)
    seen, out = set(), []
    for p in found:
        n = _normalize_phone(p)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ── State-specific license lookup scrapers ────────────────────────────────────

def _lookup_california(license_num):
    """California CSLB — public contractor license lookup."""
    import requests as r
    try:
        resp = r.get(
            'https://www2.cslb.ca.gov/OnlineServices/CheckLicenseII/LicenseDetail.aspx',
            params={'LicNum': license_num}, headers=_HEADERS, timeout=12)
        soup = _BS(resp.text, 'lxml')
        phone_el = soup.find(id='ctl00_ContentPlaceHolder1_LicenseDetailList_lblPhoneNum')
        name_el  = soup.find(id='ctl00_ContentPlaceHolder1_LicenseDetailList_lblLicenseeName')
        addr_el  = soup.find(id='ctl00_ContentPlaceHolder1_LicenseDetailList_lblAddress')
        phone = _normalize_phone(phone_el.text.strip()) if phone_el else ''
        name  = name_el.text.strip() if name_el else ''
        addr  = addr_el.text.strip() if addr_el else ''
        if phone:
            return [{'source': 'California CSLB', 'phone': phone,
                     'name': name, 'address': addr, 'website': ''}]
    except Exception:
        pass
    return []

def _lookup_florida(license_num):
    """Florida DBPR — public license lookup."""
    import requests as r
    try:
        resp = r.get(
            'https://www.myfloridalicense.com/LicenseDetail.asp',
            params={'SID': '', 'id': license_num},
            headers=_HEADERS, timeout=12)
        soup = _BS(resp.text, 'lxml')
        phones = _extract_phones(soup.get_text())
        tables = soup.find_all('table')
        name, addr = '', ''
        for t in tables:
            txt = t.get_text()
            if 'Primary' in txt or 'Business' in txt:
                rows = t.find_all('tr')
                for row in rows:
                    cells = [td.get_text(strip=True) for td in row.find_all('td')]
                    if len(cells) >= 2:
                        if 'Name' in cells[0] and not name:
                            name = cells[1]
                        if 'Address' in cells[0] and not addr:
                            addr = cells[1]
        if phones:
            return [{'source': 'Florida DBPR', 'phone': phones[0],
                     'name': name, 'address': addr, 'website': ''}]
    except Exception:
        pass
    return []

def _lookup_texas(license_num):
    """Texas TDLR — public license lookup."""
    import requests as r
    try:
        resp = r.get(
            'https://www.tdlr.texas.gov/LicenseSearch/licfile.asp',
            params={'licnum': license_num},
            headers=_HEADERS, timeout=12)
        soup = _BS(resp.text, 'lxml')
        phones = _extract_phones(soup.get_text())
        name_el = soup.find('td', string=_re.compile(r'License Name', _re.I))
        name = ''
        if name_el and name_el.find_next_sibling('td'):
            name = name_el.find_next_sibling('td').get_text(strip=True)
        if phones:
            return [{'source': 'Texas TDLR', 'phone': phones[0],
                     'name': name, 'address': '', 'website': ''}]
    except Exception:
        pass
    return []

def _lookup_web_search(name, trade, county, state_name, license_num):
    """DuckDuckGo HTML search — scrape public listings for phone numbers."""
    import requests as r
    query_parts = [p for p in [name, trade, county, state_name,
                               f'license {license_num}' if license_num else ''] if p]
    query = ' '.join(query_parts)
    results = []
    try:
        resp = r.post(
            'https://html.duckduckgo.com/html/',
            data={'q': query},
            headers={**_HEADERS, 'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=12)
        soup = _BS(resp.text, 'lxml')
        snippets = soup.select('.result__snippet, .result__body')
        full_text = ' '.join(s.get_text() for s in snippets[:15])
        phones = _extract_phones(full_text)
        for ph in phones[:3]:
            results.append({'source': 'Web Search', 'phone': ph,
                            'name': name, 'address': '', 'website': ''})
    except Exception:
        pass
    return results

def _lookup_whitepages_biz(name, county, state_name):
    """Search Yellowpages for business listing."""
    import requests as r
    try:
        query = f'{name} {county} {state_name}'
        resp = r.get(
            'https://www.yellowpages.com/search',
            params={'search_terms': name, 'geo_location_terms': f'{county}, {state_name}'},
            headers=_HEADERS, timeout=12)
        soup = _BS(resp.text, 'lxml')
        phones = _extract_phones(soup.get_text())
        links  = soup.select('.business-name')
        biz_name = links[0].get_text(strip=True) if links else name
        if phones:
            return [{'source': 'YellowPages', 'phone': phones[0],
                     'name': biz_name, 'address': '', 'website': ''}]
    except Exception:
        pass
    return []


STATE_SCRAPERS = {
    'CA': _lookup_california,
    'CALIFORNIA': _lookup_california,
    'FL': _lookup_florida,
    'FLORIDA': _lookup_florida,
    'TX': _lookup_texas,
    'TEXAS': _lookup_texas,
}


def _web_search_raw(query):
    """Return raw DuckDuckGo snippets for Claude to reason over."""
    import requests as r
    try:
        resp = r.post(
            'https://html.duckduckgo.com/html/',
            data={'q': query},
            headers={**_HEADERS, 'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=12
        )
        soup = _BS(resp.text, 'lxml')
        parts = soup.select('.result__snippet, .result__title, .result__url')
        return '\n'.join(p.get_text(strip=True) for p in parts[:15]) or 'No results.'
    except Exception as e:
        return f'Search error: {e}'


@app.route('/find-leads')
def find_leads():
    has_key = bool(os.environ.get('ANTHROPIC_API_KEY', '').strip())
    return render_template('find_leads.html', trades=TRADES, anthropic_key=has_key)

@app.route('/api/ai-lookup', methods=['POST'])
def api_ai_lookup():
    """Natural-language contractor lookup powered by Claude + public record scrapers."""
    prompt  = (request.json or {}).get('prompt', '').strip()
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()

    if not api_key:
        return jsonify({'error': 'no_key'})
    if not prompt:
        return jsonify({'error': 'Please enter a prompt.'})

    import anthropic as _ant

    tools = [
        {
            "name": "search_web",
            "description": (
                "Search the public web (DuckDuckGo) for contractor or business info. "
                "Use this to find phone numbers, business names, addresses, and websites."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Plain-text search query, e.g. 'John Smith plumbing Dallas TX license 12345'"}
                },
                "required": ["query"]
            }
        },
        {
            "name": "lookup_state_license",
            "description": (
                "Look up a contractor license on the official state licensing board "
                "to get the registrant's name, phone number, and address. "
                "Supported states: CA, FL, TX."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "state":          {"type": "string", "description": "Two-letter state code: CA, FL, or TX"},
                    "license_number": {"type": "string", "description": "Contractor license number"}
                },
                "required": ["state", "license_number"]
            }
        },
    ]

    system = (
        "You are a research assistant that finds contractor contact information from public records. "
        "When given contractor details, use the tools to find their info. "
        "Always try lookup_state_license first if a license number and state are present. "
        "Then use search_web to fill in missing details. "
        "ALWAYS respond in this exact format (use — if unknown):\n\n"
        "**Business Name:** <name>\n"
        "**Owner Name:** <name>\n"
        "**Phone:** <number>\n"
        "**Address:** <address>\n"
        "**Website:** <url or None>\n"
        "**Source:** <where you found it>\n"
        "**Notes:** <anything else useful>\n\n"
        "If nothing is found at all, say 'No results found' and explain what was searched."
    )

    messages = [{"role": "user", "content": prompt}]
    client   = _ant.Anthropic(api_key=api_key)

    for _ in range(6):  # max 6 tool-use rounds
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system,
            tools=tools,
            messages=messages,
        )

        if resp.stop_reason == 'end_turn':
            text = next((b.text for b in resp.content if hasattr(b, 'text')), '')
            return jsonify({'result': text})

        if resp.stop_reason == 'tool_use':
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type != 'tool_use':
                    continue
                if block.name == 'search_web':
                    output = _web_search_raw(block.input.get('query', ''))
                elif block.name == 'lookup_state_license':
                    state_key = block.input.get('state', '').upper()
                    lic_num   = block.input.get('license_number', '')
                    scraper   = STATE_SCRAPERS.get(state_key)
                    if scraper:
                        rows   = scraper(lic_num)
                        output = json.dumps(rows) if rows else 'No record found for that license.'
                    else:
                        output = (f'State {state_key} not directly supported. '
                                  'Try search_web with the license number.')
                else:
                    output = 'Unknown tool.'
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return jsonify({'result': 'Search complete — no definitive result found.'})


@app.route('/api/lookup-phone', methods=['POST'])
def api_lookup_phone():
    data        = request.json
    name        = data.get('name', '').strip()
    county      = data.get('county', '').strip()
    state_name  = data.get('state', '').strip()
    license_num = data.get('license_number', '').strip()
    trade       = data.get('trade', '').strip()

    if not name:
        return jsonify({'error': 'Please enter at least a full name.'}), 400

    results = []

    # 1. State-specific license board (if license number + state provided)
    if license_num and state_name:
        scraper = STATE_SCRAPERS.get(state_name.upper().strip())
        if scraper:
            try:
                results.extend(scraper(license_num))
            except Exception:
                pass

    # 2. YellowPages business lookup
    if not results and name:
        results.extend(_lookup_whitepages_biz(name, county, state_name))

    # 3. DuckDuckGo web search fallback
    if not results:
        results.extend(_lookup_web_search(name, trade, county, state_name, license_num))

    # Deduplicate by phone
    seen, deduped = set(), []
    for r in results:
        if r['phone'] not in seen:
            seen.add(r['phone'])
            deduped.append(r)

    return jsonify({'results': deduped[:5]})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
